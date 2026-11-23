"""Access a signed dynamic replay inside the ordinary backtest worker."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from types import MappingProxyType

import numpy as np
import polars as pl
from packaging.version import Version

PRO_PROVIDER = "tivelo.pro.dynamic"
REPLAY_SCALE = "_backtest_decision_factor"
_MIN_REPLAY_VERSION = Version("0.1.0a2")
_REPLAY = ContextVar("dynamic_backtest_replay", default=None)

OHLC = ("open", "high", "low", "close")
PRICE_COLUMNS = tuple(f"raw_{name}" for name in OHLC)
KEYS = ("symbol", "date")


def supports_ordinary_replay(version: str | None) -> bool:
    return version is not None and Version(version) >= _MIN_REPLAY_VERSION


def current_replay():
    replay = _REPLAY.get()
    if replay is None:
        raise ValueError("动态复权需要已授权的回测任务")
    return replay


@contextmanager
def open_dynamic_replay(data_dir):
    from multiprocessing import parent_process
    from sqlite3 import DatabaseError

    from tivelo_contracts.errors import TiveloError
    from tivelo_extensions.installation import EngineInstallation, deployment_mode
    from tivelo_extensions.native_entry import load_native_entry
    from tivelo_extensions.store import EnginePackageStore

    if parent_process() is None:
        raise ValueError("动态复权需要独立回测工作进程")
    store = EnginePackageStore(data_dir)
    try:
        pin = store.resolve(PRO_PROVIDER)
    except (OSError, ValueError, DatabaseError) as exc:
        raise ValueError("请在已安装扩展启用 Tivelo Pro 动态复权") from exc
    if not supports_ordinary_replay(pin.version):
        raise ValueError("请升级 Tivelo Pro 至 0.1.0a2 或更高版本以使用普通回测")
    try:
        installation = EngineInstallation(data_dir)
        lease = installation.lease(pin.extension_id)
        public, prove = installation.device("run", bytes.fromhex(pin.entry_sha256))
        entry, _ = store.loadable_entry(pin)
        factory = getattr(load_native_entry(entry), "create_host_replay", None)
        if not callable(factory):
            raise ValueError("当前扩展不支持普通策略回测")
        replay = factory(
            pin=pin,
            lease=lease,
            device_public_key=public,
            prove_device=prove,
            deployment=deployment_mode(),
            observations=installation.root / "research/engine/installation/observations",
        )
    except (OSError, ValueError, ImportError, TiveloError, DatabaseError) as exc:
        raise ValueError("动态复权需要有效的 Tivelo Pro A 股回测授权，请在已安装扩展检查") from exc

    token = _REPLAY.set(replay)
    try:
        yield replay
    finally:
        _REPLAY.reset(token)


def attach_exchange_quotes(panel: pl.DataFrame, raw: pl.DataFrame) -> pl.DataFrame:
    if panel.is_empty():
        return panel
    result = panel.drop(*PRICE_COLUMNS, strict=False).join(
        raw, on=list(KEYS), how="left", validate="1:1", maintain_order="left",
    )
    missing = result.filter(
        pl.any_horizontal(~pl.col(name).is_finite() | (pl.col(name) <= 0)
                          for name in PRICE_COLUMNS).fill_null(True)
    )
    if missing.height:
        row = missing.select(*KEYS).row(0, named=True)
        raise ValueError(f"缺少 {row['symbol']} 在 {row['date']} 的原始日 K，请先同步数据")
    return result


def _aligned_quotes(matrix, quotes):
    days = pl.DataFrame({
        "date": [label[:10] for label in matrix.timestamp_labels], "_day": range(matrix.shape[0]),
    }).with_columns(pl.col("date").str.to_date())
    assets = pl.DataFrame({"symbol": matrix.symbols, "_asset": range(matrix.shape[1])})
    return quotes.join(days, on="date").join(assets, on="symbol")


def attach_exchange_matrix(market, quotes):
    """Keep the existing immutable matrix cache and attach the real execution columns."""
    linked = _aligned_quotes(market, quotes)
    rows, cols = linked["_day"].to_numpy(), linked["_asset"].to_numpy()
    observed = np.isfinite(market.close) & (market.close > 0)
    fields = dict(market.fields)
    for name in PRICE_COLUMNS:
        values = np.full(market.shape, np.nan, dtype=np.float64)
        active = observed[rows, cols]
        values[rows[active], cols[active]] = linked[name].to_numpy()[active]
        missing = observed & (~np.isfinite(values) | (values <= 0))
        if missing.any():
            t, s = np.argwhere(missing)[0]
            raise ValueError(f"缺少 {market.symbols[s]} 在 {market.timestamp_labels[t]} 的原始日 K")
        values.flags.writeable = False
        fields[name] = values
    return replace(market, fields=MappingProxyType(fields))


def exact_execution_prices(matrix, quotes):
    """Keep the existing continuous return curve while restoring raw fill precision."""
    linked = _aligned_quotes(matrix, quotes)
    rows, cols = linked["_day"].to_numpy(), linked["_asset"].to_numpy()
    scale = matrix.price_scale
    values = {}
    for name in OHLC:
        series = np.array(getattr(matrix, name), dtype=np.float64)
        live = np.isfinite(series[rows, cols])
        i, j = rows[live], cols[live]
        series[i, j] = linked[f"raw_{name}"].to_numpy()[live] * scale[i, j]
        series.flags.writeable = False
        values[name] = series
    return replace(matrix, **values)
