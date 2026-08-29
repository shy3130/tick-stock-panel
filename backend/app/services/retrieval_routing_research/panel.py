"""Pinned factor panel and narrow sealed-canonical production seam."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

import numpy as np
import polars as pl

from app.backtest.factor_zoo import compute_factor, get_alpha
from .models import DEFAULT_FEATURE_IDS, REQUIRED_BAR_COLUMNS, SYMBOL_PATTERN


@dataclass(frozen=True, slots=True)
class PinnedFactorPanel:
    feature_names: tuple[str, ...]
    dates: tuple[date, ...]
    symbols: tuple[str, ...]
    features: Mapping[str, np.ndarray]
    forward_returns: np.ndarray
    label_horizon: int
    warmup_days: int
    identity: Mapping[str, Any]

    def __post_init__(self) -> None:
        names = tuple(self.feature_names)
        dates = tuple(self.dates)
        symbols = tuple(self.symbols)
        if not names or len(set(names)) != len(names):
            raise ValueError("feature_names must be non-empty and unique")
        if not dates or tuple(sorted(set(dates))) != dates:
            raise ValueError("dates must be strictly increasing and unique")
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be non-empty and unique")
        if self.label_horizon < 1 or self.warmup_days < 0:
            raise ValueError("invalid horizon or warmup")
        if not self.identity:
            raise ValueError("identity is required")
        shape = (len(dates), len(symbols))
        copied: dict[str, np.ndarray] = {}
        for name in names:
            if name not in self.features:
                raise ValueError(f"missing feature {name}")
            arr = np.array(self.features[name], dtype=np.float64, copy=True)
            if arr.shape != shape or np.isinf(arr).any():
                raise ValueError(f"invalid feature shape or infinity: {name}")
            copied[name] = arr
        if set(self.features) != set(names):
            raise ValueError("features keys must equal feature_names")
        returns = np.array(self.forward_returns, dtype=np.float64, copy=True)
        if returns.shape != shape or np.isinf(returns).any():
            raise ValueError("invalid forward_returns shape or infinity")
        object.__setattr__(self, "feature_names", names)
        object.__setattr__(self, "dates", dates)
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "features", copied)
        object.__setattr__(self, "forward_returns", returns)

    def feature_matrix(self, names: Sequence[str] | None = None) -> np.ndarray:
        selected = tuple(names) if names is not None else self.feature_names
        if not selected or any(name not in self.features for name in selected):
            raise ValueError("unknown or empty feature selection")
        return np.stack([self.features[name] for name in selected], axis=-1)


def _content_hash(
    dates: Sequence[date],
    symbols: Sequence[str],
    names: Sequence[str],
    arrays: Sequence[np.ndarray],
) -> str:
    h = hashlib.sha256()
    for value in (*[d.isoformat() for d in dates], *symbols, *names):
        h.update(value.encode())
        h.update(b"|")
    for array in arrays:
        h.update(np.ascontiguousarray(array, dtype=np.float64).tobytes())
    return h.hexdigest()


def build_pinned_factor_panel(
    reader: Any,
    symbols: Sequence[str],
    start: date,
    end: date,
    feature_ids: Sequence[str] = DEFAULT_FEATURE_IDS,
    label_horizon: int = 1,
) -> PinnedFactorPanel:
    """Build a panel from one already generation-pinned sealed reader.

    This is deliberately the only I/O seam. The evaluator accepts an explicit
    ``PinnedFactorPanel`` and performs no file access or network calls.
    """
    if start > end or label_horizon < 1:
        raise ValueError("invalid date range or label horizon")
    selected_symbols = tuple(symbols)
    if not selected_symbols or len(set(selected_symbols)) != len(selected_symbols):
        raise ValueError("symbols must be non-empty and unique")
    if any(not re.match(SYMBOL_PATTERN, symbol) for symbol in selected_symbols):
        raise ValueError("invalid symbol")
    names = tuple(feature_ids)
    if not names or len(set(names)) != len(names):
        raise ValueError("feature_ids must be non-empty and unique")
    for name in names:
        try:
            get_alpha(name)
        except KeyError as exc:
            raise ValueError(f"unknown_feature_id:{name}") from exc
    methods = ("generation", "manifest_sha256", "market_days", "daily_bars")
    if reader is None or any(not callable(getattr(reader, method, None)) for method in methods):
        raise RuntimeError("canonical_reader_invalid")
    calendar = tuple(reader.market_days(start, end))
    if not calendar:
        raise RuntimeError("canonical_calendar_empty")
    frames: list[pl.DataFrame] = []
    for symbol in selected_symbols:
        frame = reader.daily_bars(symbol, start, end)
        if frame is None or frame.is_empty():
            raise RuntimeError(f"canonical_symbol_missing:{symbol}")
        missing = [
            column for column in ("date", *REQUIRED_BAR_COLUMNS) if column not in frame.columns
        ]
        if missing:
            raise RuntimeError(f"canonical_column_missing:{','.join(missing)}")
        frames.append(
            frame.select(["date", *REQUIRED_BAR_COLUMNS]).with_columns(
                pl.lit(symbol).alias("symbol")
            )
        )
    raw = pl.concat(frames, how="vertical").sort(["symbol", "date"])
    feature_arrays: dict[str, np.ndarray] = {}
    for name in names:
        enriched = compute_factor(raw, name)
        values = np.full((len(calendar), len(selected_symbols)), np.nan, dtype=np.float64)
        row_map = {
            (row["date"], row["symbol"]): row.get(name)
            for row in enriched.select(["date", "symbol", name]).to_dicts()
        }
        for di, day in enumerate(calendar):
            for si, symbol in enumerate(selected_symbols):
                value = row_map.get((day, symbol))
                if value is not None:
                    values[di, si] = float(value)
        feature_arrays[name] = values
    closes = np.full((len(calendar), len(selected_symbols)), np.nan, dtype=np.float64)
    for row in raw.select(["date", "symbol", "close"]).to_dicts():
        if row["date"] in calendar and row["symbol"] in selected_symbols:
            closes[calendar.index(row["date"]), selected_symbols.index(row["symbol"])] = float(
                row["close"]
            )
    forward = np.full_like(closes, np.nan)
    for index in range(len(calendar) - label_horizon):
        now, future = closes[index], closes[index + label_horizon]
        valid = np.isfinite(now) & np.isfinite(future) & (now > 0)
        forward[index, valid] = future[valid] / now[valid] - 1.0
    identity = {
        "source": "published_canonical",
        "generation": reader.generation(),
        "manifest_sha256": reader.manifest_sha256(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "feature_ids": list(names),
        "label_horizon": label_horizon,
        "content_sha256": _content_hash(
            calendar, selected_symbols, names, [*feature_arrays.values(), forward]
        ),
    }
    warmup = max(get_alpha(name).warmup for name in names)
    return PinnedFactorPanel(
        names, calendar, selected_symbols, feature_arrays, forward, label_horizon, warmup, identity
    )


__all__ = ["PinnedFactorPanel", "build_pinned_factor_panel"]
