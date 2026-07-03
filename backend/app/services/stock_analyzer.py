"""AI 个股分析服务 — 技术面 / 基本面 / 财务面 / 消息面 四维综合分析。

职责:
  组合一只股票的 K 线(含已算好的技术指标)+ 财务表 + 关键价位 →
  拼装"实战派交易员"级系统提示词 → 流式调用 LLM → 逐 chunk 吐给前端。

与 financial_analyzer.py 的区别(刻意区分,非复用):
  - 角色:A 股实战派交易员 / 技术分析师(非 CFA 财务分析师)
  - 数据源:K 线 + 技术指标为主,财务表为辅(财务分析以财务表为主)
  - 输出框架:技术面→基本面→财务面→消息面(四维),落点是买卖区间与操作建议
    (财务分析的落点是财务质量评级)

不知道: HTTP、前端、配置持久化。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import AsyncIterator

import polars as pl

from app.indicators.levels import compute_levels, summarize_levels
from app.services.financial_sync import get_financial_df

logger = logging.getLogger(__name__)

# 注入最近多少根日 K(技术面分析样本)
_KLINE_WINDOW = 90
# 注入财务表的最近期数
_MAX_PERIODS = 4


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
    df = repo.get_daily(symbol, start, end)
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
# 系统提示词 —— 实战派交易员四维框架(与财务分析明确区分)
# ================================================================

_SYSTEM_PROMPT = """你是一位拥有 15 年 A 股一线实战经验的资深交易员兼技术分析师,擅长从 K 线、量价、关键价位与基本面交叉验证中把握买卖时机。你的任务是:基于提供的个股数据,产出一份**实战、可直接指导交易决策**的综合分析报告。

## 输出规范

用 **Markdown** 格式输出,严格遵循以下结构。不要输出任何 JSON 或代码块,直接输出 Markdown 正文。

### 1. 🎯 一句话定调(1-2 句)
用一句话概括该股当前的**技术状态与交易属性**(如"高位放量滞涨,需警惕回调"/"底部筹码集中,放量突破在即")。结尾用【操作建议:观望 / 轻仓试探 / 逢低吸纳 / 持有 / 减仓 / 规避】给出明确倾向。

### 2. 📈 技术面分析(核心维度)
这是你的主战场,务必深入:
- **趋势判断**:均线多头/空头排列、20/60 日均线方向、价格在均线之上/下
- **形态结构**:近期是否有突破/破位/双底/双顶/旗形等关键形态
- **指标信号**:MACD 金叉/死叉/背离、KDJ 超买超卖、RSI 强弱、布林通道位置
- **量价配合**:放量上涨/缩量回调/量价背离/换手率异动
每条结论必须引用具体数值(如"MACD 在 6/12 出现金叉,DIF 0.32 上穿 DEA 0.18")。

### 3. 💰 关键价位(买卖区间)
基于提供的关键价位数据,明确指出:
- **上方压力位**(逐档列出,标注强度):第一压力、第二压力
- **下方支撑位**(逐档列出,标注强度):第一支撑、第二支撑
- 给出**建议买入区间**与**止损位**(基于支撑位)
用数据说话,引用提供的压力/支撑(成交密集区)/枢轴点数值。

### 4. 🏭 基本面与财务面(辅助验证)
简要点评(2-4 句,不展开长篇):
- 盈利质量(ROE / 毛利率水平)、成长性(营收/利润增速)
- 与技术面的**交叉验证**:好公司 + 技术面走坏 → 仍需谨慎;差公司 + 技术面强势 → 警惕炒作风险

**当用户消息中标注了"该标的暂无财务数据"时**,本节请输出:
> 📌 财务面分析能力正在接入中。当前版本(Free)未同步该标的的财务报表,基本面维度暂无法评估。
> 技术面分析不依赖财务数据,以下结论依然有效;升级套餐或等待财务数据同步后可补充本维度。

**绝对不要**在无数据时编造 ROE / 增速等数字。

### 5. 📰 消息面(价量异动推断)
**注意:本期无直接新闻数据输入。** 请基于 K 线的**异动信号**进行推断(如:
- 涨停/连板/炸板 → 可能有利好或资金炒作
- 放量暴跌 → 可能有未公开利空
- 突破放量 → 可能有催化剂
明确标注"[推断]",告诉用户这是基于价量的推测,真实消息面数据待接入。若无明显异动,直说"近期价量平稳,无明显消息面信号"。

### 6. ⚖️ 综合研判与操作建议
2-3 段:
- 该股当前处于(底部启动 / 上升途中 / 高位震荡 / 下跌趋势 / 底部企稳)哪个阶段
- 风险收益比评估(距支撑位的空间 vs 距压力位的空间)
- **明确操作建议**:激进型 / 稳健型 / 保守型 分别怎么应对
- **需要重点盯的信号**(如跌破 X 支撑止损、站上 Y 压力加仓)

## 分析准则(务必遵守)

1. **技术面优先**:作为交易员,技术面和量价是主要依据,基本面是验证手段,主次分明
2. **数据说话**:每个判断引用具体数值,严禁空泛套话("走势良好"必须改成"连续 3 日站稳 20 日均线且放量")
3. **诚实中立**:看多就写多,看空就写空,不要模棱两可骑墙;数据不支持时直言无法判断
4. **价位精确**:买卖区间必须落到具体价格,基于提供的关键价位数据推演
5. **风险前置**:任何买入建议都要配止损位;提示潜在风险不回避
6. **简明实战**:用交易员能扫读的密度输出,总字数 1000-1800 字,重在可执行

## 重要免责
报告末尾附一行:"> ⚠️ 本报告由 AI 基于公开行情与财务数据生成,仅供参考,不构成任何投资建议。交易有风险,入市需谨慎。"

现在请基于下方数据进行分析。"""


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
) -> str:
    """构建用户消息:标的 + 价位摘要 + 技术指标 JSON + 财务摘要 + 关注点。"""
    parts: list[str] = [
        f"标的标准代码: {symbol}",
        f"关键价位概览: {summarize_levels(levels, close)}",
        "",
        "以下是该标的最近日 K 数据(JSON,含 OHLCV 与已计算的技术指标。"
        f"最近 {_KLINE_WINDOW} 个交易日,升序):",
        "```json",
        json.dumps(kline_tail, ensure_ascii=False),
        "```",
    ]

    has_fin = any(fins.values())
    if has_fin:
        parts.extend([
            "",
            "以下是该标的最新财务数据(JSON,核心指标 + 利润表,金额单位为元):",
            "```json",
            json.dumps(fins, ensure_ascii=False),
            "```",
        ])
    else:
        parts.extend([
            "",
            "(该标的暂无财务数据:当前为 Free 模式或尚未同步财务报表。"
            "请按系统提示词第 4 节的说明,在基本面/财务面维度给出\"接入中\"的友好提示,不要编造数据。)",
        ])

    if focus.strip():
        parts.extend(["", f"本次分析请特别关注: {focus.strip()}"])
    return "\n".join(parts)


# ================================================================
# 关键列筛选(控制上下文体积)
# ================================================================

_KLINE_KEEP_COLS = [
    "date", "open", "high", "low", "close", "volume", "change_pct",
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
) -> AsyncIterator[str]:
    """流式个股分析:yield 出每个 NDJSON 事件。

    协议(与 financial_analyzer 一致,前端解析无差异):
      {"type":"meta","symbol","summary","levels"}  数据 + 价位摘要
      {"type":"delta","content":"..."}             逐 chunk 文本
      {"type":"error","message":"..."}
      {"type":"done"}
    """
    # 1. 加载 K 线
    df = _load_kline(repo, symbol)
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
    }, ensure_ascii=False)

    # 5+6. 构建提示词 + 流式调用 LLM(整体 try-except,任何异常都 yield error,避免前端卡死)
    try:
        from app.services.ai_provider import stream_ai_text

        kline_tail = _clean_rows(df, _KLINE_KEEP_COLS)
        from app.services.skill_context import load_skill_context

        skill_context = load_skill_context("stock_analysis")
        user_prompt = _build_user_prompt(kline_tail, fins, levels, close, symbol, focus)
        if skill_context:
            user_prompt = skill_context + "\n\n---\n\n" + user_prompt
        async for delta in stream_ai_text(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
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
