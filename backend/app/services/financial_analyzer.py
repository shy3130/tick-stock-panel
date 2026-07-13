"""AI 财务分析服务 — 读取个股财务数据 → 构建专业提示词 → 流式调用 LLM。

职责: 拉取单只标的的 4 张财务表 → 转成紧凑 JSON → 拼装 CFA 分析师级系统提示词
       → 流式调用 OpenAI 兼容 API → 逐 chunk 吐给前端。

不知道: HTTP、前端、配置持久化。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import AsyncIterator

import polars as pl

from app.services.financial_sync import get_financial_df

logger = logging.getLogger(__name__)

# 最多注入的报告期数(最新 N 期),避免上下文爆炸 / token 浪费
_MAX_PERIODS = 4


def _load_stock_financials(data_dir: Path, symbol: str) -> dict[str, list[dict]]:
    """读取该标的的 4 张财务表,返回 {table: [records...]}(按 period_end 降序,截取最新 N 期)。

    数值统一做 NaN/Inf → null 清洗,保证 JSON 序列化不报错。
    """
    result: dict[str, list[dict]] = {}
    for table in ("metrics", "income", "balance_sheet", "cash_flow"):
        df = get_financial_df(data_dir, table)
        if df.is_empty():
            result[table] = []
            continue
        df = df.filter(pl.col("symbol") == symbol)
        if df.is_empty():
            result[table] = []
            continue
        # 按 period_end 降序,截取最新 N 期
        if "period_end" in df.columns:
            df = df.sort("period_end", descending=True).head(_MAX_PERIODS)
        # 清洗 NaN/Inf,转成 JSON 安全的 dict 列表
        rows = []
        for rec in df.to_dicts():
            clean = {}
            for k, v in rec.items():
                if k == "symbol":
                    continue  # 不需要重复回传 symbol
                if isinstance(v, float):
                    import math
                    clean[k] = None if not math.isfinite(v) else v
                else:
                    clean[k] = v
            rows.append(clean)
        result[table] = rows
    return result


def _summarize(fins: dict[str, list[dict]]) -> str:
    """生成一行业务摘要,便于 LLM 快速把握数据全貌(行数/期数)。"""
    parts = []
    for table in ("metrics", "income", "balance_sheet", "cash_flow"):
        rows = fins.get(table, [])
        if rows:
            periods = [r.get("period_end") for r in rows if r.get("period_end")]
            parts.append(f"{table}: {len(rows)}期 ({', '.join(str(p) for p in periods[:3])})")
        else:
            parts.append(f"{table}: 无数据")
    return " · ".join(parts)


# ================================================================
# 系统提示词 —— CFA 分析师级,九维分析框架
# ================================================================

_SYSTEM_PROMPT = """你是一位拥有 15 年 A 股投研经验的资深财务分析师(CFA + CPA),以机构研究的严谨标准开展工作。你的任务是:基于提供的上市公司财务数据,产出一份**客观、中立、不包含任何投资建议或买卖指令**的财务分析报告。

## 核心红线(务必遵守)

- **绝对不输出**"投资参考""建议买入/卖出""值得投资""逢低吸纳"等任何投资建议或买卖指令
- 你的角色是**客观陈述**该公司的财务特征画像、亮点、风险,供读者自行判断
- 财务质量分类(如高质量蓝筹/高风险等)可作为客观特征描述,但不与"该不该投资"挂钩

## 输出规范

用 **Markdown** 格式输出,严格遵循以下结构。不要输出任何 JSON 或代码块,直接输出 Markdown 正文。

### 1. 📌 核心摘要(1-2 句)
用一句话概括该公司的财务画像:盈利质量、成长动能、财务健康度的最关键判断。结尾用【综合评级:★★★☆☆】给出 1-5 星评级(仅反映财务质量客观水平,不构成投资建议)。

### 2. ✅ 亮点(2-3 条)
列出最值得关注的**积极信号**,每条用加粗短语领起,配数据支撑。例如盈利高增、ROE 持续提升、现金流充沛等。

### 3. ⚠️ 风险提示(2-3 条)
客观指出**潜在风险或值得警惕的信号**,例如应收激增、存货堆积、经营现金流与净利润背离、债务攀升等。宁可保守,不要回避。

### 4. 📊 分项诊断
用**表格**呈现各维度的诊断结论,列为「维度 / 关键指标 / 判断」。维度包括:
- **盈利能力**:ROE / ROA / 毛利率 / 净利率
- **成长性**:营收同比 / 净利润同比
- **偿债能力**:资产负债率 / 流动比率(用资产/负债估算)
- **现金流**:经营现金流净额 / 与净利润的匹配度
- **营运效率**:存货周转率等(有数据时)

每个判断给「优秀 / 良好 / 一般 / 偏弱 / 警惕」之一,并一句话说明依据。

### 5. 🎯 综合评估与财务特征画像
2-3 段总结:该公司当前的财务状态(优秀/稳健/承压/恶化)、核心驱动力、未来需重点跟踪的指标。**结尾客观描述该公司的财务特征画像**(如"高 ROE + 低负债 + 现金流稳定 + 营收增速放缓"),作为财务层面的客观归纳,**不下"该不该投资""属于哪类投资标的"的结论**。

## 分析准则(务必遵守)

1. **数据说话**:每个判断必须引用具体数值(如"营收同比 +28.5%"),严禁空泛套话
2. **纵向对比**:利用多期数据看趋势(改善/恶化),而非只看单期
3. **交叉验证**:经营现金流 vs 净利润(是否造血)、毛利率 vs 费用率(盈利结构)、负债 vs 资产(杠杆)
4. **行业常识**:对照 A 股常识判断水平(如 ROE>15% 优秀,资产负债率>70% 偏高,毛利率<20% 偏低)
5. **客观中立**:数据不支持时直言"数据不足,无法判断",绝不编造或过度演绎
6. **不输出投资建议**:不写"建议买入/卖出/投资参考/值得投资"等;财务质量评级仅反映客观财务水平,不与投资行为挂钩
7. **简明有力**:避免冗长,用专业读者能扫读的密度输出,总字数 800-1500 字

## 重要免责
报告末尾附一行:"> ⚠️ 本内容由 AI 基于公开财务数据生成,仅客观陈述财务特征,不构成任何投资建议或买卖指令。"

现在请基于下方数据进行分析。"""


def _build_user_prompt(fins: dict[str, list[dict]], symbol: str, focus: str) -> str:
    """构建用户消息:标的代码 + 数据 JSON + 可选关注点。"""
    data_json = json.dumps(fins, ensure_ascii=False, indent=2)
    lines = [
        f"标的标准代码: {symbol}",
        f"数据概览: {_summarize(fins)}",
        "",
        "以下是该标的最新财务数据(JSON 格式,金额单位为元,比率类指标为百分点):",
        "```json",
        data_json,
        "```",
    ]
    from app.services.ai_provider import sanitize_focus
    safe_focus = sanitize_focus(focus)
    if safe_focus:
        lines.extend([
            "",
            f"本次分析请特别关注: {safe_focus}",
        ])
    return "\n".join(lines)


async def analyze_financials_stream(
    data_dir: Path,
    symbol: str,
    focus: str = "",
) -> AsyncIterator[str]:
    """流式分析:yield 出每个文本 chunk。

    - 启动时先 yield 一条 {"type":"meta",...} 让前端显示数据摘要
    - 之后逐 chunk yield {"type":"delta","content":"..."}
    - 出错时 yield {"type":"error","message":"..."}
    - 结束 yield {"type":"done"}
    """
    # 1. 加载数据
    fins = _load_stock_financials(data_dir, symbol)
    total_rows = sum(len(v) for v in fins.values())
    if total_rows == 0:
        yield json.dumps({"type": "error", "message": f"标的 {symbol} 暂无任何财务数据,请先同步财务表"}, ensure_ascii=False)
        return

    # 2. meta
    yield json.dumps({
        "type": "meta",
        "symbol": symbol,
        "summary": _summarize(fins),
        "periods": total_rows,
    }, ensure_ascii=False)

    # 3. 调用 LLM 流式
    try:
        from app.services.ai_provider import stream_ai_text

        user_prompt = _build_user_prompt(fins, symbol, focus)
        async for delta in stream_ai_text(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=4000,
        ):
            yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)

    except Exception as e:  # noqa: BLE001
        logger.exception("AI financial analysis failed for %s: %s", symbol, e)
        yield json.dumps({"type": "error", "message": f"AI 分析失败: {e}"}, ensure_ascii=False)
        return

    yield json.dumps({"type": "done"}, ensure_ascii=False)
