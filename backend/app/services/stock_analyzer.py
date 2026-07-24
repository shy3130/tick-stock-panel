"""AI 个股分析服务 — 技术面 / 资金 / 基本面 / 财务面 / 消息面 五维综合分析。

职责:
  组合一只股票的 K 线(含已算好的技术指标)+ 财务表 + 关键价位 →
  拼装客观技术分析系统提示词 → 流式调用 LLM → 逐 chunk 吐给前端。

与 financial_analyzer.py 的区别(刻意区分,非复用):
  - 角色:客观技术分析师(非 CFA 财务分析师)
  - 数据源:K 线 + 技术指标为主,财务表为辅(财务分析以财务表为主)
  - 输出框架:技术面→资金→基本面→财务面→消息面(五维),落点是客观技术状态与风险提示
    (财务分析的落点是财务质量评级)。注意:本服务不输出买卖建议、操作指令。

不知道: HTTP、前端、配置持久化。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote

import httpx
import polars as pl

from app.indicators.levels import compute_levels, summarize_levels
from app.services.financial_sync import get_financial_df

logger = logging.getLogger(__name__)

# 注入最近多少根日 K(技术面分析样本)
_KLINE_WINDOW = 90
# 注入财务表的最近期数
_MAX_PERIODS = 4

_MARKET_CONTEXT = {
    "cn": ("A 股", "人民币"),
    "hk": ("港股", "港元"),
    "us": ("美股", "美元"),
}


def _resolve_market(symbol: str, market: str | None) -> str:
    normalized = (market or "").strip().lower()
    if normalized in _MARKET_CONTEXT:
        return normalized
    suffix = symbol.upper().rsplit(".", 1)[-1]
    if suffix == "HK":
        return "hk"
    if suffix == "US":
        return "us"
    return "cn"


def _build_capital_metrics(df: pl.DataFrame) -> dict[str, float]:
    """将成交额压缩成可复核的资金活跃度指标,不推断买卖方向。"""
    if df.is_empty() or "amount" not in df.columns:
        return {}
    values = [float(v) for v in df["amount"].drop_nulls().to_list()]
    if not values:
        return {}
    current = values[-1]
    ma5 = sum(values[-5:]) / min(5, len(values))
    ma20 = sum(values[-20:]) / min(20, len(values))
    window60 = values[-60:]
    return {
        "amount": round(current, 4),
        "amount_ma5": round(ma5, 4),
        "amount_ma20": round(ma20, 4),
        "amount_ratio_5d": round(current / ma5, 4) if ma5 else 0.0,
        "amount_ratio_20d": round(current / ma20, 4) if ma20 else 0.0,
        "amount_percentile_60d": round(
            sum(value <= current for value in window60) / len(window60), 4
        ),
    }


def _build_order_flow_context(snapshot: dict, series: dict) -> dict:
    """Keep observable order-size flows and discard upstream advice text."""
    if not snapshot.get("available"):
        return {}

    def bucket(name: str) -> dict:
        return {
            "in": snapshot.get(f"{name}In"),
            "out": snapshot.get(f"{name}Out"),
            "net": snapshot.get(f"{name}Net"),
        }

    summary = series.get("summary") if isinstance(series.get("summary"), dict) else {}
    interpretation = series.get("interpretation") if isinstance(series.get("interpretation"), dict) else {}
    recent = interpretation.get("recentChange") if isinstance(interpretation.get("recentChange"), dict) else {}
    return {
        "as_of": snapshot.get("snapshotMinute") or snapshot.get("capitalUpdatedAt"),
        "amount_unit": "万（对应市场币种）",
        "large": bucket("large"),
        "medium": bucket("medium"),
        "small": bucket("small"),
        "total_in": snapshot.get("totalIn"),
        "total_out": snapshot.get("totalOut"),
        "total_net": snapshot.get("totalNet"),
        "large_net_ratio": snapshot.get("largeNetRatio"),
        "signal": snapshot.get("signalLabel") or snapshot.get("signal"),
        "session_summary": {
            key: summary.get(key)
            for key in ("label", "priceChange", "totalNetChange", "largeNetChange", "reason")
        },
        "recent_change": {
            key: recent.get(key)
            for key in ("timeRange", "judgement", "priceChange", "totalNetChange", "largeNetChange")
        },
    }


def _load_order_flow_context(symbol: str) -> dict:
    endpoint = os.getenv("LONGBRIDGE_API_URL", "").strip().rstrip("/")
    if not endpoint:
        return {}
    timeout = float(os.getenv("LONGBRIDGE_API_TIMEOUT_SECONDS", "20"))
    try:
        snapshot_response = httpx.get(
            f"{endpoint}/api/realtime/capital/latest",
            params={"symbol": symbol},
            timeout=timeout,
        )
        snapshot_response.raise_for_status()
        series_response = httpx.get(
            f"{endpoint}/api/stocks/{quote(symbol, safe='')}/capital-series",
            timeout=timeout,
        )
        series_response.raise_for_status()
        return _build_order_flow_context(snapshot_response.json(), series_response.json())
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("stock AI order flow unavailable for %s: %s", symbol, type(exc).__name__)
        return {}


# ================================================================
# 数据加载
# ================================================================

def _load_kline(repo, symbol: str) -> pl.DataFrame:
    """读取该标的最近 N 根日 K(已含技术指标 / 信号)。

    repo: KlineRepository;走内存缓存,性能可控。
    """
    from datetime import date, timedelta

    end = date.today()
    start = end - timedelta(days=_KLINE_WINDOW * 2)  # 多取一些保证交易日够
    # 按资产类型分流: ETF/指数走独立 enriched 存储 (无财务数据, 提示词已有兜底)
    df = repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, start, end)
    if df.is_empty():
        return df
    return df.tail(_KLINE_WINDOW)


def _clean_rows(df: pl.DataFrame, keep_cols: list[str]) -> list[dict]:
    """把 DataFrame 转成 JSON 安全的 dict 列表(只保留关键列 + 清洗 NaN/Inf + date→字符串)。

    polars 的 date 列会变成 Python datetime.date,json.dumps 无法直接序列化,
    必须转成 ISO 字符串,否则 json.dumps 抛 TypeError 让整个流静默失败。
    """
    import datetime
    import math
    cols = [c for c in keep_cols if c in df.columns]
    sub = df.select(cols)
    rows = []
    for rec in sub.to_dicts():
        clean = {}
        for k, v in rec.items():
            if isinstance(v, float):
                clean[k] = None if not math.isfinite(v) else round(v, 4)
            elif isinstance(v, (datetime.date, datetime.datetime)):
                clean[k] = v.isoformat()
            else:
                clean[k] = v
        rows.append(clean)
    return rows


def _load_financials(data_dir: Path, symbol: str) -> dict[str, list[dict]]:
    """读取该标的核心财务指标 + 利润表(只取最有信息量的两张表)。

    财务面只需要关键指标(ROE / 增速 / 毛利率 等),不需要把 4 张表全塞进上下文
    (那是 financial_analyzer 的职责)。这里取轻量,留给技术面更多 token。
    """
    out: dict[str, list[dict]] = {}
    for table in ("metrics", "income"):
        df = get_financial_df(data_dir, table)
        if df.is_empty():
            out[table] = []
            continue
        df = df.filter(pl.col("symbol") == symbol)
        if df.is_empty():
            out[table] = []
            continue
        if "period_end" in df.columns:
            df = df.sort("period_end", descending=True).head(2)  # 只取最近 2 期
        import math
        rows = []
        for rec in df.to_dicts():
            clean = {}
            for k, v in rec.items():
                if k == "symbol":
                    continue
                if isinstance(v, float):
                    clean[k] = None if not math.isfinite(v) else v
                else:
                    clean[k] = v
            rows.append(clean)
        out[table] = rows
    return out


# ================================================================
# 系统提示词 —— 客观技术分析五维框架(与财务分析明确区分)
# 重要原则:只描述"指标/价位处于什么客观状态",不输出"应该怎么操作"。
# 本报告定位为客观行情分析,不提供任何买卖建议、仓位建议或交易指令。
# ================================================================

_SYSTEM_PROMPT = """你是一位拥有 15 年 A 股一线研究经验的技术分析分析师,擅长从 K 线、量价、关键价位与基本面交叉验证中客观解读个股的技术状态。你的任务是:基于提供的个股数据,产出一份**客观、中立、不包含任何买卖或操作建议**的技术分析报告。

## 核心红线(务必遵守)

- **绝对不输出**"买入/卖出/加仓/减仓/观望/轻仓/重仓/止损/止盈/建议买入区间/操作建议"等任何交易指令或倾向性措辞
- **绝对不**按"激进型/稳健型/保守型"给用户分别的操作建议
- 你的角色是**客观陈述**该股当前的技术状态、价位结构、量价特征与潜在风险,让读者自行判断
- 换成"一个中立财经记者能不能写出来"——能写就保留,不能写就删除

## 输出规范

用 **Markdown** 格式输出,严格遵循以下结构。不要输出任何 JSON 或代码块,直接输出 Markdown 正文。

### 1. 🎯 一句话定调(1-2 句)
用一句话概括该股当前的**技术状态**(如"近期高位放量滞涨,量能持续性存疑"/"价格在 60 日均线上方运行,均线呈多头排列")。结尾用【当前状态:企稳 / 反弹 / 震荡 / 调整 / 走弱】客观描述技术形态,**不评价好坏、不下操作结论**。

### 2. 📈 技术面分析(核心维度)
这是你的主战场,务必深入,只陈述客观事实:
- **趋势判断**:均线多头/空头排列、20/60 日均线方向、价格在均线之上/下
- **形态结构**:近期是否出现突破/破位/双底/双顶/旗形等关键形态
- **指标信号**:MACD 金叉/死叉/背离、KDJ 超买超卖、RSI 强弱、布林通道位置
- **量价配合**:放量上涨/缩量回调/量价背离/换手率异动
每条结论必须引用具体数值(如"MACD 在 6/12 出现金叉,DIF 0.32 上穿 DEA 0.18"),客观陈述,不下买卖定性。

### 3. 💰 关键价位(客观价位结构)
基于提供的关键价位数据,客观列出价位结构:
- **上方压力位**(逐档列出,标注来源):第一压力、第二压力
- **下方支撑位**(逐档列出,标注来源):第一支撑、第二支撑
- 用数据说话,引用提供的压力/支撑(成交密集区)/枢轴点数值
- **注意:只客观列出价位及其技术含义(如"此处为前期成交密集区"),不输出"建议买入区间""止损位""止盈位"等操作指令**

### 4. 🏭 基本面与财务面(辅助验证)
简要点评(2-4 句,不展开长篇):
- 盈利质量(ROE / 毛利率水平)、成长性(营收/利润增速)的客观水平
- 与技术面的**客观对照**:好公司 + 技术面走坏 → 客观陈述两者背离;差公司 + 技术面强势 → 客观提示炒作可能性
- 不下"逢低吸纳""规避"等结论

**当用户消息中标注了"该标的暂无财务数据"时**,本节请输出:
> 📌 财务面分析能力正在接入中。当前未同步该标的的财务报表,基本面维度暂无法评估。
> 技术面分析不依赖财务数据,以下结论依然有效;待财务数据同步后可补充本维度。

**绝对不要**在无数据时编造 ROE / 增速等数字。

### 5. 📰 消息面(价量异动推断)
**注意:本期无直接新闻数据输入。** 请基于 K 线的**异动信号**进行客观推断(如:
- 涨停/连板/炸板 → 可能存在利好或资金关注
- 放量暴跌 → 可能存在未公开扰动
- 突破放量 → 可能存在催化剂
明确标注"[推断]",告诉读者这是基于价量的客观推测,真实消息面数据待接入。若无明显异动,直说"近期价量平稳,无明显异动信号"。

### 6. ⚖️ 综合研判与风险提示
2-3 段,只做客观描述,不下操作结论:
- 客观描述该股当前所处的技术阶段(底部企稳 / 上升途中 / 高位震荡 / 下跌趋势)
- 客观评估当前价位的"空间不对称性"(距上方压力位与下方支撑位的距离),不评价好坏
- 客观列出后续值得关注的量价信号(如量能能否维持、某均线得失、是否放量突破压力位),**不附任何操作结论**

## 分析准则(务必遵守)

1. **技术面优先**:技术面和量价是主要分析对象,基本面是交叉验证手段,主次分明
2. **数据说话**:每个判断引用具体数值,严禁空泛套话("走势良好"必须改成"连续 3 日站稳 20 日均线且放量")
3. **客观中立**:看多就客观陈述多头特征,看空就客观陈述空头特征,不下"该买/该卖"结论;数据不支持时直言无法判断
4. **价位精确**:压力位/支撑位必须落到具体价格,基于提供的关键价位数据陈述
5. **不输出操作指令**:不写"买入/卖出/止损/加仓/减仓/仓位建议/操作建议"等任何交易指令;提示潜在风险但不下操作结论
6. **简明客观**:用读者能扫读的密度输出,总字数 1000-1800 字,重在客观信息密度

## 重要免责
报告末尾附一行:"> ⚠️ 本内容由 AI 基于公开行情与财务数据生成,仅客观陈述技术状态,不构成任何投资建议或买卖指令。交易有风险,入市需谨慎。"

现在请基于下方数据进行分析。"""


def _build_system_prompt(market: str) -> str:
    resolved = _resolve_market("", market)
    market_name, currency = _MARKET_CONTEXT[resolved]
    prompt = _SYSTEM_PROMPT.replace(
        "拥有 15 年 A 股一线研究经验",
        f"拥有 15 年{market_name}一线研究经验",
        1,
    )
    if resolved != "cn":
        prompt = prompt.replace(
            "- 涨停/连板/炸板 → 可能存在利好或资金关注\n",
            "- 成交额与波动同步显著放大 → 可能存在事件驱动或市场关注\n",
        )
    capital_section = """
### 3. 💧 资金状态(成交活跃度与量价结构)
基于提供的成交额统计,独立描述资金活跃状态:
- 对比当日成交额、5 日/20 日平均成交额、相对倍数及 60 日分位
- 结合涨跌幅、量比和换手率,归纳放量上涨、放量下跌、缩量上涨、缩量回调或巨量震荡
- 必须引用具体数值,说明成交活跃度是增强、平稳还是减弱
- **成交额仅表示交易活跃度,不等于资金净流入;缺少主动买卖方向数据时,不得表述为资金净流入、主力流入或主力流出**

"""
    prompt = prompt.replace("### 3. 💰 关键价位", capital_section + "### 4. 💰 关键价位")
    prompt = prompt.replace("### 4. 🏮 基本面与财务面", "### 5. 🏮 基本面与财务面")
    prompt = prompt.replace("### 5. 📰 消息面", "### 6. 📰 消息面")
    prompt = prompt.replace("### 6. ⚖️ 综合研判与风险提示", "### 7. ⚖️ 综合研判与风险提示")
    return (
        f"## 市场口径\n\n本次标的是{market_name},价格及成交额币种为{currency}。"
        "所有结论必须使用该市场的交易制度与行情语义。\n\n"
        + prompt
        + "\n\n## 大中小单资金语义边界\n"
        + "大单、中单、小单仅表示成交金额分档，不代表可确认的交易者身份。"
        + "不得直接断言为机构、主力或特定主体买卖；只能描述分档净流入/流出、资金分歧、"
        + "价格资金背离或疑似承接，并引用具体数值与数据时间。\n"
    )


# ================================================================
# 用户消息构建
# ================================================================

def _build_user_prompt(
    kline_tail: list[dict],
    fins: dict[str, list[dict]],
    levels: dict[str, list[dict]],
    close: float | None,
    symbol: str,
    focus: str,
    market: str = "cn",
    capital_metrics: dict[str, float] | None = None,
    order_flow_context: dict | None = None,
) -> str:
    """构建用户消息:标的 + 价位摘要 + 技术指标 JSON + 财务摘要 + 关注点。"""
    resolved_market = _resolve_market(symbol, market)
    market_name, currency = _MARKET_CONTEXT[resolved_market]
    parts: list[str] = [
        f"标的标准代码: {symbol}",
        f"市场: {market_name}",
        f"关键价位概览: {summarize_levels(levels, close)}",
        "",
        "以下是该标的最近日 K 数据(JSON,含 OHLCV 与已计算的技术指标。"
        f"最近 {_KLINE_WINDOW} 个交易日,升序):",
        "```json",
        json.dumps(kline_tail, ensure_ascii=False),
        "```",
    ]

    if capital_metrics:
        parts.extend([
            "",
            "以下是成交额资金活跃度统计(JSON,金额币种遵循上述市场口径):",
            "```json",
            json.dumps(capital_metrics, ensure_ascii=False),
            "```",
        ])

    if order_flow_context:
        parts.extend([
            "",
            "以下是盘中大中小单资金数据(JSON；按成交金额分档，不代表机构身份):",
            "```json",
            json.dumps(order_flow_context, ensure_ascii=False),
            "```",
        ])

    has_fin = any(fins.values())
    if has_fin:
        parts.extend([
            "",
            f"以下是该标的最新财务数据(JSON,核心指标 + 利润表,金额单位为{currency}):",
            "```json",
            json.dumps(fins, ensure_ascii=False),
            "```",
        ])
    else:
        parts.extend([
            "",
            "(该标的暂无财务数据:当前为 Free 模式或尚未同步财务报表。"
            "请按系统提示词第 5 节的说明,在基本面/财务面维度给出\"接入中\"的友好提示,不要编造数据。)",
        ])

    from app.services.ai_provider import sanitize_focus
    safe_focus = sanitize_focus(focus)
    if safe_focus:
        parts.extend(["", f"本次分析请特别关注: {safe_focus}"])
    return "\n".join(parts)


# ================================================================
# 关键列筛选(控制上下文体积)
# ================================================================

_KLINE_KEEP_COLS = [
    "date", "open", "high", "low", "close", "volume", "amount", "change_pct",
    "ma5", "ma10", "ma20", "ma60",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "rsi_6", "rsi_14", "rsi_24",
    "boll_upper", "boll_mid", "boll_lower",
    "atr_14", "vol_ratio_5d", "turnover_rate",
    "consecutive_limit_ups",
    # 信号类(布尔)——只挑对消息面推断有用的几个
    "signal_limit_up", "signal_broken_limit_up", "signal_macd_golden",
    "signal_macd_death", "signal_ma_golden_5_20", "signal_volume_surge",
    "signal_boll_breakout_upper", "signal_boll_breakout_lower",
]


# ================================================================
# 流式分析入口
# ================================================================

async def analyze_stock_stream(
    repo,
    data_dir: Path,
    symbol: str,
    focus: str = "",
    market: str | None = None,
    kline_df: pl.DataFrame | None = None,
    data_as_of: str | None = None,
    is_realtime: bool = False,
    quote_timestamp: int | float | None = None,
    order_flow_context: dict | None = None,
) -> AsyncIterator[str]:
    """流式个股分析:yield 出每个 NDJSON 事件。

    协议(与 financial_analyzer 一致,前端解析无差异):
      {"type":"meta","symbol","summary","levels"}  数据 + 价位摘要
      {"type":"delta","content":"..."}             逐 chunk 文本
      {"type":"error","message":"..."}
      {"type":"done"}
    """
    # 1. 加载 K 线
    df = kline_df if kline_df is not None else _load_kline(repo, symbol)
    if df.is_empty():
        yield json.dumps({
            "type": "error",
            "message": f"标的 {symbol} 暂无日 K 数据,请先同步",
        }, ensure_ascii=False)
        return

    # 2. 价位计算(基于 K 线)
    levels = compute_levels(df)
    close = float(df.tail(1)["close"][0]) if "close" in df.columns else None

    # 3. 财务(辅助)
    fins = _load_financials(data_dir, symbol)

    # 4. meta
    yield json.dumps({
        "type": "meta",
        "symbol": symbol,
        "summary": summarize_levels(levels, close),
        "levels": levels,
        "close": close,
        "data_as_of": data_as_of,
        "is_realtime": is_realtime,
        "quote_timestamp": quote_timestamp,
        "order_flow_available": bool(order_flow_context),
        "order_flow_as_of": (order_flow_context or {}).get("as_of"),
    }, ensure_ascii=False)

    # 5+6. 构建提示词 + 流式调用 LLM(整体 try-except,任何异常都 yield error,避免前端卡死)
    try:
        from app.services.ai_provider import stream_ai_text

        kline_tail = _clean_rows(df, _KLINE_KEEP_COLS)
        resolved_market = _resolve_market(symbol, market)
        user_prompt = _build_user_prompt(
            kline_tail,
            fins,
            levels,
            close,
            symbol,
            focus,
            resolved_market,
            _build_capital_metrics(df),
            order_flow_context,
        )
        async for delta in stream_ai_text(
            [
                {"role": "system", "content": _build_system_prompt(resolved_market)},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
            max_tokens=4500,
        ):
            yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)

    except Exception as e:  # noqa: BLE001
        logger.exception("AI stock analysis failed for %s: %s", symbol, e)
        yield json.dumps({"type": "error", "message": f"AI 分析失败: {e}"}, ensure_ascii=False)
        return

    yield json.dumps({"type": "done"}, ensure_ascii=False)
