"""共享三市场结构突破策略到 TickFlow 矩阵信号的适配层。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Literal

import numpy as np
from longbridge_stock.structure_breakout_scanner import (
    LongBoxConfig,
    LongBoxDetector,
    RightTriangleConfig,
    RightTriangleDetector,
    StructureCandidate,
    SymmetricTriangleConfig,
    SymmetricTriangleDetector,
    scan_history,
)

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from app.market_rules import market_for_symbol

ScanFn = Callable[..., tuple[StructureCandidate, ...]]
_ENTRY_PHASES = frozenset({"BREAKOUT_CONFIRMED", "RETEST_CONFIRMED"})
_EXIT_PHASES = frozenset({"FAILED", "INVALIDATED"})
_ENTRY_SIGNAL_IDS = ("shared_structure_breakout_entry",)
_EXIT_SIGNAL_IDS = ("shared_structure_breakout_exit",)


def candidates_to_signal_matrix(
    *,
    shape: tuple[int, int],
    symbols: tuple[str, ...],
    candidates_by_symbol: Mapping[str, Sequence[StructureCandidate]],
    time_indexes_by_symbol: Mapping[str, Sequence[int]],
) -> SignalMatrix:
    """将每只证券的本地候选索引映射到联合交易日矩阵。"""

    entry = np.zeros(shape, dtype=np.uint8)
    exit_ = np.zeros(shape, dtype=np.uint8)
    score = np.zeros(shape, dtype=np.float32)
    entry_codes = np.full(shape, -1, dtype=np.int16)
    exit_codes = np.full(shape, -1, dtype=np.int16)

    for asset_id, symbol in enumerate(symbols):
        local_times = tuple(time_indexes_by_symbol.get(symbol, ()))
        events: dict[int, list[StructureCandidate]] = {}
        for item in candidates_by_symbol.get(symbol, ()):
            local_index = int(item.known_at_index)
            if 0 <= local_index < len(local_times):
                events.setdefault(int(local_times[local_index]), []).append(item)

        active = False
        for time_id in sorted(events):
            items = events[time_id]
            score[time_id, asset_id] = max(float(item.score) for item in items)
            has_exit = any(item.phase in _EXIT_PHASES for item in items)
            if has_exit:
                exit_[time_id, asset_id] = 1
                exit_codes[time_id, asset_id] = 0
                active = False
                continue
            has_entry = any(item.phase in _ENTRY_PHASES for item in items)
            if has_entry and not active:
                entry[time_id, asset_id] = 1
                entry_codes[time_id, asset_id] = 0
                active = True

    return make_signal_matrix(
        shape,
        entry=entry,
        exit=exit_,
        score=score,
        entry_signal_code=entry_codes,
        exit_signal_code=exit_codes,
        entry_signal_ids=_ENTRY_SIGNAL_IDS,
        exit_signal_ids=_EXIT_SIGNAL_IDS,
    )


FixedPattern = Literal["right_triangle", "symmetric_triangle"]


def _detectors_for_params(
    params: dict,
    fixed_pattern: FixedPattern | None = None,
) -> tuple[object, ...]:
    min_volume = float(params.get("min_breakout_volume_ratio", 1.5))
    if fixed_pattern == "right_triangle":
        return (
            RightTriangleDetector(
                RightTriangleConfig(min_breakout_volume_ratio=min_volume)
            ),
        )
    if fixed_pattern == "symmetric_triangle":
        return (
            SymmetricTriangleDetector(
                SymmetricTriangleConfig(min_breakout_volume_ratio=min_volume)
            ),
        )
    detectors: list[object] = []
    if params.get("include_long_box", True):
        detectors.append(LongBoxDetector(LongBoxConfig(min_breakout_volume_ratio=min_volume)))
    if params.get("include_right_triangle", True):
        detectors.append(
            RightTriangleDetector(RightTriangleConfig(min_breakout_volume_ratio=min_volume))
        )
    if params.get("include_symmetric_triangle", True):
        detectors.append(
            SymmetricTriangleDetector(
                SymmetricTriangleConfig(min_breakout_volume_ratio=min_volume)
            )
        )
    return tuple(detectors)


class SharedStructureBreakoutMatrixStrategy:
    def __init__(
        self,
        scan_fn: ScanFn = scan_history,
        fixed_pattern: FixedPattern | None = None,
    ) -> None:
        if fixed_pattern not in {None, "right_triangle", "symmetric_triangle"}:
            raise ValueError(f"unsupported fixed structure pattern: {fixed_pattern}")
        self._scan_fn = scan_fn
        self._fixed_pattern = fixed_pattern

    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume", "amount"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 520

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        detectors = _detectors_for_params(params, self._fixed_pattern)
        candidates_by_symbol: dict[str, tuple[StructureCandidate, ...]] = {}
        time_indexes_by_symbol: dict[str, tuple[int, ...]] = {}
        amount = market.fields.get("amount")

        for asset_id, symbol in enumerate(market.symbols):
            valid = (
                np.isfinite(market.open[:, asset_id])
                & np.isfinite(market.high[:, asset_id])
                & np.isfinite(market.low[:, asset_id])
                & np.isfinite(market.close[:, asset_id])
            )
            time_indexes = tuple(int(value) for value in np.flatnonzero(valid))
            time_indexes_by_symbol[symbol] = time_indexes
            rows: list[dict] = []
            for time_id in time_indexes:
                close = float(market.close[time_id, asset_id])
                turnover = (
                    float(amount[time_id, asset_id])
                    if amount is not None and np.isfinite(amount[time_id, asset_id])
                    else close * float(market.volume[time_id, asset_id])
                )
                rows.append({
                    "trade_date": market.timestamp_labels[time_id][:10],
                    "open": float(market.open[time_id, asset_id]),
                    "high": float(market.high[time_id, asset_id]),
                    "low": float(market.low[time_id, asset_id]),
                    "close": close,
                    "volume": float(market.volume[time_id, asset_id]),
                    "turnover": turnover,
                })
            candidates_by_symbol[symbol] = (
                self._scan_fn(
                    symbol,
                    market_for_symbol(symbol),
                    rows,
                    detectors=detectors,
                )
                if rows and detectors
                else ()
            )

        return candidates_to_signal_matrix(
            shape=market.shape,
            symbols=market.symbols,
            candidates_by_symbol=candidates_by_symbol,
            time_indexes_by_symbol=time_indexes_by_symbol,
        )
