"""财务因子: 基于本地财务快照的点时 (point-in-time) 无未来函数接入。

数据契约:
- 输入为 data/financials/metrics/part.parquet, 每行一份报告期指标;
- ``announce_date`` 是公告日。因子只在 **严格晚于公告日的交易日** 才有值
  (公告多在盘后发布, 保守取 T+1 生效), 此前保持 null;
- 财报历史按 (symbol, period_end) 累积 (见 services/financial_sync.py),
  同一期以最新公告为准;
- 无财务数据的标的/日期一律为 null, 绝不填 0 (填 0 会污染截面排名,
  例如资产负债率 0 会被当成最优杠杆)。下游 IC/分层/评分对 null 自动剔除。

性能:
- 财务表约数千行, join_asof 按 symbol 分组回填, 对百万行面板的代价是
  毫秒级; 矩阵路径每个因子只物化一张 float32 TxN 矩阵 (T~900, N~5500
  约 20MB), 且仅在策略/挖掘请求该因子时才构建。
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import MappingProxyType
from typing import Any

import numpy as np
import polars as pl

logger = logging.getLogger(__name__)

# 财务因子名 -> (metrics 表列名, 是否需要除以收盘价)
# pb_latest 单列声明为 bps 倒数口径: 因子值 = close / bps。
FUNDAMENTAL_FACTORS: dict[str, dict[str, Any]] = {
    "pb_latest": {"column": "bps", "price_ratio": True},
    "roe_latest": {"column": "roe", "price_ratio": False},
    "gross_margin_latest": {"column": "gross_margin", "price_ratio": False},
    "net_margin_latest": {"column": "net_margin", "price_ratio": False},
    "revenue_yoy_latest": {"column": "revenue_yoy", "price_ratio": False},
    "net_income_yoy_latest": {"column": "net_income_yoy", "price_ratio": False},
    "debt_ratio_latest": {"column": "debt_to_asset_ratio", "price_ratio": False},
}

FUNDAMENTAL_FACTOR_NAMES = frozenset(FUNDAMENTAL_FACTORS)


def load_fundamental_snapshot(data_dir: Path | None) -> pl.DataFrame | None:
    """读取财务指标快照; 文件缺失或无有效行时返回 None。

    返回列: symbol, _announce (Date), 以及各因子对应的 metrics 列。
    """
    if data_dir is None:
        return None
    path = data_dir / "financials" / "metrics" / "part.parquet"
    if not path.exists():
        return None
    try:
        frame = pl.read_parquet(path)
    except Exception as exc:
        logger.warning("读取财务指标快照失败: %s", exc)
        return None
    needed = {"symbol", "announce_date"} | {
        spec["column"] for spec in FUNDAMENTAL_FACTORS.values()
    }
    if not needed.issubset(frame.columns):
        logger.warning("财务指标快照缺少列: %s", sorted(needed - set(frame.columns)))
        return None
    snapshot = (
        frame.select(sorted(needed))
        .filter(
            pl.col("symbol").is_not_null()
            & pl.col("announce_date").is_not_null()
        )
        .with_columns(
            pl.col("announce_date").cast(pl.Utf8).str.slice(0, 10).str.to_date().alias("_announce")
        )
        .sort(["symbol", "_announce"])
    )
    if snapshot.is_empty():
        return None
    return snapshot


def attach_fundamental_factors(
    panel: pl.DataFrame,
    snapshot: pl.DataFrame | None,
    names: Any,
) -> pl.DataFrame:
    """把财务因子列按公告日门控地并入日频面板。

    - snapshot 为 None (本地无财务数据): 产出全 null 列, 保持面板形状,
      由上层决定是否报"无财务数据"错误;
    - 面板必须已按 (symbol, date) 排序 (存储与挖掘路径均满足)。
    """
    requested = [str(name) for name in names if str(name) in FUNDAMENTAL_FACTOR_NAMES]
    missing_columns = [name for name in requested if name not in panel.columns]
    if not missing_columns:
        return panel

    if snapshot is None:
        return panel.with_columns([
            pl.lit(None, dtype=pl.Float64).alias(name)
            for name in missing_columns
        ])

    columns = sorted(
        {FUNDAMENTAL_FACTORS[name]["column"] for name in missing_columns}
    )
    # asof 键取生效日 (公告日次日) 而非公告日: 直接用公告日回看会在换报告期的
    # 公告当日取到「尚未生效」的新一期并被门控置 null, 打断上一期的前向填充,
    # 与矩阵路径 (searchsorted side="right") 不一致。
    right = (
        snapshot.select(["symbol", "_announce", *columns])
        .with_columns(pl.col("_announce").dt.offset_by("1d").alias("_effective"))
        .sort(["symbol", "_effective"])
    )
    joined = panel.join_asof(
        right,
        left_on="date",
        right_on="_effective",
        by="symbol",
        strategy="backward",
        check_sortedness=False,  # 双侧均已按 (symbol, key) 排序, 免除逐组检查开销
    )
    announced = pl.col("_announce").is_not_null() & (pl.col("date") > pl.col("_announce"))
    expressions = []
    for name in missing_columns:
        spec = FUNDAMENTAL_FACTORS[name]
        source = pl.col(spec["column"])
        if spec["price_ratio"]:
            value = (
                pl.when(source > 0)
                .then(pl.col("close") / source)
                .otherwise(None)
            )
        else:
            value = source
        expressions.append(
            pl.when(announced).then(value).otherwise(None).alias(name)
        )
    return joined.with_columns(expressions).drop("_effective")


def build_fundamental_matrices(
    market: Any,
    snapshot: pl.DataFrame | None,
    names: Any,
) -> dict[str, np.ndarray]:
    """为 MarketDataMatrix 构建财务因子 TxN float32 字段。

    与 attach_fundamental_factors 同一口径: 公告日次一交易日起前向填充,
    无数据为 NaN。pb 类因子在矩阵侧用 close / bps 现算。
    """
    requested = [str(name) for name in names if str(name) in FUNDAMENTAL_FACTOR_NAMES]
    if not requested:
        return {}

    shape = market.shape
    result: dict[str, np.ndarray] = {}
    if snapshot is None:
        for name in requested:
            result[name] = np.full(shape, np.nan, dtype=np.float32)
        return result

    asset_index = {symbol: index for index, symbol in enumerate(market.symbols)}
    labels = market.timestamp_labels
    label_dates = np.array([label[:10] for label in labels], dtype="datetime64[D]")

    raw_columns = {
        FUNDAMENTAL_FACTORS[name]["column"]: np.full(shape, np.nan, dtype=np.float32)
        for name in requested
    }
    announce_text = snapshot["announce_date"].str.slice(0, 10)
    for row_index, symbol in enumerate(snapshot["symbol"].to_list()):
        column_index = asset_index.get(symbol)
        if column_index is None:
            continue
        announce = announce_text[row_index]
        if announce is None:
            continue
        # 公告日之后 (严格大于) 的首个时间行索引
        start = int(np.searchsorted(label_dates, np.datetime64(announce, "D"), side="right"))
        if start >= shape[0]:
            continue
        for column, target in raw_columns.items():
            value = snapshot[column][row_index]
            numeric = float("nan") if value is None else float(value)
            # 新一期该指标为空时必须覆盖旧值为 NaN: 跳过写入会让同一行混用两期
            # 报告 (bps 取新期、roe 停在上一期), 与 polars 侧 join_asof 只认
            # 最新一期整行的口径不一致。
            target[start:, column_index] = numeric if np.isfinite(numeric) else np.nan

    for name in requested:
        spec = FUNDAMENTAL_FACTORS[name]
        source = raw_columns[spec["column"]]
        if spec["price_ratio"]:
            with np.errstate(divide="ignore", invalid="ignore"):
                matrix = (market.close / source).astype(np.float32)
            matrix[~(source > 0)] = np.nan
            matrix[np.isinf(matrix)] = np.nan
        else:
            matrix = source
        result[name] = matrix
    return result


def attach_matrix_fundamental_fields(market: Any, data_dir: Path | None, names: Any, *, evidence=None) -> Any:
    """把财务因子作为 matrix fields 附加到 (frozen) MarketDataMatrix 副本。"""
    import dataclasses

    requested = [str(name) for name in names if str(name) in FUNDAMENTAL_FACTOR_NAMES]
    if not requested:
        return market
    snapshot = load_fundamental_snapshot(data_dir)
    if evidence is not None and snapshot is not None:
        evidence.record_frame("financial_announcements", snapshot)
    extra = build_fundamental_matrices(market, snapshot, requested)
    if not extra:
        return market
    merged = {**dict(market.fields), **extra}
    for array in extra.values():
        array.flags.writeable = False
    return dataclasses.replace(market, fields=MappingProxyType(merged))
