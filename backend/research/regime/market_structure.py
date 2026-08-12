"""无未来泄漏的 A 股结构牛市/结构熊市标签。

标签描述的是 2024-09-24 之后大级别行情内部的横截面结构，不声称重新定义宏观
牛熊周期。所有用于交易日 ``t`` 的状态特征都来自 ``t-1`` 或更早的数据。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import polars as pl

STRUCTURAL_BULL = "structural_bull"
STRUCTURAL_BEAR = "structural_bear"
WARMUP = "warmup"


@dataclass(frozen=True)
class MarketStructureConfig:
    """预注册的 v1 市场结构规则；阈值不得用测试期策略收益反向选择。"""

    short_ma: int = 20
    long_ma: int = 60
    return_window: int = 20
    advance_window: int = 10
    min_valid_assets: int = 1_000
    bull_breadth_short: float = 0.55
    bull_breadth_long: float = 0.50
    bull_return: float = 0.00
    bear_breadth_short: float = 0.45
    bear_breadth_long: float = 0.40
    bear_return: float = -0.03
    confirm_days: int = 2
    signal_lag_days: int = 1

    def validate(self) -> None:
        if self.short_ma <= 1 or self.long_ma <= self.short_ma:
            raise ValueError("long_ma must be greater than short_ma > 1")
        if self.return_window <= 1 or self.advance_window <= 1:
            raise ValueError("rolling windows must be greater than one")
        if self.min_valid_assets <= 0:
            raise ValueError("min_valid_assets must be positive")
        if self.confirm_days <= 0:
            raise ValueError("confirm_days must be positive")
        if self.signal_lag_days != 1:
            raise ValueError("market structure v1 requires exactly one-day signal lag")
        ratios = (
            self.bull_breadth_short,
            self.bull_breadth_long,
            self.bear_breadth_short,
            self.bear_breadth_long,
        )
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in ratios):
            raise ValueError("breadth thresholds must be finite ratios in [0, 1]")
        if self.bull_breadth_short < self.bear_breadth_short:
            raise ValueError("short breadth bull threshold must exceed bear threshold")
        if self.bull_breadth_long < self.bear_breadth_long:
            raise ValueError("long breadth bull threshold must exceed bear threshold")
        if self.bull_return < self.bear_return:
            raise ValueError("bull return threshold must exceed bear threshold")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def protocol_hash(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _rolling_compounded_return(returns: np.ndarray, window: int) -> np.ndarray:
    result = np.full(len(returns), np.nan, dtype=np.float64)
    gross = np.where(np.isfinite(returns), 1.0 + returns, np.nan)
    for index in range(window - 1, len(returns)):
        sample = gross[index - window + 1 : index + 1]
        if np.isfinite(sample).all() and (sample > 0).all():
            result[index] = float(np.prod(sample) - 1.0)
    return result


def _rolling_nanmean(values: np.ndarray, window: int) -> np.ndarray:
    result = np.full(len(values), np.nan, dtype=np.float64)
    for index in range(window - 1, len(values)):
        sample = values[index - window + 1 : index + 1]
        if np.isfinite(sample).all():
            result[index] = float(sample.mean())
    return result


def build_market_structure_features(
    daily: pl.DataFrame | pl.LazyFrame,
    config: MarketStructureConfig | None = None,
) -> pl.DataFrame:
    """Build full-market breadth and equal-weight return features.

    ``ew_return_1d`` is the mean of per-symbol adjusted close returns, clipped
    to the A-share daily move envelope.  It deliberately does not average raw
    stock price levels.
    """
    config = config or MarketStructureConfig()
    config.validate()
    lazy = daily.lazy() if isinstance(daily, pl.DataFrame) else daily
    schema = lazy.collect_schema()
    required = {"symbol", "date", "close"}
    if not required <= set(schema.names()):
        raise ValueError(f"daily data missing columns: {sorted(required - set(schema.names()))}")

    per_symbol = (
        lazy.select(
            pl.col("symbol").cast(pl.Utf8),
            pl.col("date").cast(pl.Date, strict=False),
            pl.col("close").cast(pl.Float64, strict=False),
        )
        .filter(
            pl.col("symbol").is_not_null()
            & pl.col("date").is_not_null()
            & pl.col("close").is_finite()
            & (pl.col("close") > 0)
        )
        .unique(subset=["symbol", "date"], keep="last")
        .sort(["symbol", "date"])
        .with_columns(
            pl.col("close")
            .rolling_mean(config.short_ma, min_samples=config.short_ma)
            .over("symbol")
            .alias("_ma_short"),
            pl.col("close")
            .rolling_mean(config.long_ma, min_samples=config.long_ma)
            .over("symbol")
            .alias("_ma_long"),
            (
                pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
            )
            .clip(-0.30, 0.30)
            .alias("_return_1d"),
        )
    )
    aggregate = (
        per_symbol.group_by("date")
        .agg(
            pl.len().alias("asset_count"),
            pl.col("_ma_short").is_not_null().sum().alias("breadth_short_valid"),
            pl.col("_ma_long").is_not_null().sum().alias("breadth_long_valid"),
            pl.col("_return_1d").is_not_null().sum().alias("return_valid"),
            (pl.col("close") > pl.col("_ma_short"))
            .filter(pl.col("_ma_short").is_not_null())
            .mean()
            .alias("breadth_short"),
            (pl.col("close") > pl.col("_ma_long"))
            .filter(pl.col("_ma_long").is_not_null())
            .mean()
            .alias("breadth_long"),
            (pl.col("_return_1d") > 0)
            .filter(pl.col("_return_1d").is_not_null())
            .mean()
            .alias("advance_ratio"),
            pl.col("_return_1d").drop_nulls().mean().alias("ew_return_1d"),
        )
        .sort("date")
        .collect()
    )
    returns = np.asarray(
        aggregate["ew_return_1d"].cast(pl.Float64).to_list(),
        dtype=np.float64,
    )
    advances = np.asarray(
        aggregate["advance_ratio"].cast(pl.Float64).to_list(),
        dtype=np.float64,
    )
    return_window = _rolling_compounded_return(returns, config.return_window)
    advance_window = _rolling_nanmean(advances, config.advance_window)
    market_level = np.full(len(returns), np.nan, dtype=np.float64)
    level = 1.0
    for index, value in enumerate(returns):
        if math.isfinite(float(value)):
            level *= 1.0 + float(value)
            market_level[index] = level

    breadth_short = np.asarray(aggregate["breadth_short"].to_list(), dtype=np.float64)
    breadth_long = np.asarray(aggregate["breadth_long"].to_list(), dtype=np.float64)
    trend_component = np.clip(0.5 + return_window * 2.5, 0.0, 1.0)
    score = (
        0.35 * breadth_short
        + 0.35 * breadth_long
        + 0.15 * advance_window
        + 0.15 * trend_component
    )
    return aggregate.with_columns(
        pl.Series("ew_market_level", market_level),
        pl.Series(f"ew_return_{config.return_window}d", return_window),
        pl.Series(f"advance_ratio_{config.advance_window}d", advance_window),
        pl.Series("structure_score", score),
    )


def _finite_feature(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def classify_market_structure(
    features: pl.DataFrame,
    config: MarketStructureConfig | None = None,
) -> pl.DataFrame:
    """Classify every decision date from the previous trading day's features."""
    config = config or MarketStructureConfig()
    config.validate()
    return_key = f"ew_return_{config.return_window}d"
    advance_key = f"advance_ratio_{config.advance_window}d"
    required = {
        "date",
        "breadth_short",
        "breadth_long",
        "breadth_short_valid",
        "breadth_long_valid",
        "return_valid",
        return_key,
        advance_key,
        "structure_score",
    }
    if not required <= set(features.columns):
        raise ValueError(f"structure features missing columns: {sorted(required - set(features.columns))}")
    ordered = features.sort("date")
    if ordered["date"].n_unique() != ordered.height:
        raise ValueError("structure features require unique dates")
    rows = ordered.to_dicts()
    output: list[dict[str, Any]] = []
    state = WARMUP
    candidate: str | None = None
    candidate_days = 0

    for index, current in enumerate(rows):
        if index == 0:
            output.append(
                {
                    **current,
                    "source_date": None,
                    "regime": WARMUP,
                    "regime_code": -1,
                    "state_change": False,
                    "decision_reason": "no_prior_trading_day",
                }
            )
            continue

        source = rows[index - config.signal_lag_days]
        breadth_short = _finite_feature(source, "breadth_short")
        breadth_long = _finite_feature(source, "breadth_long")
        market_return = _finite_feature(source, return_key)
        sufficient = (
            int(source.get("breadth_short_valid") or 0) >= config.min_valid_assets
            and int(source.get("breadth_long_valid") or 0) >= config.min_valid_assets
            and int(source.get("return_valid") or 0) >= config.min_valid_assets
            and breadth_short is not None
            and breadth_long is not None
            and market_return is not None
        )
        changed = False
        if not sufficient:
            candidate = None
            candidate_days = 0
            reason = "insufficient_prior_data"
        else:
            bull_trigger = bool(
                breadth_short >= config.bull_breadth_short
                and breadth_long >= config.bull_breadth_long
                and market_return >= config.bull_return
            )
            bear_trigger = bool(
                breadth_short <= config.bear_breadth_short
                or (
                    breadth_long <= config.bear_breadth_long
                    and market_return <= config.bear_return
                )
            )
            if bull_trigger:
                wanted = STRUCTURAL_BULL
                trigger_reason = "breadth_and_trend_bull"
            elif bear_trigger:
                wanted = STRUCTURAL_BEAR
                trigger_reason = "breadth_or_trend_bear"
            elif state != WARMUP:
                wanted = state
                trigger_reason = "hysteresis_hold"
            else:
                wanted = WARMUP
                trigger_reason = "neutral_before_first_state"

            if wanted in {STRUCTURAL_BULL, STRUCTURAL_BEAR} and wanted != state:
                if candidate == wanted:
                    candidate_days += 1
                else:
                    candidate = wanted
                    candidate_days = 1
                if candidate_days >= config.confirm_days:
                    state = wanted
                    candidate = None
                    candidate_days = 0
                    changed = True
                    reason = f"{trigger_reason}_confirmed"
                else:
                    reason = f"{trigger_reason}_pending"
            else:
                candidate = None
                candidate_days = 0
                reason = trigger_reason

        output.append(
            {
                **current,
                "source_date": source["date"],
                "regime": state,
                "regime_code": (
                    1 if state == STRUCTURAL_BULL else 0 if state == STRUCTURAL_BEAR else -1
                ),
                "state_change": changed,
                "decision_reason": reason,
            }
        )
    return pl.DataFrame(output)


def market_structure_segments(labels: pl.DataFrame) -> list[dict[str, Any]]:
    """Collapse daily labels into contiguous, auditable segments."""
    required = {"date", "regime"}
    if not required <= set(labels.columns):
        raise ValueError("labels must contain date and regime")
    rows = labels.sort("date").select("date", "regime").to_dicts()
    if not rows:
        return []
    segments: list[dict[str, Any]] = []
    start = 0
    for index in range(1, len(rows) + 1):
        if index == len(rows) or rows[index]["regime"] != rows[start]["regime"]:
            segments.append(
                {
                    "regime": rows[start]["regime"],
                    "start": rows[start]["date"],
                    "end": rows[index - 1]["date"],
                    "trading_days": index - start,
                }
            )
            start = index
    return segments
