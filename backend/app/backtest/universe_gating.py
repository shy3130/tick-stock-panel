"""上市天数门控与幸存者偏差警告拆分 (B6)。

原 provenance 的单一 ``survivorship_bias`` 字符串警告混合了两种性质不同的偏差:

- delisting_bias: 退市标的历史 K 线缺失, 本地数据源无法回补 —— 门控无法修复,
  只能显式告警 (回测收益可能被高估)。
- listing_age_bias: 次新股 (上市初期) 行为偏差 —— 可通过上市天数门控显式过滤。

门控语义 (fail-open 但显式计数):

- ``listing_date`` 为 null 或 ``listing_dates`` 中无对应 symbol 的行**保留**,
  计入 ``unknown_listing_date``; 绝不伪造上市日期或用代理值剔除。
- ``min_listed_days <= 0`` 视为门控关闭, 原样返回面板。
"""

from __future__ import annotations

import polars as pl

# join 用的临时列名带前缀, 避免与面板/上市日期表的自然列名冲突
_GATE_LISTING_COL = "__gating_listing_date"


def apply_listing_age_gate(
    panel: pl.DataFrame,
    listing_dates: pl.DataFrame,
    min_listed_days: int,
) -> tuple[pl.DataFrame, dict]:
    """按上市天数过滤面板行, 返回 (过滤后面板, 统计)。

    Parameters
    ----------
    panel : pl.DataFrame
        需含 ``symbol`` / ``date`` 列 (date 为 Date 或 Datetime, 统一按日截断)。
    listing_dates : pl.DataFrame
        需含 ``symbol`` / ``listing_date`` 列; 同一 symbol 重复时取首条。
    min_listed_days : int
        保留条件 ``(date - listing_date).days >= min_listed_days`` (边界恰好
        等于 N 天时保留); ``<= 0`` 时门控关闭, 原样返回。

    Returns
    -------
    tuple[pl.DataFrame, dict]
        统计字段: ``enabled`` / ``min_listed_days`` / ``rows_before`` /
        ``rows_after`` / ``rows_dropped`` / ``symbols_dropped`` /
        ``unknown_listing_date``; 其中 ``symbols_dropped`` 为整段被滤掉的
        symbol 数, ``unknown_listing_date`` 为上市日期缺失但被保留的行数。
    """
    missing_panel_cols = {"symbol", "date"} - set(panel.columns)
    if missing_panel_cols:
        raise ValueError(f"面板缺少必需列: {sorted(missing_panel_cols)}")
    missing_listing_cols = {"symbol", "listing_date"} - set(listing_dates.columns)
    if missing_listing_cols:
        raise ValueError(f"上市日期表缺少必需列: {sorted(missing_listing_cols)}")

    rows_before = panel.height
    if min_listed_days <= 0:
        stats = {
            "enabled": False,
            "min_listed_days": min_listed_days,
            "rows_before": rows_before,
            "rows_after": rows_before,
            "rows_dropped": 0,
            "symbols_dropped": 0,
            "unknown_listing_date": 0,
        }
        return panel, stats

    lookup = (
        listing_dates.select(
            pl.col("symbol"),
            pl.col("listing_date").cast(pl.Date).alias(_GATE_LISTING_COL),
        )
        .unique(subset=["symbol"], keep="first")
    )
    # left join 保持面板行序, 掩码逐行对齐
    joined = panel.join(lookup, on="symbol", how="left")

    # 上市日期缺失 (null) → 上市天数无法判定 → fail-open 保留并显式计数
    unknown_listing_date = int(joined.get_column(_GATE_LISTING_COL).is_null().sum())

    age_days = (
        pl.col("date").cast(pl.Date) - pl.col(_GATE_LISTING_COL)
    ).dt.total_days()
    keep = age_days.is_null() | (age_days >= min_listed_days)

    filtered = joined.filter(keep).drop(_GATE_LISTING_COL)

    stats = {
        "enabled": True,
        "min_listed_days": min_listed_days,
        "rows_before": rows_before,
        "rows_after": filtered.height,
        "rows_dropped": rows_before - filtered.height,
        "symbols_dropped": (
            panel.get_column("symbol").n_unique() - filtered.get_column("symbol").n_unique()
        ),
        "unknown_listing_date": unknown_listing_date,
    }
    return filtered, stats


def split_survivorship_warnings(
    universe_is_full_market: bool,
    listing_gate_active: bool,
    min_listed_days: int = 0,
) -> list[dict]:
    """把幸存者偏差警告拆分为结构化条目 (``{code, message, ...}``)。

    - 显式标的列表 (``universe_is_full_market=False``) → 空列表, 与现有
      provenance 语义一致: 显式列表无幸存者警告。
    - 全市场池: ``delisting_bias`` 永远输出 (门控无法修复退市缺失);
      未启用门控时输出 ``listing_age_bias`` (次新股行为偏差), 启用后改为
      输出 ``listing_age_gated`` 信息性条目并记录 ``min_listed_days``。
    """
    if not universe_is_full_market:
        return []

    warnings: list[dict] = [
        {
            "code": "delisting_bias",
            "message": (
                "退市标的历史 K 线缺失，当前本地源无法回补，回测收益可能被高估"
            ),
        }
    ]
    if listing_gate_active:
        warnings.append(
            {
                "code": "listing_age_gated",
                "message": (
                    f"已启用上市天数门控（min_listed_days={min_listed_days}），"
                    f"上市不足 {min_listed_days} 天的样本被显式过滤"
                ),
                "min_listed_days": int(min_listed_days),
            }
        )
    else:
        warnings.append(
            {
                "code": "listing_age_bias",
                "message": "未启用上市天数门控，次新股（上市初期）行为偏差样本未被过滤",
            }
        )
    return warnings
