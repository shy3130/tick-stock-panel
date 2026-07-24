"""Adapt shared causal Dow-pattern matches to TickFlow signal matrices."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

import numpy as np
from longbridge_stock.dow_trend_replay import Bar
from longbridge_stock.system_patterns import PatternMatch, scan_system_pattern

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix

logger = logging.getLogger(__name__)

ScanFn = Callable[
    [str, tuple[Bar, ...], dict],
    tuple[PatternMatch, ...],
]


class DowPatternMatrixStrategy:
    def __init__(
        self,
        pattern_id: str,
        role: str,
        warmup_bars: int,
        signal_id: str,
        scan_fn: ScanFn = scan_system_pattern,
    ) -> None:
        if role not in {"buy", "early_buy", "risk"}:
            raise ValueError("role must be buy, early_buy, or risk")
        self.pattern_id = pattern_id
        self.role = role
        self.warmup_bars = int(warmup_bars)
        self.signal_id = signal_id
        self._scan_fn = scan_fn

    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume", "amount"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return self.warmup_bars

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = np.zeros(market.shape, dtype=np.uint8)
        exit_ = np.zeros(market.shape, dtype=np.uint8)
        score = np.zeros(market.shape, dtype=np.float32)
        entry_codes = np.full(market.shape, -1, dtype=np.int16)
        exit_codes = np.full(market.shape, -1, dtype=np.int16)

        for asset_id, symbol in enumerate(market.symbols):
            valid = (
                np.isfinite(market.open[:, asset_id])
                & np.isfinite(market.high[:, asset_id])
                & np.isfinite(market.low[:, asset_id])
                & np.isfinite(market.close[:, asset_id])
                & np.isfinite(market.volume[:, asset_id])
            )
            time_indexes = tuple(int(value) for value in np.flatnonzero(valid))
            bars = tuple(
                Bar(
                    index=local_id,
                    timestamp=date.fromisoformat(market.timestamp_labels[time_id][:10]),
                    open=float(market.open[time_id, asset_id]),
                    high=float(market.high[time_id, asset_id]),
                    low=float(market.low[time_id, asset_id]),
                    close=float(market.close[time_id, asset_id]),
                    volume=float(market.volume[time_id, asset_id]),
                )
                for local_id, time_id in enumerate(time_indexes)
            )
            if not bars:
                continue
            try:
                matches = self._scan_fn(self.pattern_id, bars, params)
            except Exception:
                logger.exception(
                    "Dow pattern scan failed: pattern_id=%s symbol=%s",
                    self.pattern_id,
                    symbol,
                )
                continue

            for match in matches:
                local_id = int(match.known_at_index)
                if not 0 <= local_id < len(time_indexes):
                    continue
                time_id = time_indexes[local_id]
                score[time_id, asset_id] = max(
                    score[time_id, asset_id],
                    float(match.score),
                )
                if self.role == "risk":
                    exit_[time_id, asset_id] = 1
                    exit_codes[time_id, asset_id] = 0
                else:
                    entry[time_id, asset_id] = 1
                    entry_codes[time_id, asset_id] = 0

        is_risk = self.role == "risk"
        return make_signal_matrix(
            market.shape,
            entry=entry,
            exit=exit_,
            score=score,
            entry_signal_code=entry_codes,
            exit_signal_code=exit_codes,
            entry_signal_ids=() if is_risk else (self.signal_id,),
            exit_signal_ids=(self.signal_id,) if is_risk else (),
        )
