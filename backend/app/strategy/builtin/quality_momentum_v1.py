"""质量动量选股 v1（实验）：趋势确认后，对追高和交易风险显式扣分。

本策略不使用未来数据，也不依赖研究目录。所有滚动特征只包含当日及更早行情；
回测引擎仍负责把当日信号延迟到下一交易日成交。
"""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    make_signal_matrix,
    matrix_feature,
    valid_rolling_max,
    valid_rolling_mean,
    valid_shift,
)

META = {
    "id": "quality_momentum_v1",
    "name": "质量动量选股 v1（实验）",
    "description": "均线趋势确认 + 动量/量价质量评分 - 过热/波动/回撤/跳空风险",
    "tags": ["实验", "趋势", "动量", "风控", "可解释"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "min_momentum_20d", "type": "float", "default": 0.03},
        {"id": "max_momentum_20d", "type": "float", "default": 0.35},
        {"id": "min_momentum_60d", "type": "float", "default": 0.05},
        {"id": "max_ma20_bias", "type": "float", "default": 0.18},
        {"id": "max_annual_vol_20d", "type": "float", "default": 1.00},
        {"id": "max_abs_gap", "type": "float", "default": 0.10},
        {"id": "min_amount_20d", "type": "float", "default": 30_000_000.0},
    ],
    # The strategy owns its composite score. Framework scoring must not overwrite it.
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["quality_momentum_eligible"]
EXIT_SIGNALS = ["quality_ma_dead_5_20", "quality_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 20
ALERTS = []


def _safe_ratio(numerator, denominator):
    out = np.full(np.asarray(numerator).shape, np.nan, dtype=np.float32)
    valid = np.isfinite(numerator) & np.isfinite(denominator) & (denominator != 0)
    np.divide(numerator, denominator, out=out, where=valid)
    return out


def _unit_interval(values, low, high):
    if high <= low:
        raise ValueError("score interval must have positive width")
    return np.clip((values - np.float32(low)) / np.float32(high - low), 0.0, 1.0)


def _triangle(values, left, peak, right):
    rising = _unit_interval(values, left, peak)
    falling = _unit_interval(np.float32(right) - values, 0.0, right - peak)
    return np.minimum(rising, falling)


def compute_quality_components(market, params=None):
    """Return the complete point-in-time scorecard used by the strategy.

    The returned masks are also consumed by the research audit, so the human-readable
    explanation and the executable strategy cannot silently drift apart.
    """
    params = params or {}
    close = market.close
    valid = np.isfinite(close) & (close > 0)
    ma5 = matrix_feature(market, "ma5")
    ma10 = matrix_feature(market, "ma10")
    ma20 = matrix_feature(market, "ma20")
    ma60 = matrix_feature(market, "ma60")
    mom20 = matrix_feature(market, "momentum_20d")
    mom60 = matrix_feature(market, "momentum_60d")
    annual_vol20 = matrix_feature(market, "annual_vol_20d")
    vol_ratio5 = matrix_feature(market, "vol_ratio_5d")

    previous_close = valid_shift(close, 1, valid)
    gap = _safe_ratio(market.open, previous_close) - np.float32(1.0)
    ma20_bias = _safe_ratio(close, ma20) - np.float32(1.0)
    ma20_previous10 = valid_shift(ma20, 10, np.isfinite(ma20))
    ma20_slope10 = _safe_ratio(ma20, ma20_previous10) - np.float32(1.0)
    high20 = valid_rolling_max(close, valid, 20)
    drawdown20 = _safe_ratio(close, high20) - np.float32(1.0)

    try:
        amount = market.field("amount")
    except ValueError:
        amount = np.full(market.shape, np.nan, dtype=np.float32)
    amount20 = valid_rolling_mean(amount, valid & np.isfinite(amount), 20)

    alignment = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)
    trend_spacing = (
        np.nan_to_num(_safe_ratio(ma5, ma10) - 1.0, nan=0.0)
        + np.nan_to_num(_safe_ratio(ma10, ma20) - 1.0, nan=0.0)
        + np.nan_to_num(_safe_ratio(ma20, ma60) - 1.0, nan=0.0)
    )
    trend_quality = (
        0.65 * _unit_interval(trend_spacing, 0.0, 0.10)
        + 0.35 * _unit_interval(ma20_slope10, 0.0, 0.08)
    )
    momentum_quality = (
        0.60 * _triangle(mom20, 0.03, 0.15, 0.35)
        + 0.40 * _triangle(mom60, 0.05, 0.35, 0.90)
    )
    volume_confirmation = _triangle(vol_ratio5, 0.70, 1.50, 3.00)
    liquidity_quality = _unit_interval(np.log10(np.maximum(amount20, 1.0)), 7.5, 9.5)

    overextension_penalty = _unit_interval(ma20_bias, 0.08, 0.22)
    volatility_penalty = _unit_interval(annual_vol20, 0.45, 1.10)
    drawdown_penalty = _unit_interval(-drawdown20, 0.08, 0.25)
    gap_penalty = _unit_interval(np.abs(gap), 0.04, 0.12)
    extreme_momentum_penalty = _unit_interval(mom20, 0.28, 0.50)

    positive = (
        0.32 * trend_quality
        + 0.28 * momentum_quality
        + 0.12 * volume_confirmation
        + 0.08 * liquidity_quality
    )
    penalties = (
        0.08 * overextension_penalty
        + 0.05 * volatility_penalty
        + 0.03 * drawdown_penalty
        + 0.02 * gap_penalty
        + 0.02 * extreme_momentum_penalty
    )
    score = np.clip(100.0 * (positive - penalties), 0.0, 100.0).astype(np.float32)
    score[~np.isfinite(score)] = 0.0

    thresholds = {
        "min_momentum_20d": float(params.get("min_momentum_20d", 0.03)),
        "max_momentum_20d": float(params.get("max_momentum_20d", 0.35)),
        "min_momentum_60d": float(params.get("min_momentum_60d", 0.05)),
        "max_ma20_bias": float(params.get("max_ma20_bias", 0.18)),
        "max_annual_vol_20d": float(params.get("max_annual_vol_20d", 1.00)),
        "max_abs_gap": float(params.get("max_abs_gap", 0.10)),
        "min_amount_20d": float(params.get("min_amount_20d", 30_000_000.0)),
    }
    checks = {
        "finite_history": (
            valid
            & np.isfinite(ma60)
            & np.isfinite(mom20)
            & np.isfinite(mom60)
            & np.isfinite(annual_vol20)
            & np.isfinite(gap)
            & np.isfinite(amount20)
        ),
        "ma_alignment": alignment,
        "close_above_ma20": close >= ma20,
        "momentum_20d_floor": mom20 >= thresholds["min_momentum_20d"],
        "momentum_20d_ceiling": mom20 <= thresholds["max_momentum_20d"],
        "momentum_60d_floor": mom60 >= thresholds["min_momentum_60d"],
        "ma20_bias_ceiling": ma20_bias <= thresholds["max_ma20_bias"],
        "annual_vol_ceiling": annual_vol20 <= thresholds["max_annual_vol_20d"],
        "gap_ceiling": np.abs(gap) <= thresholds["max_abs_gap"],
        "liquidity_floor": amount20 >= thresholds["min_amount_20d"],
    }
    eligible = np.ones(market.shape, dtype=bool)
    for check in checks.values():
        eligible &= check

    return {
        "score": score,
        "eligible": eligible,
        "checks": checks,
        "thresholds": thresholds,
        "features": {
            "momentum_20d": mom20,
            "momentum_60d": mom60,
            "ma20_bias": ma20_bias,
            "ma20_slope_10d": ma20_slope10,
            "annual_vol_20d": annual_vol20,
            "volume_ratio_5d": vol_ratio5,
            "drawdown_20d": drawdown20,
            "gap": gap,
            "amount_20d": amount20,
        },
        "components": {
            "trend_quality": trend_quality,
            "momentum_quality": momentum_quality,
            "volume_confirmation": volume_confirmation,
            "liquidity_quality": liquidity_quality,
            "overextension_penalty": overextension_penalty,
            "volatility_penalty": volatility_penalty,
            "drawdown_penalty": drawdown_penalty,
            "gap_penalty": gap_penalty,
            "extreme_momentum_penalty": extreme_momentum_penalty,
        },
    }


class QualityMomentumV1MatrixStrategy:
    def required_fields(self):
        return frozenset({"close", "open", "high", "low", "volume", "amount"})

    def required_warmup_bars(self, params):
        del params
        return 70

    def compute_signals(self, market, params):
        result = compute_quality_components(market, params)
        ma5 = matrix_feature(market, "ma5")
        ma20 = matrix_feature(market, "ma20")
        previous_ma5 = valid_shift(ma5, 1, np.isfinite(ma5))
        previous_ma20 = valid_shift(ma20, 1, np.isfinite(ma20))
        previous_close = valid_shift(market.close, 1, np.isfinite(market.close))
        ma_dead = (ma5 < ma20) & (previous_ma5 >= previous_ma20)
        ma20_breakdown = (market.close < ma20) & (previous_close >= previous_ma20)
        exit_ = ma_dead | ma20_breakdown
        entry = result["eligible"] & ~exit_
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=result["score"],
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(ma_dead, 0, np.where(ma20_breakdown, 1, -1)).astype(np.int16),
            entry_signal_ids=("quality_momentum_eligible",),
            exit_signal_ids=("quality_ma_dead_5_20", "quality_ma20_breakdown"),
        )


MATRIX_STRATEGY = QualityMomentumV1MatrixStrategy()
