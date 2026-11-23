"""The shared NumPy strategy features and job-local compute cache."""

from __future__ import annotations

import hashlib
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import numpy as np

from .matrix import MarketDataMatrix, ValidBarIndex, _build_valid_bar_index
from .matrix import SignalMatrix as SignalMatrix
from .matrix import make_signal_matrix as make_signal_matrix
from .numba_runtime import run_numba_parallel

try:
    from numba import njit, prange
except ImportError:
    def njit(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def decorator(func):
            return func

        return decorator

    prange = range


_MATRIX_CACHE_VERSION = 1
_ROLLING_MATERIALIZED_WINDOW_BUDGET_BYTES = 32 * 1024 * 1024


_ACTIVE_MATRIX_CACHE: ContextVar[MatrixComputeCache | None] = ContextVar(
    "active_matrix_compute_cache",
    default=None,
)
_ACTIVE_VALID_BAR_INDEX: ContextVar[Any] = ContextVar(
    "active_valid_bar_index",
    default=None,
)


def _freeze_cache_value(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, dict):
        return tuple(
            (str(key), _freeze_cache_value(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_cache_value(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze_cache_value(item) for item in value), key=repr))
    if isinstance(value, float):
        if np.isnan(value):
            return ("float", "nan")
        if np.isposinf(value):
            return ("float", "inf")
        if np.isneginf(value):
            return ("float", "-inf")
        return ("float", value)
    if isinstance(value, (str, int, bool, bytes, type(None))):
        return value
    return (type(value).__qualname__, repr(value))


class MatrixComputeCache:
    """Job-scoped byte-bounded cache for deterministic matrix operations."""

    def __init__(
        self,
        *,
        max_bytes: int = 512 * 1024 * 1024,
        max_item_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("matrix cache max_bytes must be positive")
        if max_item_bytes <= 0:
            raise ValueError("matrix cache max_item_bytes must be positive")
        self.max_bytes = int(max_bytes)
        self.max_item_bytes = min(int(max_item_bytes), self.max_bytes)
        self._entries: OrderedDict[tuple, np.ndarray] = OrderedDict()
        self._lineage: dict[int, tuple[weakref.ReferenceType[np.ndarray], tuple]] = {}
        self._market_tokens: dict[int, tuple[MarketDataMatrix, tuple]] = {}
        self._market_counter = 0
        self._lock = threading.RLock()
        self._closed = False
        self._current_bytes = 0
        self._peak_bytes = 0
        self._calls = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._skipped = 0
        self._fingerprint_bytes = 0
        self._fingerprint_ms = 0.0
        self._operations: dict[str, dict[str, int | float]] = {}

    @contextmanager
    def activate(self, market: MarketDataMatrix) -> Iterator[MatrixComputeCache]:
        self.register_market(market)
        token = _ACTIVE_MATRIX_CACHE.set(self)
        try:
            yield self
        finally:
            _ACTIVE_MATRIX_CACHE.reset(token)

    def register_market(self, market: MarketDataMatrix) -> tuple:
        self._ensure_open()
        market_id = id(market)
        with self._lock:
            existing = self._market_tokens.get(market_id)
            if existing is not None and existing[0] is market:
                return existing[1]
            self._market_counter += 1
            token = ("market", self._market_counter)
            self._market_tokens[market_id] = (market, token)
            arrays = {
                "timestamps": market.timestamps,
                "session_ids": market.session_ids,
                "valid_bar_offsets": market.valid_bars.offsets,
                "valid_bar_rows": market.valid_bars.rows,
                "open": market.open,
                "high": market.high,
                "low": market.low,
                "close": market.close,
                "volume": market.volume,
                "tradable": market.tradable,
                "limit_up_locked": market.limit_up_locked,
                "limit_down_locked": market.limit_down_locked,
                **{f"field:{name}": values for name, values in market.fields.items()},
            }
            for name, values in arrays.items():
                self._register_lineage(values, (token, name))
            return token

    @contextmanager
    def suspend(self) -> Iterator[None]:
        """Temporarily bypass this cache without changing its retained entries."""
        token = _ACTIVE_MATRIX_CACHE.set(None)
        try:
            yield
        finally:
            _ACTIVE_MATRIX_CACHE.reset(token)

    def market_token(self, market: MarketDataMatrix) -> tuple:
        return self.register_market(market)

    def get_or_compute(
        self,
        operation: str,
        inputs: tuple[np.ndarray, ...],
        params: Any,
        compute: Callable[[], np.ndarray],
        *,
        key_parts: Any = (),
    ) -> np.ndarray:
        self._ensure_open()
        input_tokens = tuple(self._array_token(values) for values in inputs)
        key = (
            _MATRIX_CACHE_VERSION,
            str(operation),
            input_tokens,
            _freeze_cache_value(params),
            _freeze_cache_value(key_parts),
        )
        with self._lock:
            self._calls += 1
            op_stats = self._operations.setdefault(
                str(operation),
                {
                    "calls": 0,
                    "hits": 0,
                    "misses": 0,
                    "compute_ms": 0.0,
                    "computed_bytes": 0,
                },
            )
            op_stats["calls"] += 1
            cached = self._entries.get(key)
            if cached is not None:
                self._hits += 1
                op_stats["hits"] += 1
                self._entries.move_to_end(key)
                return cached
            self._misses += 1
            op_stats["misses"] += 1

        compute_started = time.perf_counter()
        result = np.asarray(compute())
        compute_ms = (time.perf_counter() - compute_started) * 1000.0
        with self._lock:
            op_stats = self._operations[str(operation)]
            op_stats["compute_ms"] = float(op_stats["compute_ms"]) + compute_ms
            op_stats["computed_bytes"] = int(op_stats["computed_bytes"]) + int(result.nbytes)
        if result.ndim == 0:
            raise ValueError(f"cached matrix operation {operation} returned a scalar")
        if result.flags.writeable:
            result.flags.writeable = False
        if result.nbytes > self.max_item_bytes or result.nbytes > self.max_bytes:
            with self._lock:
                self._skipped += 1
            return result

        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                self._entries.move_to_end(key)
                return existing
            self._evict_for(result.nbytes)
            self._entries[key] = result
            self._current_bytes += int(result.nbytes)
            self._peak_bytes = max(self._peak_bytes, self._current_bytes)
            derived = hashlib.blake2b(repr(key).encode("utf-8"), digest_size=16).digest()
            self._register_lineage(result, ("derived", derived))
        return result

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            hit_rate = self._hits / self._calls if self._calls else 0.0
            return {
                "enabled": True,
                "max_bytes": self.max_bytes,
                "max_item_bytes": self.max_item_bytes,
                "current_bytes": self._current_bytes,
                "peak_bytes": self._peak_bytes,
                "entries": len(self._entries),
                "calls": self._calls,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "skipped": self._skipped,
                "hit_rate": round(float(hit_rate), 6),
                "fingerprint_bytes": self._fingerprint_bytes,
                "fingerprint_ms": round(self._fingerprint_ms, 3),
                "operations": {
                    name: {
                        **values,
                        "compute_ms": round(float(values["compute_ms"]), 3),
                    }
                    for name, values in sorted(self._operations.items())
                },
            }

    @property
    def current_bytes(self) -> int:
        with self._lock:
            return int(self._current_bytes)

    def has_cached_operation(self, operation: str) -> bool:
        with self._lock:
            return any(key[1] == operation for key in self._entries)

    def close(self) -> None:
        with self._lock:
            self._entries.clear()
            self._lineage.clear()
            self._market_tokens.clear()
            self._current_bytes = 0
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("matrix compute cache is closed")

    def _array_token(self, values: np.ndarray) -> tuple:
        array = np.asarray(values)
        with self._lock:
            existing = self._lineage.get(id(array))
            if existing is not None and existing[0]() is array:
                return existing[1]

        contiguous = array if array.flags.c_contiguous else np.ascontiguousarray(array)
        started = time.perf_counter()
        digest = hashlib.blake2b(contiguous.view(np.uint8), digest_size=16).digest()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self._lock:
            self._fingerprint_bytes += int(contiguous.nbytes)
            self._fingerprint_ms += elapsed_ms
        return ("content", array.dtype.str, tuple(array.shape), digest)

    def _register_lineage(self, array: np.ndarray, token: tuple) -> None:
        array_id = id(array)

        def _remove(reference: weakref.ReferenceType[np.ndarray]) -> None:
            with self._lock:
                current = self._lineage.get(array_id)
                if current is not None and current[0] is reference:
                    self._lineage.pop(array_id, None)

        reference = weakref.ref(array, _remove)
        self._lineage[array_id] = (reference, token)

    def _evict_for(self, incoming_bytes: int) -> None:
        while self._entries and self._current_bytes + incoming_bytes > self.max_bytes:
            _, evicted = self._entries.popitem(last=False)
            self._current_bytes -= int(evicted.nbytes)
            self._evictions += 1


def active_matrix_compute_cache() -> MatrixComputeCache | None:
    return _ACTIVE_MATRIX_CACHE.get()


@contextmanager
def _activate_valid_bar_index(index: ValidBarIndex) -> Iterator[None]:
    token = _ACTIVE_VALID_BAR_INDEX.set(index)
    try:
        yield
    finally:
        _ACTIVE_VALID_BAR_INDEX.reset(token)


def _cached_matrix_operation(
    operation: str,
    inputs: tuple[np.ndarray, ...],
    params: Any,
    compute: Callable[[], np.ndarray],
    *,
    key_parts: Any = (),
) -> np.ndarray:
    cache = active_matrix_compute_cache()
    if cache is None:
        return compute()
    return cache.get_or_compute(
        operation,
        inputs,
        params,
        compute,
        key_parts=key_parts,
    )


def shift(values: np.ndarray, periods: int = 1) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    out = np.full(source.shape, np.nan, dtype=np.float32)
    if periods == 0:
        out[:] = source
    elif periods > 0 and periods < source.shape[0]:
        out[periods:] = source[:-periods]
    elif periods < 0 and -periods < source.shape[0]:
        out[:periods] = source[-periods:]
    return out


def _resolve_valid_bar_index(
    source: np.ndarray,
    valid: np.ndarray,
    bar_index: ValidBarIndex | None,
) -> ValidBarIndex:
    if bar_index is not None:
        if bar_index.shape != source.shape:
            raise ValueError("valid bar index shape does not match values")
        return bar_index
    active = _ACTIVE_VALID_BAR_INDEX.get()
    if isinstance(active, ValidBarIndex) and active.shape == source.shape:
        return active
    return _build_valid_bar_index(valid)


@njit(cache=True, nogil=True, parallel=True)
def _valid_shift_kernel(
    source: np.ndarray,
    valid: np.ndarray,
    offsets: np.ndarray,
    rows: np.ndarray,
    periods: int,
) -> np.ndarray:
    out = np.full(source.shape, np.nan, dtype=np.float32)
    distance = abs(periods)
    for asset_id in prange(source.shape[1]):
        start = int(offsets[asset_id])
        stop = int(offsets[asset_id + 1])
        ring = np.empty(distance, dtype=np.int32)
        seen = 0
        if periods > 0:
            for position in range(start, stop):
                row = int(rows[position])
                if not valid[row, asset_id] or not np.isfinite(source[row, asset_id]):
                    continue
                slot = seen % distance
                if seen >= distance:
                    out[row, asset_id] = source[int(ring[slot]), asset_id]
                ring[slot] = row
                seen += 1
        else:
            for position in range(stop - 1, start - 1, -1):
                row = int(rows[position])
                if not valid[row, asset_id] or not np.isfinite(source[row, asset_id]):
                    continue
                slot = seen % distance
                if seen >= distance:
                    out[row, asset_id] = source[int(ring[slot]), asset_id]
                ring[slot] = row
                seen += 1
    return out


def valid_shift(
    values: np.ndarray,
    periods: int = 1,
    valid_mask: np.ndarray | None = None,
    *,
    bar_index: ValidBarIndex | None = None,
) -> np.ndarray:
    """Shift by effective observations, skipping missing market bars.

    The matrix keeps a shared time axis, so a suspended asset can have NaN
    rows between two valid bars.  Standard per-symbol indicators must treat
    those rows as absent rather than as observations.
    """
    source = np.asarray(values, dtype=np.float32)
    valid = (
        np.isfinite(source)
        if valid_mask is None
        else np.asarray(valid_mask, dtype=bool) & np.isfinite(source)
    )
    if valid.shape != source.shape:
        raise ValueError("valid_shift mask shape does not match values")
    if periods == 0:
        out = np.full(source.shape, np.nan, dtype=np.float32)
        out[valid] = source[valid]
        return out
    index = _resolve_valid_bar_index(source, valid, bar_index)

    return _cached_matrix_operation(
        "valid_shift",
        (source, valid, index.offsets, index.rows),
        {"periods": int(periods)},
        lambda: run_numba_parallel(
            lambda: _valid_shift_kernel(
                source,
                valid,
                index.offsets,
                index.rows,
                int(periods),
            )
        ),
    )


def rolling_min(values: np.ndarray, window: int) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    return _cached_matrix_operation(
        "rolling_min",
        (source,),
        {"window": int(window)},
        lambda: _rolling_reduce(source, window, np.min),
    )


def rolling_max(values: np.ndarray, window: int) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    return _cached_matrix_operation(
        "rolling_max",
        (source,),
        {"window": int(window)},
        lambda: _rolling_reduce(source, window, np.max),
    )


def rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    return _cached_matrix_operation(
        "rolling_mean",
        (source,),
        {"window": int(window)},
        lambda: _rolling_reduce(source, window, np.mean),
    )


def rolling_sum(values: np.ndarray, window: int) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    return _cached_matrix_operation(
        "rolling_sum",
        (source,),
        {"window": int(window)},
        lambda: _rolling_reduce(source, window, np.sum),
    )


def rolling_std(values: np.ndarray, window: int, *, ddof: int = 0) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    return _cached_matrix_operation(
        "rolling_std",
        (source,),
        {"window": int(window), "ddof": int(ddof)},
        lambda: _rolling_reduce(
            source,
            window,
            lambda view, axis: np.std(view, axis=axis, ddof=int(ddof)),
            materialized_window_budget_bytes=_ROLLING_MATERIALIZED_WINDOW_BUDGET_BYTES,
        ),
    )


_VALID_REDUCE_MIN = 0
_VALID_REDUCE_MAX = 1
_VALID_REDUCE_MEAN = 2
_VALID_REDUCE_STD = 3
_VALID_REDUCE_SUM = 4


@njit(cache=True, nogil=True, parallel=True)
def _valid_rolling_kernel(
    source: np.ndarray,
    valid: np.ndarray,
    offsets: np.ndarray,
    rows: np.ndarray,
    window: int,
    operation: int,
    ddof: int,
) -> np.ndarray:
    out = np.full(source.shape, np.nan, dtype=np.float32)
    window_value = float(window)
    denominator = float(window - ddof)
    for asset_id in prange(source.shape[1]):
        start = int(offsets[asset_id])
        stop = int(offsets[asset_id + 1])
        ring = np.empty(window, dtype=np.float32)
        seen = 0
        for position in range(start, stop):
            row = int(rows[position])
            value = source[row, asset_id]
            if not valid[row, asset_id] or not np.isfinite(value):
                continue
            ring[seen % window] = value
            seen += 1
            if seen < window:
                continue
            first = seen - window
            if operation == _VALID_REDUCE_MIN:
                result = ring[first % window]
                for offset in range(1, window):
                    candidate = ring[(first + offset) % window]
                    if candidate < result:
                        result = candidate
                out[row, asset_id] = result
            elif operation == _VALID_REDUCE_MAX:
                result = ring[first % window]
                for offset in range(1, window):
                    candidate = ring[(first + offset) % window]
                    if candidate > result:
                        result = candidate
                out[row, asset_id] = result
            else:
                total = 0.0
                for offset in range(window):
                    total += float(ring[(first + offset) % window])
                mean = total / window_value
                if operation == _VALID_REDUCE_MEAN:
                    out[row, asset_id] = mean
                elif operation == _VALID_REDUCE_SUM:
                    out[row, asset_id] = total
                else:
                    squared = 0.0
                    for offset in range(window):
                        delta = float(ring[(first + offset) % window]) - mean
                        squared += delta * delta
                    out[row, asset_id] = np.sqrt(squared / denominator)
    return out


def _valid_rolling_reduce(
    values: np.ndarray,
    valid_mask: np.ndarray,
    window: int,
    operation: int,
    *,
    ddof: int = 0,
    bar_index: ValidBarIndex | None = None,
) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    valid = np.asarray(valid_mask, dtype=bool)
    if source.ndim != 2 or valid.shape != source.shape:
        raise ValueError("valid rolling inputs must be matching 2D arrays")
    if window <= 0:
        raise ValueError("valid rolling window must be positive")
    if ddof < 0 or ddof >= window:
        raise ValueError("valid rolling ddof must be in [0, window)")
    index = _resolve_valid_bar_index(source, valid, bar_index)
    return run_numba_parallel(
        lambda: _valid_rolling_kernel(
            source,
            valid,
            index.offsets,
            index.rows,
            int(window),
            int(operation),
            int(ddof),
        )
    )


def valid_rolling_min(
    values: np.ndarray,
    valid_mask: np.ndarray,
    window: int,
    *,
    bar_index: ValidBarIndex | None = None,
) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    valid = np.asarray(valid_mask, dtype=bool) & np.isfinite(source)
    index = _resolve_valid_bar_index(source, valid, bar_index)
    return _cached_matrix_operation(
        "valid_rolling_min",
        (source, valid, index.offsets, index.rows),
        {"window": int(window)},
        lambda: _valid_rolling_reduce(
            source,
            valid,
            window,
            _VALID_REDUCE_MIN,
            bar_index=index,
        ),
    )


def valid_rolling_max(
    values: np.ndarray,
    valid_mask: np.ndarray,
    window: int,
    *,
    bar_index: ValidBarIndex | None = None,
) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    valid = np.asarray(valid_mask, dtype=bool) & np.isfinite(source)
    index = _resolve_valid_bar_index(source, valid, bar_index)
    return _cached_matrix_operation(
        "valid_rolling_max",
        (source, valid, index.offsets, index.rows),
        {"window": int(window)},
        lambda: _valid_rolling_reduce(
            source,
            valid,
            window,
            _VALID_REDUCE_MAX,
            bar_index=index,
        ),
    )


def valid_rolling_mean(
    values: np.ndarray,
    valid_mask: np.ndarray,
    window: int,
    *,
    bar_index: ValidBarIndex | None = None,
) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    valid = np.asarray(valid_mask, dtype=bool) & np.isfinite(source)
    index = _resolve_valid_bar_index(source, valid, bar_index)
    return _cached_matrix_operation(
        "valid_rolling_mean",
        (source, valid, index.offsets, index.rows),
        {"window": int(window)},
        lambda: _valid_rolling_reduce(
            source,
            valid,
            window,
            _VALID_REDUCE_MEAN,
            bar_index=index,
        ),
    )


def valid_rolling_std(
    values: np.ndarray,
    valid_mask: np.ndarray,
    window: int,
    *,
    ddof: int = 0,
    bar_index: ValidBarIndex | None = None,
) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    valid = np.asarray(valid_mask, dtype=bool) & np.isfinite(source)
    index = _resolve_valid_bar_index(source, valid, bar_index)
    return _cached_matrix_operation(
        "valid_rolling_std",
        (source, valid, index.offsets, index.rows),
        {"window": int(window), "ddof": int(ddof)},
        lambda: _valid_rolling_reduce(
            source,
            valid,
            window,
            _VALID_REDUCE_STD,
            ddof=int(ddof),
            bar_index=index,
        ),
    )


def valid_rolling_sum(
    values: np.ndarray,
    valid_mask: np.ndarray,
    window: int,
    *,
    bar_index: ValidBarIndex | None = None,
) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    valid = np.asarray(valid_mask, dtype=bool) & np.isfinite(source)
    index = _resolve_valid_bar_index(source, valid, bar_index)
    return _cached_matrix_operation(
        "valid_rolling_sum",
        (source, valid, index.offsets, index.rows),
        {"window": int(window)},
        lambda: _valid_rolling_reduce(
            source,
            valid,
            window,
            _VALID_REDUCE_SUM,
            bar_index=index,
        ),
    )


def rolling_quantile(values: np.ndarray, window: int, quantile: float) -> np.ndarray:
    source = np.asarray(values, dtype=np.float32)
    q = float(quantile)
    if not 0.0 <= q <= 1.0:
        raise ValueError("rolling quantile must be in [0, 1]")
    return _cached_matrix_operation(
        "rolling_quantile",
        (source,),
        {"window": int(window), "quantile": q},
        lambda: _rolling_reduce(
            source,
            window,
            lambda view, axis: np.quantile(view, q, axis=axis),
            materialized_window_budget_bytes=_ROLLING_MATERIALIZED_WINDOW_BUDGET_BYTES,
        ),
    )


def ewm_adjust_false(
    values: np.ndarray,
    *,
    span: int | None = None,
    alpha: float | None = None,
) -> np.ndarray:
    """Pandas-compatible EWM mean for ``adjust=False, ignore_na=False``."""
    if alpha is None:
        if span is None or span <= 0:
            raise ValueError("span must be positive when alpha is omitted")
        alpha = 2.0 / (float(span) + 1.0)
    if not 0.0 < float(alpha) <= 1.0:
        raise ValueError("alpha must be in (0, 1]")

    source = np.asarray(values, dtype=np.float32)
    alpha_value = float(alpha)

    def _compute() -> np.ndarray:
        out = np.full(source.shape, np.nan, dtype=np.float32)
        weighted = np.zeros(source.shape[1], dtype=np.float64)
        old_weight = np.ones(source.shape[1], dtype=np.float64)
        initialized = np.zeros(source.shape[1], dtype=bool)
        decay = 1.0 - alpha_value

        for time_id in range(source.shape[0]):
            row = source[time_id].astype(np.float64, copy=False)
            finite = np.isfinite(row)
            continuing = finite & initialized
            starting = finite & ~initialized

            old_weight[initialized] *= decay
            if continuing.any():
                denominator = old_weight[continuing] + alpha_value
                weighted[continuing] = (
                    old_weight[continuing] * weighted[continuing]
                    + alpha_value * row[continuing]
                ) / denominator
                old_weight[continuing] = 1.0
            if starting.any():
                weighted[starting] = row[starting]
                old_weight[starting] = 1.0
                initialized[starting] = True
            out[time_id, initialized] = weighted[initialized].astype(np.float32)
        return out

    return _cached_matrix_operation(
        "ewm_adjust_false",
        (source,),
        {"alpha": alpha_value},
        _compute,
    )


@njit(cache=True, nogil=True, parallel=True)
def _valid_ewm_kernel(
    source: np.ndarray,
    valid: np.ndarray,
    offsets: np.ndarray,
    rows: np.ndarray,
    alpha: float,
) -> np.ndarray:
    out = np.full(source.shape, np.nan, dtype=np.float32)
    decay = 1.0 - alpha
    for asset_id in prange(source.shape[1]):
        start = int(offsets[asset_id])
        stop = int(offsets[asset_id + 1])
        initialized = False
        state = 0.0
        for position in range(start, stop):
            row = int(rows[position])
            value = source[row, asset_id]
            if not valid[row, asset_id] or not np.isfinite(value):
                continue
            if initialized:
                state = decay * state + alpha * float(value)
            else:
                state = float(value)
                initialized = True
            out[row, asset_id] = np.float32(state)
    return out


def valid_ewm_adjust_false(
    values: np.ndarray,
    valid_mask: np.ndarray,
    *,
    span: int | None = None,
    alpha: float | None = None,
    bar_index: ValidBarIndex | None = None,
) -> np.ndarray:
    """Pandas-compatible EWM that advances only on effective observations."""
    if alpha is None:
        if span is None or span <= 0:
            raise ValueError("span must be positive when alpha is omitted")
        alpha = 2.0 / (float(span) + 1.0)
    if not 0.0 < float(alpha) <= 1.0:
        raise ValueError("alpha must be in (0, 1]")
    source = np.asarray(values, dtype=np.float32)
    valid = np.asarray(valid_mask, dtype=bool) & np.isfinite(source)
    if valid.shape != source.shape:
        raise ValueError("valid_ewm mask shape does not match values")
    alpha_value = float(alpha)
    index = _resolve_valid_bar_index(source, valid, bar_index)

    return _cached_matrix_operation(
        "valid_ewm_adjust_false",
        (source, valid, index.offsets, index.rows),
        {"alpha": alpha_value},
        lambda: run_numba_parallel(
            lambda: _valid_ewm_kernel(
                source,
                valid,
                index.offsets,
                index.rows,
                alpha_value,
            )
        ),
    )


def safe_divide(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    out = np.full(np.broadcast_shapes(numerator.shape, denominator.shape), np.nan, dtype=np.float32)
    np.divide(
        numerator,
        denominator,
        out=out,
        where=np.isfinite(denominator) & (denominator != 0),
    )
    return out


def _rolling_reduce(
    values: np.ndarray,
    window: int,
    reducer: Callable[..., np.ndarray],
    *,
    asset_chunk_size: int = 256,
    materialized_window_budget_bytes: int | None = None,
) -> np.ndarray:
    if window <= 0:
        raise ValueError("rolling window must be positive")
    source = np.asarray(values, dtype=np.float32)
    if source.ndim != 2:
        raise ValueError("matrix rolling features require a 2D array")
    out = np.full(source.shape, np.nan, dtype=np.float32)
    if source.shape[0] < window:
        return out

    if materialized_window_budget_bytes is not None:
        output_rows = source.shape[0] - window + 1
        logical_bytes_per_asset = output_rows * window * source.itemsize
        if logical_bytes_per_asset > 0:
            budget_chunk_size = max(
                1,
                int(materialized_window_budget_bytes) // logical_bytes_per_asset,
            )
            asset_chunk_size = min(asset_chunk_size, budget_chunk_size)

    for start in range(0, source.shape[1], asset_chunk_size):
        stop = min(start + asset_chunk_size, source.shape[1])
        view = np.lib.stride_tricks.sliding_window_view(
            source[:, start:stop],
            window_shape=window,
            axis=0,
        )
        out[window - 1 :, start:stop] = reducer(view, axis=-1).astype(np.float32, copy=False)
    return out


_MATRIX_COMPUTED_FEATURES = frozenset({
    "prev_close", "change_pct", "change_amount", "amplitude",
    "boll_upper", "boll_lower", "boll_position", "boll_width",
    "high_60d", "low_60d", "annual_vol_20d",
    "macd_dif", "macd_dea", "macd_hist",
    "macd_dif_pct", "macd_dea_pct", "macd_hist_pct",
    "kdj_k", "kdj_d", "kdj_j", "atr_14", "atr_pct",
    "vol_ma5", "vol_ma10", "vol_ratio_5d", "vol_ratio_10d", "vol_trend_5_10",
    "turnover_ratio_5d", "log_amount", "amount_ratio_5d",
    "gap_return", "intraday_return", "close_position",
    "distance_to_high_60d", "distance_from_low_60d",
    "max_ret_20d", "ret_skew_20d", "up_days_20d",
    "amihud_20d", "turnover_z_60d", "vol_price_corr_20d",
    "vwap_bias", "vol_trend_5_60",
    "limit_up_count_20d", "limit_up_count_60d",
    # --- 扩充批次 (2026-09-05): 与注册表/scoring 口径一致的 16 个新虚拟因子 ---
    "log_float_mv", "mom_accel_20_60", "rsi_14_delta_5d",
    "overnight_ret_20d", "intraday_ret_20d", "downside_vol_20d",
    "vol_regime_5_60", "amplitude_trend_20_60", "obv_trend_20d",
    "amount_mean_20d", "turnover_mean_20d", "turnover_std_20d",
    "position_240d", "distance_to_high_240d", "kdj_kd_diff",
})


def matrix_feature(
    market: MarketDataMatrix,
    name: str,
    *,
    resolve_feature: Callable[[MarketDataMatrix, str], np.ndarray] | None = None,
) -> np.ndarray:
    """Resolve a stored or built-in feature on the supplied matrix."""
    if name in {"open", "high", "low", "close", "volume"} or name in market.fields:
        return market.field(name)
    supported = (
        name in _MATRIX_COMPUTED_FEATURES
        or (name.startswith("ma") and name.endswith("_bias") and name[2:-5].isdigit())
        or (name.startswith("ema") and name.endswith("_bias") and name[3:-5].isdigit())
        or (name.startswith("ma") and name[2:].isdigit())
        or (name.startswith("ema") and name[3:].isdigit())
        or (name.startswith("rsi_") and name[4:].isdigit())
        or (name.startswith("momentum_") and name.endswith("d"))
    )
    if not supported:
        raise ValueError(f"unsupported matrix feature: {name}")
    inputs = (market.close,)
    if name in {"vwap_bias", "log_float_mv"}:
        inputs += (
            market.fields.get("raw_close", market.close), market.volume,
            market.field("amount" if name == "vwap_bias" else "turnover_rate"),
        )
    with _activate_valid_bar_index(market.valid_bars):
        return _cached_matrix_operation(
            "matrix_feature",
            inputs,
            {"name": name},
            lambda: _compute_matrix_feature(market, name, resolve_feature or matrix_feature),
        )


def _compute_matrix_feature(
    market: MarketDataMatrix,
    name: str,
    resolve_feature: Callable[[MarketDataMatrix, str], np.ndarray],
) -> np.ndarray:
    close_valid = np.isfinite(market.close)
    if name == "prev_close":
        return valid_shift(market.close, 1, close_valid)
    if name == "change_pct":
        return _valid_return_over_bars(market.close, close_valid, 1)
    if name == "change_amount":
        previous = valid_shift(market.close, 1, close_valid)
        out = np.full(market.shape, np.nan, dtype=np.float32)
        np.subtract(market.close, previous, out=out, where=np.isfinite(previous))
        return out
    if name == "amplitude":
        previous = valid_shift(market.close, 1, close_valid)
        out = np.full(market.shape, np.nan, dtype=np.float32)
        np.divide(
            market.high - market.low,
            previous,
            out=out,
            where=np.isfinite(previous) & (previous > 0),
        )
        return out
    if name.startswith("momentum_") and name.endswith("d"):
        try:
            bars = int(name.removeprefix("momentum_").removesuffix("d"))
        except ValueError as exc:
            raise ValueError(f"unsupported matrix feature: {name}") from exc
        return _valid_return_over_bars(market.close, close_valid, bars)
    if name.startswith("ma") and name.endswith("_bias"):
        period = int(name.removeprefix("ma").removesuffix("_bias"))
        return _matrix_relative(market.close, valid_rolling_mean(market.close, close_valid, period))
    if name.startswith("ema") and name.endswith("_bias"):
        period = int(name.removeprefix("ema").removesuffix("_bias"))
        return _matrix_relative(market.close, _matrix_ema(market.close, close_valid, period))
    if name.startswith("ma") and name[2:].isdigit():
        return valid_rolling_mean(market.close, close_valid, int(name[2:]))
    if name.startswith("ema") and name[3:].isdigit():
        return _matrix_ema(market.close, close_valid, int(name[3:]))
    if name in {"macd_dif", "macd_dea", "macd_hist"}:
        dif, dea = _matrix_macd(market.close, close_valid)
        if name == "macd_dif":
            return dif
        if name == "macd_dea":
            return dea
        return ((dif - dea) * np.float32(2.0)).astype(np.float32, copy=False)
    if name in {"macd_dif_pct", "macd_dea_pct", "macd_hist_pct"}:
        source = resolve_feature(market, name.removesuffix("_pct"))
        return _matrix_ratio(source, market.close)
    if name in {"vol_ratio_5d", "vol_ratio_10d"}:
        window = 5 if name == "vol_ratio_5d" else 10
        volume_valid = close_valid & np.isfinite(market.volume)
        previous_volume = valid_shift(market.volume, 1, volume_valid)
        previous_mean = valid_rolling_mean(
            previous_volume,
            np.isfinite(previous_volume),
            window,
        )
        return _matrix_ratio(market.volume, previous_mean)
    if name in {"vol_ma5", "vol_ma10"}:
        window = 5 if name == "vol_ma5" else 10
        volume_valid = close_valid & np.isfinite(market.volume)
        return valid_rolling_mean(market.volume, volume_valid, window)
    if name == "vol_trend_5_10":
        return _matrix_relative(
            resolve_feature(market, "vol_ma5"),
            resolve_feature(market, "vol_ma10"),
        )
    if name == "turnover_ratio_5d":
        turnover = market.field("turnover_rate")
        valid = close_valid & np.isfinite(turnover)
        previous = valid_shift(turnover, 1, valid)
        return _matrix_relative(
            turnover,
            valid_rolling_mean(previous, np.isfinite(previous), 5),
        )
    if name == "log_amount":
        amount = market.field("amount")
        out = np.full(market.shape, np.nan, dtype=np.float32)
        valid = close_valid & np.isfinite(amount) & (amount >= 0)
        np.log(amount + np.float32(1.0), out=out, where=valid)
        return out
    if name == "amount_ratio_5d":
        amount = market.field("amount")
        valid = close_valid & np.isfinite(amount)
        previous = valid_shift(amount, 1, valid)
        return _matrix_relative(
            amount,
            valid_rolling_mean(previous, np.isfinite(previous), 5),
        )
    if name == "boll_upper" or name == "boll_lower":
        middle = valid_rolling_mean(market.close, close_valid, 20)
        deviation = valid_rolling_std(market.close, close_valid, 20, ddof=1)
        offset = np.float32(2.0) * deviation
        return middle + offset if name == "boll_upper" else middle - offset
    if name == "boll_position":
        return _matrix_ratio(
            market.close - resolve_feature(market, "boll_lower"),
            resolve_feature(market, "boll_upper") - resolve_feature(market, "boll_lower"),
        )
    if name == "boll_width":
        return _matrix_ratio(
            resolve_feature(market, "boll_upper") - resolve_feature(market, "boll_lower"),
            resolve_feature(market, "ma20"),
        )
    if name == "high_60d":
        return valid_rolling_max(market.close, close_valid, 60)
    if name == "low_60d":
        return valid_rolling_min(market.close, close_valid, 60)
    if name == "annual_vol_20d":
        daily = _valid_return_over_bars(market.close, close_valid, 1)
        return valid_rolling_std(
            daily,
            np.isfinite(daily),
            20,
            ddof=1,
        ) * np.float32(252 ** 0.5)
    if name in {"kdj_k", "kdj_d", "kdj_j"}:
        low_valid = close_valid & np.isfinite(market.low)
        high_valid = close_valid & np.isfinite(market.high)
        low_9 = valid_rolling_min(market.low, low_valid, 9)
        high_9 = valid_rolling_max(market.high, high_valid, 9)
        rsv = _matrix_ratio(market.close - low_9, high_9 - low_9) * np.float32(100.0)
        k = valid_ewm_adjust_false(rsv, np.isfinite(rsv), alpha=1.0 / 3.0)
        if name == "kdj_k":
            return k
        d = valid_ewm_adjust_false(k, np.isfinite(k), alpha=1.0 / 3.0)
        if name == "kdj_d":
            return d
        return (np.float32(3.0) * k - np.float32(2.0) * d).astype(np.float32, copy=False)
    if name in {"atr_14", "atr_pct"}:
        previous = valid_shift(market.close, 1, close_valid)
        true_range = np.fmax.reduce([
            market.high - market.low,
            np.abs(market.high - previous),
            np.abs(market.low - previous),
        ]).astype(np.float32, copy=False)
        true_range[~close_valid] = np.nan
        atr = valid_ewm_adjust_false(true_range, np.isfinite(true_range), alpha=1.0 / 14.0)
        return atr if name == "atr_14" else _matrix_ratio(atr, market.close)
    if name.startswith("rsi_") and name[4:].isdigit():
        window = int(name[4:])
        delta = market.close - valid_shift(market.close, 1, close_valid)
        delta_valid = close_valid
        gain = np.where(delta > 0, delta, 0.0).astype(np.float32, copy=False)
        loss = np.where(delta < 0, -delta, 0.0).astype(np.float32, copy=False)
        gain[~delta_valid] = np.nan
        loss[~delta_valid] = np.nan
        average_gain = valid_ewm_adjust_false(
            gain,
            delta_valid,
            alpha=1.0 / window,
        )
        average_loss = valid_ewm_adjust_false(
            loss,
            delta_valid,
            alpha=1.0 / window,
        )
        denominator = np.where(average_loss == 0, np.float32(1e-12), average_loss)
        out = np.full(market.shape, np.nan, dtype=np.float32)
        np.divide(average_gain, denominator, out=out, where=np.isfinite(denominator))
        out = np.float32(100.0) - np.float32(100.0) / (np.float32(1.0) + out)
        return out
    if name == "gap_return":
        return _matrix_relative(market.open, valid_shift(market.close, 1, close_valid))
    if name == "intraday_return":
        return _matrix_relative(market.close, market.open)
    if name == "close_position":
        return _matrix_ratio(market.close - market.low, market.high - market.low)
    if name == "distance_to_high_60d":
        return _matrix_relative(market.close, resolve_feature(market, "high_60d"))
    if name == "distance_from_low_60d":
        return _matrix_relative(market.close, resolve_feature(market, "low_60d"))
    if name == "max_ret_20d":
        daily = resolve_feature(market, "change_pct")
        return valid_rolling_max(daily, np.isfinite(daily), 20)
    if name == "ret_skew_20d":
        return _matrix_rolling_skew(resolve_feature(market, "change_pct"), 20)
    if name == "up_days_20d":
        daily = resolve_feature(market, "change_pct")
        up = np.where(daily > 0, np.float32(1.0), np.float32(0.0)).astype(np.float32)
        up[~np.isfinite(daily)] = np.nan
        return valid_rolling_sum(up, np.isfinite(up), 20)
    if name == "amihud_20d":
        daily = resolve_feature(market, "change_pct")
        amount = market.field("amount")
        amount_yi = amount / np.float32(1e8)
        illiquidity = _matrix_ratio(np.abs(daily), amount_yi)
        return valid_rolling_mean(
            illiquidity,
            close_valid & np.isfinite(illiquidity),
            20,
        )
    if name == "turnover_z_60d":
        turnover = market.field("turnover_rate")
        valid = close_valid & np.isfinite(turnover)
        previous = valid_shift(turnover, 1, valid)
        baseline_valid = np.isfinite(previous)
        mean = valid_rolling_mean(previous, baseline_valid, 60)
        std = valid_rolling_std(previous, baseline_valid, 60, ddof=1)
        deviation = _matrix_ratio(turnover - mean, std)
        deviation[np.isfinite(std) & (std <= 0)] = np.nan
        return deviation
    if name == "vol_price_corr_20d":
        daily = resolve_feature(market, "change_pct")
        return _matrix_rolling_corr(daily, market.volume, close_valid, 20)
    if name == "vwap_bias":
        quote = market.fields.get("raw_close", market.close)
        amount = market.field("amount")
        shares = market.volume * np.float32(100.0)
        valid = np.isfinite(quote) & np.isfinite(amount) & (market.volume > 0) & (amount > 0)
        vwap = np.full(market.shape, np.nan, dtype=np.float32)
        np.divide(amount, shares, out=vwap, where=valid)
        return _matrix_relative(quote, vwap)
    if name == "vol_trend_5_60":
        volume_valid = close_valid & np.isfinite(market.volume)
        fast = valid_rolling_mean(market.volume, volume_valid, 5)
        slow = valid_rolling_mean(market.volume, volume_valid, 60)
        return _matrix_relative(fast, slow)
    if name in {"limit_up_count_20d", "limit_up_count_60d"}:
        window = 20 if name == "limit_up_count_20d" else 60
        consecutive = market.field("consecutive_limit_ups")
        hits = np.where(np.isfinite(consecutive) & (consecutive > 0), np.float32(1.0), np.float32(0.0))
        hits = hits.astype(np.float32)
        return valid_rolling_sum(hits, close_valid, window)
    # --- 扩充批次 (2026-09-05): numpy 内核实现, 口径与 strategy/scoring.py 一致 ---
    if name == "log_float_mv":
        quote = market.fields.get("raw_close", market.close)
        turnover = market.field("turnover_rate")
        valid = np.isfinite(quote) & np.isfinite(turnover) & (turnover > 0) & (market.volume > 0)
        out = np.full(market.shape, np.nan, dtype=np.float32)
        np.multiply(quote, market.volume, out=out, where=valid)
        np.divide(out, turnover, out=out, where=valid)
        np.log(out, out=out, where=valid)
        return out
    if name == "mom_accel_20_60" or name == "kdj_kd_diff":
        left, right = (
            (resolve_feature(market, "momentum_20d"), resolve_feature(market, "momentum_60d"))
            if name == "mom_accel_20_60"
            else (resolve_feature(market, "kdj_k"), resolve_feature(market, "kdj_d"))
        )
        out = np.full(market.shape, np.nan, dtype=np.float32)
        np.subtract(left, right, out=out, where=np.isfinite(left) & np.isfinite(right))
        return out
    if name == "rsi_14_delta_5d":
        rsi = resolve_feature(market, "rsi_14")
        return rsi - valid_shift(rsi, 5, np.isfinite(rsi))
    if name == "overnight_ret_20d":
        overnight = _matrix_relative(market.open, resolve_feature(market, "prev_close"))
        return valid_rolling_sum(overnight, np.isfinite(overnight), 20)
    if name == "intraday_ret_20d":
        intraday = _matrix_relative(market.close, market.open)
        return valid_rolling_sum(intraday, np.isfinite(intraday), 20)
    if name == "downside_vol_20d":
        daily = resolve_feature(market, "change_pct")
        downside = np.where(
            np.isfinite(daily), np.minimum(daily, np.float32(0.0)), np.nan,
        ).astype(np.float32)
        mean_sq = valid_rolling_mean(np.square(downside, dtype=np.float32), np.isfinite(downside), 20)
        out = np.full(market.shape, np.nan, dtype=np.float32)
        np.sqrt(mean_sq, out=out, where=np.isfinite(mean_sq))
        return out
    if name == "vol_regime_5_60":
        daily = resolve_feature(market, "change_pct")
        valid = np.isfinite(daily)
        return _matrix_ratio(
            valid_rolling_std(daily, valid, 5, ddof=1),
            valid_rolling_std(daily, valid, 60, ddof=1),
        )
    if name == "amplitude_trend_20_60":
        amplitude = resolve_feature(market, "amplitude")
        valid = np.isfinite(amplitude)
        return _matrix_relative(
            valid_rolling_mean(amplitude, valid, 20),
            valid_rolling_mean(amplitude, valid, 60),
        )
    if name == "obv_trend_20d":
        daily = resolve_feature(market, "change_pct")
        volume_valid = close_valid & np.isfinite(market.volume)
        signed = np.where(
            np.isfinite(daily), np.sign(daily) * market.volume, np.nan,
        ).astype(np.float32)
        total = valid_rolling_sum(signed, volume_valid & np.isfinite(daily), 20)
        scale = valid_rolling_mean(market.volume, volume_valid, 20) * np.float32(20.0)
        return _matrix_ratio(total, scale)
    if name == "amount_mean_20d":
        amount = market.field("amount")
        return valid_rolling_mean(amount / np.float32(1e8), np.isfinite(amount), 20)
    if name == "turnover_mean_20d" or name == "turnover_std_20d":
        turnover = market.field("turnover_rate")
        valid = np.isfinite(turnover)
        mean = valid_rolling_mean(turnover, valid, 20)
        if name == "turnover_mean_20d":
            return mean
        return _matrix_ratio(valid_rolling_std(turnover, valid, 20, ddof=1), mean)
    if name == "position_240d":
        high = valid_rolling_max(market.close, close_valid, 240)
        low = valid_rolling_min(market.close, close_valid, 240)
        return _matrix_ratio(market.close - low, high - low)
    if name == "distance_to_high_240d":
        return _matrix_relative(market.close, valid_rolling_max(market.close, close_valid, 240))
    raise ValueError(f"unsupported matrix feature: {name}")


def _matrix_rolling_skew(values: np.ndarray, window: int) -> np.ndarray:
    valid = np.isfinite(values)
    first = valid_rolling_mean(values, valid, window)
    second = valid_rolling_mean(np.square(values, dtype=np.float32), valid, window)
    third = valid_rolling_mean(
        (values * values * values).astype(np.float32), valid, window
    )
    variance = second - np.square(first, dtype=np.float32)
    central_third = (
        third
        - np.float32(3.0) * first * second
        + np.float32(2.0) * np.power(first, 3)
    )
    out = _matrix_ratio(central_third, np.sqrt(np.power(variance, 3)))
    out[np.isfinite(variance) & (variance <= 0)] = np.nan
    return out


def _matrix_rolling_corr(
    left: np.ndarray, right: np.ndarray, valid_mask: np.ndarray, window: int
) -> np.ndarray:
    valid = valid_mask & np.isfinite(left) & np.isfinite(right)
    product = (left * right).astype(np.float32)
    mean_left = valid_rolling_mean(left, valid, window)
    mean_right = valid_rolling_mean(right, valid, window)
    mean_product = valid_rolling_mean(product, valid, window)
    mean_left_sq = valid_rolling_mean(
        np.square(left, dtype=np.float32), valid, window
    )
    mean_right_sq = valid_rolling_mean(
        np.square(right, dtype=np.float32), valid, window
    )
    covariance = mean_product - mean_left * mean_right
    variance_left = mean_left_sq - np.square(mean_left, dtype=np.float32)
    variance_right = mean_right_sq - np.square(mean_right, dtype=np.float32)
    denominator = np.sqrt(variance_left * variance_right)
    out = _matrix_ratio(covariance, denominator)
    degenerate = (
        np.isfinite(variance_left)
        & np.isfinite(variance_right)
        & ((variance_left <= 0) | (variance_right <= 0))
    )
    out[degenerate] = np.nan
    return out


def _matrix_ema(values: np.ndarray, valid: np.ndarray, period: int) -> np.ndarray:
    return valid_ewm_adjust_false(values, valid, alpha=2.0 / (period + 1.0))


def _matrix_macd(values: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    dif = _matrix_ema(values, valid, 12) - _matrix_ema(values, valid, 26)
    dif = dif.astype(np.float32, copy=False)
    dea = valid_ewm_adjust_false(dif, np.isfinite(dif), alpha=2.0 / 10.0)
    return dif, dea


def _matrix_ratio(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    out = np.full(numerator.shape, np.nan, dtype=np.float32)
    valid = np.isfinite(numerator) & np.isfinite(denominator) & (denominator != 0)
    np.divide(numerator, denominator, out=out, where=valid)
    return out


def _matrix_relative(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    out = _matrix_ratio(numerator, denominator)
    out[np.isfinite(out)] -= np.float32(1.0)
    return out


def _valid_return_over_bars(
    values: np.ndarray,
    valid_mask: np.ndarray,
    bars: int,
) -> np.ndarray:
    previous = valid_shift(values, bars, valid_mask)
    out = np.full(values.shape, np.nan, dtype=np.float32)
    np.divide(values, previous, out=out, where=np.isfinite(previous) & (previous != 0))
    out -= np.float32(1.0)
    return out
