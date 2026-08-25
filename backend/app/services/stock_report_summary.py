"""F13 程序化结构化摘要 —— 只做程序组装，不做第二次 LLM 调用。

数据来源全部是 analyze_stock_stream 已有产物：关键位 levels、现价 close、
preflight 警告与 summarize_levels 文本。组装失败返回 None，由调用方省略
summary 事件，不阻塞 markdown 输出。
"""
from __future__ import annotations

import math
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict

from app.indicators.levels import LEVEL_TYPES, summarize_levels

# key_levels 总量封顶（compute_levels 每组已限量，这里只控制摘要体积）
MAX_KEY_LEVELS = 8


class StockReportSummary(BaseModel):
    """个股报告结构化摘要（仅结构事实）。

    extra="forbid"：多出的任何键（如 action / direction / buy / sell /
    target*）都在校验阶段被拒绝，模型层杜绝方向性字段。
    """

    model_config = ConfigDict(extra="forbid")

    trend: str
    key_levels: list[str]
    data_gaps: list[str]


def _iter_level_points(levels: dict) -> Iterator[tuple[float, str]]:
    """遍历 (价位, 描述文本)；仅接受有限数值，其余跳过。"""
    for key, label in LEVEL_TYPES.items():
        for point in levels.get(key) or []:
            if not isinstance(point, dict):
                continue
            value = point.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            value = float(value)
            if not math.isfinite(value):
                continue
            point_label = str(point.get("label") or "").strip()
            text = f"{label}·{point_label}={value:.2f}" if point_label else f"{label}={value:.2f}"
            yield value, text


def build_report_summary(
    levels: Any,
    close: float | None,
    warnings: Any = None,
) -> StockReportSummary | None:
    """纯函数组装 StockReportSummary；任何异常返回 None（省略，不阻塞 markdown）。"""
    try:
        if not isinstance(levels, dict):
            levels = {}
        data_gaps = [str(w) for w in (warnings or [])]
        points = list(_iter_level_points(levels))

        close_value: float | None = None
        if isinstance(close, (int, float)) and not isinstance(close, bool) and math.isfinite(float(close)):
            close_value = float(close)

        if close_value is None or not points:
            # 无现价或无有效关键位：退化为 summarize_levels 文本（同为程序产物）；
            # 脏点位使 summarize_levels 抛错时再退化为纯现价描述。
            try:
                trend_text = summarize_levels(levels, close_value)
            except Exception:  # noqa: BLE001
                trend_text = f"现价 {close_value:.2f}" if close_value is not None else "无价位数据"
            return StockReportSummary(
                trend=trend_text,
                key_levels=[text for _, text in points[:MAX_KEY_LEVELS]],
                data_gaps=data_gaps,
            )
        nearest = sorted(points, key=lambda item: abs(item[0] - close_value))[:MAX_KEY_LEVELS]
        above = sum(1 for value, _ in points if value > close_value)
        below = sum(1 for value, _ in points if value < close_value)
        if above > below:
            position = "偏下方，上方关键位更多"
        elif below > above:
            position = "偏上方，下方关键位更多"
        else:
            position = "中部，上下关键位数量相当"
        trend = (
            f"现价 {close_value:.2f} 上方关键位 {above} 个、下方 {below} 个，"
            f"价格处于关键位区间{position}"
        )
        return StockReportSummary(
            trend=trend,
            key_levels=[text for _, text in nearest],
            data_gaps=data_gaps,
        )
    except Exception:  # noqa: BLE001 —— 摘要失败只省略，不阻塞主流程
        return None
