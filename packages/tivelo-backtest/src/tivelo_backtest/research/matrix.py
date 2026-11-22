"""Shared immutable matrices and encoding; market rules are explicitly injected."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import numpy as np
import polars as pl
from tivelo_contracts.errors import ErrorCode, TiveloError

from .minute_trigger import build_minute_exit_reference

# Legacy payload identifier; no dynamic projection implementation lives here.
LEGACY_PRICE_SCALE = "_backtest_cum_factor"


@dataclass(frozen=True)
class ValidBarIndex:
    """Asset-major CSR index of effective market bars."""

    shape: tuple[int, int]
    offsets: np.ndarray
    rows: np.ndarray

    @property
    def nbytes(self) -> int:
        return int(self.offsets.nbytes + self.rows.nbytes)


def _build_valid_bar_index(valid_mask: np.ndarray) -> ValidBarIndex:
    valid = np.asarray(valid_mask, dtype=bool)
    if valid.ndim != 2:
        raise ValueError("valid bar index requires a 2D mask")
    counts = np.count_nonzero(valid, axis=0).astype(np.int64, copy=False)
    offsets = np.empty(valid.shape[1] + 1, dtype=np.int64)
    offsets[0] = 0
    np.cumsum(counts, out=offsets[1:])
    rows = np.empty(int(offsets[-1]), dtype=np.int32)
    for asset_id in range(valid.shape[1]):
        start = int(offsets[asset_id])
        stop = int(offsets[asset_id + 1])
        rows[start:stop] = np.flatnonzero(valid[:, asset_id]).astype(
            np.int32,
            copy=False,
        )
    offsets.flags.writeable = False
    rows.flags.writeable = False
    return ValidBarIndex(shape=valid.shape, offsets=offsets, rows=rows)


@dataclass(frozen=True)
class MarketDataMatrix:
    """Compact base market data shared by matrix-native strategies and matchers."""

    timestamps: np.ndarray
    timestamp_labels: tuple[str, ...]
    session_ids: np.ndarray
    symbols: tuple[str, ...]
    names: tuple[str, ...]

    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    tradable: np.ndarray
    limit_up_locked: np.ndarray
    limit_down_locked: np.ndarray
    fields: Mapping[str, np.ndarray]
    cache_status: str = "memory"
    cache_path: str | None = None
    cache_lease: Any | None = field(default=None, compare=False, repr=False)
    vector_fields: frozenset[str] = field(default_factory=frozenset)
    cache_timing_ms: Mapping[str, float] = field(default_factory=dict)
    _valid_bars: ValidBarIndex | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    @property
    def shape(self) -> tuple[int, int]:
        return self.open.shape

    @property
    def nbytes(self) -> int:
        arrays = [
            self.timestamps,
            self.session_ids,
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.tradable,
            self.limit_up_locked,
            self.limit_down_locked,
            *self.fields.values(),
        ]
        index_bytes = self._valid_bars.nbytes if self._valid_bars is not None else 0
        return int(sum(array.nbytes for array in arrays) + index_bytes)

    @property
    def valid_bars(self) -> ValidBarIndex:
        index = self._valid_bars
        if index is None:
            index = _build_valid_bar_index(np.isfinite(self.close))
            object.__setattr__(self, "_valid_bars", index)
        return index

    def field(self, name: str) -> np.ndarray:
        if name == "open":
            return self.open
        if name == "high":
            return self.high
        if name == "low":
            return self.low
        if name == "close":
            return self.close
        if name == "volume":
            return self.volume
        try:
            return self.fields[name]
        except KeyError as exc:
            raise ValueError(f"MarketDataMatrix missing field: {name}") from exc


@dataclass(frozen=True)
class SignalMatrix:
    """Strategy output before execution delays are applied."""

    entry: np.ndarray
    exit: np.ndarray
    score: np.ndarray
    entry_signal_code: np.ndarray
    exit_signal_code: np.ndarray
    entry_signal_ids: tuple[str, ...] = ()
    exit_signal_ids: tuple[str, ...] = ()

    @property
    def shape(self) -> tuple[int, int]:
        return self.entry.shape

    @property
    def nbytes(self) -> int:
        return int(
            sum(value.nbytes for value in self.__dict__.values() if isinstance(value, np.ndarray))
        )


@dataclass(frozen=True)
class MarketMatrix:
    """Execution matrix consumed by the Python matcher and future Numba kernel."""

    timestamps: np.ndarray
    timestamp_labels: tuple[str, ...]
    session_ids: np.ndarray
    symbols: tuple[str, ...]
    names: tuple[str, ...]

    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    score: np.ndarray
    entry: np.ndarray
    exit: np.ndarray
    tradable: np.ndarray
    limit_up_locked: np.ndarray
    limit_down_locked: np.ndarray
    reference_price: np.ndarray

    entry_signal_time: np.ndarray
    exit_signal_time: np.ndarray
    entry_signal_code: np.ndarray
    exit_signal_code: np.ndarray
    entry_signal_ids: tuple[str, ...]
    exit_signal_ids: tuple[str, ...]
    # 逐格入场价覆盖 (time x asset, NaN=回退 open/close 惯例)。分钟策略回测用:
    # 信号在盘中第 m 根触发, 入场价 = 触发分钟收盘价, 而非当日开盘/收盘。
    entry_price: np.ndarray | None = None
    price_scale: np.ndarray | None = None

    def scale_at(self, time_id: int, asset_id: int) -> float:
        return 1.0 if self.price_scale is None else float(self.price_scale[time_id, asset_id])

    @property
    def shape(self) -> tuple[int, int]:
        return self.open.shape

    @property
    def nbytes(self) -> int:
        return int(
            sum(value.nbytes for value in self.__dict__.values() if isinstance(value, np.ndarray))
        )


def build_market_data_matrix(
    panel: pl.DataFrame,
    *,
    field_columns: set[str] | frozenset[str] | None = None,
    price_limit_provider: Callable | None = None,
) -> MarketDataMatrix:
    """Encode a long base panel into immutable ``time x asset`` arrays."""
    if panel.is_empty():
        raise ValueError("cannot build MarketDataMatrix from an empty panel")

    timestamp_col, unique_timestamps, symbol_values, time_id, asset_id = _encode_axes(panel)
    shape = (len(unique_timestamps), len(symbol_values))

    def float_matrix(
        column: str,
        default: float = np.nan,
        null_fill: float | None = None,
    ) -> np.ndarray:
        return _float_matrix(panel, column, shape, time_id, asset_id, default, null_fill)

    open_ = float_matrix("open")
    high = float_matrix("high")
    low = float_matrix("low")
    close = float_matrix("close")
    volume = float_matrix("volume", 0.0 if "volume" in panel.columns else 1.0, 0.0)
    limit_up_locked = _bool_matrix(panel, "signal_limit_up", shape, time_id, asset_id)
    limit_down_locked = _bool_matrix(panel, "signal_limit_down", shape, time_id, asset_id)
    tradable = _tradable_matrix(open_, high, low, close, volume)

    core_columns = {
        timestamp_col,
        "symbol",
        "name",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "signal_limit_up",
        "signal_limit_down",
    }
    wanted_fields = set(field_columns or ()) - core_columns
    fields: dict[str, np.ndarray] = {}
    for column in sorted(wanted_fields):
        if column == "price_limit_pct":
            continue
        if column in panel.columns and panel[column].dtype.is_numeric():
            fields[column] = float_matrix(column)
        elif column == "raw_close":
            # A live quote is already an unadjusted price when no separate raw
            # field is supplied. Keep this explicit compatibility contract for
            # strategies that estimate market value from historical raw prices.
            fields[column] = np.array(close, copy=True)

    names = [""] * len(symbol_values)
    if "name" in panel.columns:
        row_names = panel["name"].fill_null("").cast(pl.Utf8).to_numpy()
        for row, aid in enumerate(asset_id):
            if not names[int(aid)] and row_names[row]:
                names[int(aid)] = str(row_names[row])

    if "price_limit_pct" in wanted_fields:
        if price_limit_provider is None:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Price-limit matrix requires an explicit market rule provider",
            )
        trading_dates = unique_timestamps.cast(pl.Date).to_list()
        fields["price_limit_pct"] = price_limit_provider(
            np.empty(shape, dtype=np.float32),
            trading_dates,
            symbol_values,
            names,
            valid=np.isfinite(close),
        )

    timestamp_labels = tuple(str(value)[:19] for value in unique_timestamps.to_numpy())
    timestamps = _timestamp_int64(unique_timestamps)
    session_dates = unique_timestamps.cast(pl.Date).to_numpy()
    session_values = np.unique(session_dates)
    session_ids = np.searchsorted(session_values, session_dates).astype(np.int32)

    arrays = (
        timestamps,
        session_ids,
        open_,
        high,
        low,
        close,
        volume,
        tradable,
        limit_up_locked,
        limit_down_locked,
        *fields.values(),
    )
    _make_read_only(*arrays)

    return MarketDataMatrix(
        timestamps=timestamps,
        timestamp_labels=timestamp_labels,
        session_ids=session_ids,
        symbols=tuple(str(value) for value in symbol_values),
        names=tuple(names),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        tradable=tradable,
        limit_up_locked=limit_up_locked,
        limit_down_locked=limit_down_locked,
        fields=MappingProxyType(fields),
    )


def make_signal_matrix(
    shape: tuple[int, int],
    *,
    entry: np.ndarray | None = None,
    exit: np.ndarray | None = None,
    score: np.ndarray | None = None,
    entry_signal_code: np.ndarray | None = None,
    exit_signal_code: np.ndarray | None = None,
    entry_signal_ids: tuple[str, ...] = (),
    exit_signal_ids: tuple[str, ...] = (),
) -> SignalMatrix:
    """Create a compact read-only signal matrix with canonical dtypes."""
    entry_array = _coerce_array(entry, shape, np.uint8, 0)
    exit_array = _coerce_array(exit, shape, np.uint8, 0)
    score_array = _coerce_array(score, shape, np.float32, 0.0)
    entry_codes = _coerce_array(entry_signal_code, shape, np.int16, -1)
    exit_codes = _coerce_array(exit_signal_code, shape, np.int16, -1)
    return _finalize_signal_matrix(
        entry_array,
        exit_array,
        score_array,
        entry_codes,
        exit_codes,
        entry_signal_ids=entry_signal_ids,
        exit_signal_ids=exit_signal_ids,
    )


def _finalize_signal_matrix(
    entry: np.ndarray,
    exit_: np.ndarray,
    score: np.ndarray,
    entry_signal_code: np.ndarray,
    exit_signal_code: np.ndarray,
    *,
    entry_signal_ids: tuple[str, ...] = (),
    exit_signal_ids: tuple[str, ...] = (),
) -> SignalMatrix:
    shape = entry.shape
    _make_read_only(entry, exit_, score, entry_signal_code, exit_signal_code)
    result = SignalMatrix(
        entry=entry,
        exit=exit_,
        score=score,
        entry_signal_code=entry_signal_code,
        exit_signal_code=exit_signal_code,
        entry_signal_ids=tuple(entry_signal_ids),
        exit_signal_ids=tuple(exit_signal_ids),
    )
    validate_signal_matrix(result, shape)
    return result


def validate_signal_matrix(signals: SignalMatrix, shape: tuple[int, int]) -> None:
    """Fail explicitly when a matrix strategy violates the shared output contract."""
    specs = {
        "entry": (signals.entry, np.dtype(np.uint8)),
        "exit": (signals.exit, np.dtype(np.uint8)),
        "score": (signals.score, np.dtype(np.float32)),
        "entry_signal_code": (signals.entry_signal_code, np.dtype(np.int16)),
        "exit_signal_code": (signals.exit_signal_code, np.dtype(np.int16)),
    }
    for name, (array, dtype) in specs.items():
        if not isinstance(array, np.ndarray):
            raise TypeError(f"SignalMatrix.{name} must be a numpy array")
        if array.shape != shape:
            raise ValueError(
                f"SignalMatrix.{name} shape {array.shape} does not match market {shape}"
            )
        if array.dtype != dtype:
            raise TypeError(f"SignalMatrix.{name} must use {dtype}, got {array.dtype}")
        if array.flags.writeable:
            raise ValueError(f"SignalMatrix.{name} must be read-only")
    if not np.isfinite(signals.score).all():
        raise ValueError("SignalMatrix.score must contain only finite values")


def build_market_matrix_from_signals(
    market: MarketDataMatrix,
    signals: SignalMatrix,
    *,
    entry_delay_bars: int = 0,
    exit_delay_bars: int = 0,
    reference_price: np.ndarray | None = None,
    minute_exit_trigger: bool = False,
    entry_price_override: np.ndarray | None = None,
) -> MarketMatrix:
    """Combine base data and strategy signals into the matcher input matrix."""
    if entry_delay_bars not in (0, 1) or exit_delay_bars not in (0, 1):
        raise ValueError("phase-two MarketMatrix supports only zero or one bar delay")
    validate_signal_matrix(signals, market.shape)
    if entry_price_override is not None and entry_price_override.shape != market.shape:
        raise ValueError("entry_price_override shape does not match MarketDataMatrix")

    present = _present_matrix(market.open, market.high, market.low, market.close, market.volume)
    entry, entry_signal_time, entry_signal_code = _delay_signal_matrix(
        signals.entry,
        signals.entry_signal_code,
        present,
        entry_delay_bars,
    )
    exit_, exit_signal_time, exit_signal_code = _delay_signal_matrix(
        signals.exit,
        signals.exit_signal_code,
        present,
        exit_delay_bars,
    )

    if reference_price is not None:
        if reference_price.shape != market.shape:
            raise ValueError("reference_price shape does not match MarketDataMatrix")
        resolved_reference_price = np.array(reference_price, dtype=np.float32, copy=True)
    else:
        # polars_expr / 长表路径不传 reference_price, 回退用 panel 均线。
        # enriched maN 含当根收盘; 盘中成交不可能知道当日收盘, 代数剔除
        # 与 build_minute_entry_reference / 卖出侧同一纪律 (#388 只修了
        # matrix_native 显式传入的那条路径)。
        resolved_reference_price = np.full(market.shape, np.nan, dtype=np.float32)
        with np.errstate(divide="ignore", invalid="ignore"):
            for window, column in ((5, "ma5"), (10, "ma10"), (20, "ma20")):
                values = market.fields.get(column)
                if values is None:
                    continue
                stripped = (float(window) * values - market.close) / float(window - 1)
                use = (
                    ~np.isfinite(resolved_reference_price) & np.isfinite(stripped) & (stripped > 0)
                )
                resolved_reference_price[use] = stripped[use].astype(np.float32, copy=False)

    if minute_exit_trigger:
        trigger_reference = build_minute_exit_reference(
            market.close,
            market.fields,
            signals.exit_signal_code,
            signals.exit_signal_ids,
        )
        trigger_mask = signals.exit != 0
        resolved_reference_price[trigger_mask] = trigger_reference[trigger_mask]

    _make_read_only(
        entry,
        exit_,
        resolved_reference_price,
        entry_signal_time,
        exit_signal_time,
        entry_signal_code,
        exit_signal_code,
    )

    return MarketMatrix(
        timestamps=market.timestamps,
        timestamp_labels=market.timestamp_labels,
        session_ids=market.session_ids,
        symbols=market.symbols,
        names=market.names,
        open=market.open,
        high=market.high,
        low=market.low,
        close=market.close,
        volume=market.volume,
        score=signals.score,
        entry=entry,
        exit=exit_,
        tradable=market.tradable,
        limit_up_locked=market.limit_up_locked,
        limit_down_locked=market.limit_down_locked,
        reference_price=resolved_reference_price,
        entry_signal_time=entry_signal_time,
        exit_signal_time=exit_signal_time,
        entry_signal_code=entry_signal_code,
        exit_signal_code=exit_signal_code,
        entry_signal_ids=signals.entry_signal_ids,
        exit_signal_ids=signals.exit_signal_ids,
        price_scale=market.fields.get(LEGACY_PRICE_SCALE),
        entry_price=(
            np.array(entry_price_override, dtype=np.float32, copy=True)
            if entry_price_override is not None
            else None
        ),
    )


def build_market_matrix(
    panel: pl.DataFrame,
    entries: pl.Series | None,
    exits: pl.Series | None,
    *,
    entry_delay_bars: int = 0,
    exit_delay_bars: int = 0,
    entry_signal_ids: list[str] | None = None,
    exit_signal_ids: list[str] | None = None,
    minute_exit_trigger: bool = False,
) -> MarketMatrix:
    """Backward-compatible long-panel boundary used by legacy/Polars strategies."""
    if panel.is_empty():
        raise ValueError("cannot build MarketMatrix from an empty panel")
    encoding = encode_panel_matrix(
        panel,
        entry_signal_ids=entry_signal_ids,
        exit_signal_ids=exit_signal_ids,
    )
    score = (
        panel.get_column("score")
        if "score" in panel.columns and panel["score"].dtype.is_numeric()
        else None
    )
    return build_market_matrix_from_encoding(
        encoding,
        score=score,
        entries=entries,
        exits=exits,
        entry_delay_bars=entry_delay_bars,
        exit_delay_bars=exit_delay_bars,
        minute_exit_trigger=minute_exit_trigger,
    )


@dataclass(frozen=True)
class PanelMatrixEncoding:
    """长面板 → 矩阵的参数无关编码, 供优化器跨参数组复用 (P2a-2)。

    行情矩阵不带 score 场 — score 随参数组的 universe 掩码变化 (_apply_score
    把非候选行置空), 由 build_market_matrix_from_encoding 逐组散布进信号
    矩阵; 全部数组只读共享, 复用零拷贝。"""

    market: MarketDataMatrix
    time_id: np.ndarray
    asset_id: np.ndarray
    panel_rows: int
    entry_signal_code: np.ndarray
    entry_signal_ids: tuple[str, ...]
    exit_signal_code: np.ndarray
    exit_signal_ids: tuple[str, ...]
    # 编码所用的列序; 调用方与当组 sim_columns 比对, 不一致回退整建
    # (回退是优化放弃, 不是正确性闸门 — 整建结果总是正确)。
    sim_columns: tuple[str, ...]

    @property
    def shape(self) -> tuple[int, int]:
        return self.market.shape


def encode_panel_matrix(
    panel: pl.DataFrame,
    *,
    entry_signal_ids: list[str] | None = None,
    exit_signal_ids: list[str] | None = None,
) -> PanelMatrixEncoding:
    """编码面板的参数无关半边: 行情矩阵 + 轴 id + 信号码矩阵。

    score/entry/exit 掩码随参数组变化, 不在编码内 — 见
    build_market_matrix_from_encoding。"""
    market = build_market_data_matrix(
        panel, field_columns={"ma5", "ma10", "ma20", LEGACY_PRICE_SCALE} & set(panel.columns)
    )
    _, _, _, time_id, asset_id = _encode_axes(panel)
    shape = market.shape
    entry_codes, normalized_entry_ids = _signal_code_matrix(
        panel,
        entry_signal_ids,
        shape,
        time_id,
        asset_id,
    )
    exit_codes, normalized_exit_ids = _signal_code_matrix(
        panel,
        exit_signal_ids,
        shape,
        time_id,
        asset_id,
    )
    return PanelMatrixEncoding(
        market=market,
        time_id=time_id,
        asset_id=asset_id,
        panel_rows=len(panel),
        entry_signal_code=entry_codes,
        entry_signal_ids=normalized_entry_ids,
        exit_signal_code=exit_codes,
        exit_signal_ids=normalized_exit_ids,
        sim_columns=tuple(panel.columns),
    )


def build_market_matrix_from_encoding(
    encoding: PanelMatrixEncoding,
    *,
    score: pl.Series | None,
    entries: pl.Series | None,
    exits: pl.Series | None,
    entry_delay_bars: int = 0,
    exit_delay_bars: int = 0,
    minute_exit_trigger: bool = False,
) -> MarketMatrix:
    """共享编码 + 参数相关的 score/entry/exit → MarketMatrix (优化器试算路径)。

    散布与合成和 build_market_matrix 走同一套件 (_scatter_bool_series /
    make_signal_matrix / build_market_matrix_from_signals), score 的
    非有限值→nan 语义与 _float_matrix 一致。"""
    shape = encoding.shape
    raw_entry = _scatter_bool_series(
        entries,
        encoding.panel_rows,
        shape,
        encoding.time_id,
        encoding.asset_id,
    )
    raw_exit = _scatter_bool_series(
        exits,
        encoding.panel_rows,
        shape,
        encoding.time_id,
        encoding.asset_id,
    )
    score_matrix = None
    if score is not None and len(score) == encoding.panel_rows:
        score_matrix = np.full(shape, np.nan, dtype=np.float32)
        values = np.array(
            score.cast(pl.Float32, strict=False).to_numpy(),
            dtype=np.float32,
            copy=True,
        )
        values[~np.isfinite(values)] = np.nan
        score_matrix[encoding.time_id, encoding.asset_id] = values
    signals = make_signal_matrix(
        shape,
        entry=raw_entry,
        exit=raw_exit,
        score=np.nan_to_num(score_matrix, nan=0.0) if score_matrix is not None else None,
        entry_signal_code=encoding.entry_signal_code,
        exit_signal_code=encoding.exit_signal_code,
        entry_signal_ids=encoding.entry_signal_ids,
        exit_signal_ids=encoding.exit_signal_ids,
    )
    return build_market_matrix_from_signals(
        encoding.market,
        signals,
        entry_delay_bars=entry_delay_bars,
        exit_delay_bars=exit_delay_bars,
        minute_exit_trigger=minute_exit_trigger,
    )


def slice_market_data_matrix(market: MarketDataMatrix, start: int, stop: int) -> MarketDataMatrix:
    """Return a read-only time slice without copying the underlying market arrays."""
    fields = {name: values[start:stop] for name, values in market.fields.items()}
    result = MarketDataMatrix(
        timestamps=market.timestamps[start:stop],
        timestamp_labels=market.timestamp_labels[start:stop],
        session_ids=market.session_ids[start:stop],
        symbols=market.symbols,
        names=market.names,
        open=market.open[start:stop],
        high=market.high[start:stop],
        low=market.low[start:stop],
        close=market.close[start:stop],
        volume=market.volume[start:stop],
        tradable=market.tradable[start:stop],
        limit_up_locked=market.limit_up_locked[start:stop],
        limit_down_locked=market.limit_down_locked[start:stop],
        fields=MappingProxyType(fields),
        cache_status=market.cache_status,
        cache_path=market.cache_path,
        cache_lease=market.cache_lease,
        vector_fields=market.vector_fields,
        cache_timing_ms=market.cache_timing_ms,
    )
    _make_read_only(
        result.timestamps,
        result.session_ids,
        result.open,
        result.high,
        result.low,
        result.close,
        result.volume,
        result.tradable,
        result.limit_up_locked,
        result.limit_down_locked,
        *fields.values(),
    )
    return result


def slice_signal_matrix(signals: SignalMatrix, start: int, stop: int) -> SignalMatrix:
    return _finalize_signal_matrix(
        signals.entry[start:stop],
        signals.exit[start:stop],
        signals.score[start:stop],
        signals.entry_signal_code[start:stop],
        signals.exit_signal_code[start:stop],
        entry_signal_ids=signals.entry_signal_ids,
        exit_signal_ids=signals.exit_signal_ids,
    )


def _encode_axes(
    panel: pl.DataFrame,
) -> tuple[str, pl.Series, np.ndarray, np.ndarray, np.ndarray]:
    timestamp_col = "datetime" if "datetime" in panel.columns else "date"
    required = {timestamp_col, "symbol", "open", "high", "low", "close"}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"MarketDataMatrix missing columns: {sorted(missing)}")

    timestamp_series = panel[timestamp_col]
    unique_timestamps = timestamp_series.unique().sort()
    symbol_series = panel["symbol"].cast(pl.Utf8)
    unique_symbols = symbol_series.unique().sort()
    row_timestamps = timestamp_series.to_numpy()
    timestamp_values = unique_timestamps.to_numpy()
    row_symbols = symbol_series.to_numpy()
    symbol_values = unique_symbols.to_numpy()
    time_id = np.searchsorted(timestamp_values, row_timestamps).astype(np.int32)
    asset_id = np.searchsorted(symbol_values, row_symbols).astype(np.int32)
    keys = time_id.astype(np.int64) * len(symbol_values) + asset_id
    if np.unique(keys).size != len(panel):
        raise ValueError("MarketDataMatrix requires unique timestamp/symbol rows")
    return timestamp_col, unique_timestamps, symbol_values, time_id, asset_id


def _float_matrix(
    panel: pl.DataFrame,
    column: str,
    shape: tuple[int, int],
    time_id: np.ndarray,
    asset_id: np.ndarray,
    default: float = np.nan,
    null_fill: float | None = None,
) -> np.ndarray:
    out = np.full(shape, default, dtype=np.float32)
    if column not in panel.columns:
        return out
    values = np.array(
        panel[column].cast(pl.Float32, strict=False).to_numpy(),
        dtype=np.float32,
        copy=True,
    )
    values[~np.isfinite(values)] = np.nan if null_fill is None else null_fill
    out[time_id, asset_id] = values
    return out


def _timestamp_int64(series: pl.Series) -> np.ndarray:
    if series.dtype == pl.Date:
        return series.cast(pl.Datetime("ms")).cast(pl.Int64).to_numpy()
    if isinstance(series.dtype, pl.Datetime):
        return series.cast(pl.Datetime("ms")).cast(pl.Int64).to_numpy()
    return series.cast(pl.Int64, strict=False).to_numpy()


def _bool_matrix(
    panel: pl.DataFrame,
    column: str,
    shape: tuple[int, int],
    time_id: np.ndarray,
    asset_id: np.ndarray,
) -> np.ndarray:
    out = np.zeros(shape, dtype=np.uint8)
    if column in panel.columns:
        out[time_id, asset_id] = panel[column].fill_null(False).cast(pl.UInt8).to_numpy()
    return out


def _tradable_matrix(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
) -> np.ndarray:
    present = _present_matrix(open_, high, low, close, volume)
    valid_ohlc = present & ((open_ > 0) | (high > 0) | (low > 0) | (close > 0))
    max_price = np.array(open_, dtype=np.float32, copy=True)
    min_price = np.array(open_, dtype=np.float32, copy=True)
    for values in (high, low, close):
        np.fmax(max_price, values, out=max_price)
        np.fmin(min_price, values, out=min_price)
    spread = max_price - min_price
    tolerance = np.maximum(np.abs(close) * np.float32(1e-4), np.float32(0.01))
    suspended_zero_volume = ((volume <= 0) | np.isnan(volume)) & (spread <= tolerance)
    return (valid_ohlc & ~suspended_zero_volume).astype(np.uint8)


def _present_matrix(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
) -> np.ndarray:
    del volume
    return np.isfinite(open_) | np.isfinite(high) | np.isfinite(low) | np.isfinite(close)


def _normalize_signal(signal: str) -> str:
    return signal if signal.startswith(("signal_", "csg_")) else f"signal_{signal}"


def _signal_code_matrix(
    panel: pl.DataFrame,
    signal_ids: list[str] | None,
    shape: tuple[int, int],
    time_id: np.ndarray,
    asset_id: np.ndarray,
) -> tuple[np.ndarray, tuple[str, ...]]:
    normalized = tuple(_normalize_signal(signal) for signal in (signal_ids or []))
    row_codes = np.full(len(panel), -1, dtype=np.int16)
    for code, column in enumerate(normalized):
        if column not in panel.columns:
            continue
        mask = panel[column].fill_null(False).cast(pl.Boolean).to_numpy()
        row_codes[(row_codes < 0) & mask] = code
    codes = np.full(shape, -1, dtype=np.int16)
    codes[time_id, asset_id] = row_codes
    return codes, normalized


def _scatter_bool_series(
    series: pl.Series | None,
    length: int,
    shape: tuple[int, int],
    time_id: np.ndarray,
    asset_id: np.ndarray,
) -> np.ndarray:
    out = np.zeros(shape, dtype=np.uint8)
    if series is None or len(series) != length:
        return out
    out[time_id, asset_id] = series.fill_null(False).cast(pl.UInt8).to_numpy()
    return out


def _delay_signal_matrix(
    raw: np.ndarray,
    codes: np.ndarray,
    present: np.ndarray,
    delay_bars: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    shape = raw.shape
    output = np.zeros(shape, dtype=np.uint8)
    signal_time = np.full(shape, -1, dtype=np.int32)
    signal_code = np.full(shape, -1, dtype=np.int16)
    for asset_id in range(shape[1]):
        rows = np.flatnonzero(present[:, asset_id])
        if len(rows) <= delay_bars:
            continue
        source_rows = rows[: len(rows) - delay_bars] if delay_bars else rows
        target_rows = rows[delay_bars:] if delay_bars else rows
        active = raw[source_rows, asset_id] != 0
        if not active.any():
            continue
        sources = source_rows[active]
        targets = target_rows[active]
        output[targets, asset_id] = 1
        signal_time[targets, asset_id] = sources.astype(np.int32)
        signal_code[targets, asset_id] = codes[sources, asset_id]
    return output, signal_time, signal_code


def _coerce_array(
    value: np.ndarray | None,
    shape: tuple[int, int],
    dtype: np.dtype | type,
    fill: int | float,
) -> np.ndarray:
    if value is None:
        return np.full(shape, fill, dtype=dtype)
    array = np.asarray(value, dtype=dtype)
    if array.shape != shape:
        raise ValueError(f"matrix shape {array.shape} does not match expected {shape}")
    return np.array(array, dtype=dtype, copy=True)


def _make_read_only(*arrays: np.ndarray) -> None:
    for array in arrays:
        array.flags.writeable = False
