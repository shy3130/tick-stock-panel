"""Shared A-share matrix filters and scoring for the host and standalone research."""

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field

import numpy as np

from .matrix import MarketDataMatrix, SignalMatrix
from .matrix_features import (
    _cached_matrix_operation,
    active_matrix_compute_cache,
    matrix_feature,
)
from .scoring import apply_matrix_selection


@dataclass(frozen=True)
class MatrixPipelineConfig:
    basic_filter: dict
    scoring: dict[str, float]
    order_by: str | None
    descending: bool
    scoring_directions: dict[str, str] = field(default_factory=dict)
    asset_mask: np.ndarray | None = None
    protect_strategy_cache: bool = False
    supports_continuous_prices: bool = False


def select_matrix_signals(
    market: MarketDataMatrix,
    signals: SignalMatrix,
    config: MatrixPipelineConfig,
    *,
    use_raw_prices: bool = False,
    resolve_feature: Callable[[MarketDataMatrix, str], np.ndarray] = matrix_feature,
) -> SignalMatrix:
    cache = active_matrix_compute_cache()
    protect_cache = (
        cache is not None
        and config.protect_strategy_cache
        and not cache.has_cached_operation("basic_filter_mask")
        and cache.current_bytes + _estimate_pipeline_cache_bytes(market, config) > cache.max_bytes
    )
    with cache.suspend() if protect_cache else nullcontext():
        candidates = build_pipeline_filter_mask(market, config, use_raw_prices=use_raw_prices)
        return apply_matrix_selection(
            market,
            signals,
            candidates,
            scoring=config.scoring,
            order_by=config.order_by,
            descending=config.descending,
            directions=config.scoring_directions,
            resolve_feature=resolve_feature,
        )


def _estimate_pipeline_cache_bytes(market: MarketDataMatrix, config: MatrixPipelineConfig) -> int:
    float_bytes = int(market.close.nbytes)
    bool_bytes = int(market.shape[0] * market.shape[1])
    estimated = bool_bytes
    if config.asset_mask is not None:
        estimated += bool_bytes

    feature_names = {name for name, weight in config.scoring.items() if float(weight) != 0.0}
    if not feature_names and config.order_by and config.order_by != "score":
        feature_names.add(str(config.order_by))
    for name in feature_names:
        if name in {"open", "high", "low", "close", "volume"} or name in market.fields:
            continue
        estimated += 5 * float_bytes
    return estimated


def build_pipeline_filter_mask(
    market: MarketDataMatrix,
    config: MatrixPipelineConfig,
    *,
    use_raw_prices: bool = False,
) -> np.ndarray:
    basic_mask = build_basic_filter_mask(market, config.basic_filter, use_raw_prices=use_raw_prices)
    if config.asset_mask is None:
        return basic_mask

    asset_mask = np.asarray(config.asset_mask, dtype=bool)
    if asset_mask.shape != (market.shape[1],):
        raise ValueError("matrix strategy asset mask length does not match market")
    return _cached_matrix_operation(
        "pipeline_filter_mask",
        (basic_mask, asset_mask),
        {},
        lambda: basic_mask & asset_mask[None, :],
    )


def build_basic_filter_mask(
    market: MarketDataMatrix,
    config: dict,
    *,
    use_raw_prices: bool = False,
) -> np.ndarray:
    cache = active_matrix_compute_cache()
    if cache is None:
        return _build_basic_filter_mask_uncached(market, config, use_raw_prices)
    return cache.get_or_compute(
        "basic_filter_mask",
        (),
        config,
        lambda: _build_basic_filter_mask_uncached(market, config, use_raw_prices),
        key_parts=(*cache.market_token(market), use_raw_prices),
    )


def _build_basic_filter_mask_uncached(
    market: MarketDataMatrix,
    config: dict,
    use_raw_prices: bool,
) -> np.ndarray:
    mask = np.ones(market.shape, dtype=bool)
    if not config or not config.get("enabled", True):
        return mask

    close = market.fields.get("raw_close", market.close) if use_raw_prices else market.close
    if config.get("price_min") is not None:
        mask &= close >= float(config["price_min"])
    if config.get("price_max") is not None:
        mask &= close <= float(config["price_max"])

    for prefix, name in (
        ("market_cap", "total_shares"),
        ("float_cap", "float_shares"),
        ("amount", "amount"),
        ("turnover", "turnover_rate"),
    ):
        minimum, maximum = config.get(f"{prefix}_min"), config.get(f"{prefix}_max")
        if minimum is None and maximum is None:
            continue
        values = market.field(name) if name == "amount" else market.fields.get(name)
        if values is None:
            continue
        if prefix in {"market_cap", "float_cap"}:
            values = close * values
        if not np.isfinite(values).any():
            continue
        if minimum is not None:
            mask &= values >= float(minimum)
        if maximum is not None:
            mask &= values <= float(maximum)

    if config.get("exclude_st"):
        historical_st = market.fields.get("is_st")
        if historical_st is not None:
            mask &= historical_st == 0
        else:
            asset_mask = np.array([not is_risk_warning_name(name) for name in market.names], dtype=bool)
            mask &= asset_mask[None, :]

    boards = config.get("boards")
    if isinstance(boards, list) and boards:
        board_mask = np.array(
            [_symbol_in_boards(symbol, boards) for symbol in market.symbols], dtype=bool
        )
        mask &= board_mask[None, :]
    return mask


def is_risk_warning_name(name: str) -> bool:
    return "ST" in name.upper() or "退" in name


def _symbol_in_boards(symbol: str, boards: list[str]) -> bool:
    for board in boards:
        if board == "沪主板" and symbol.startswith("60"):
            return True
        if board == "深主板" and symbol.startswith(("00", "001")):
            return True
        if board == "创业板" and symbol.startswith(("300", "301", "302")):
            return True
        if board == "科创板" and symbol.startswith(("688", "689")):
            return True
        if board == "北交所" and symbol.endswith(".BJ"):
            return True
    return False
