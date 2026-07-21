"""自选股服务(§6.1)。

存储:`data/user_data/watchlist.parquet`,字段 symbol + added_at + note。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import polars as pl

from app.config import settings
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.client import get_client
from app.tickflow.rate_limits import chunked, resolve_limit

logger = logging.getLogger(__name__)


def _path() -> Path:
    from app.services.user_storage import path_for
    p = path_for(settings.data_dir, "watchlist.parquet")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def list_symbols() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    df = pl.read_parquet(p)
    if df.is_empty():
        return []
    return df.to_dicts()


def add(symbol: str, note: str = "") -> list[dict]:
    p = _path()
    if p.exists():
        df = pl.read_parquet(p)
        # 已存在则先移除，后面重新插入到最前面
        if symbol in df["symbol"].to_list():
            df = df.filter(pl.col("symbol") != symbol)
    else:
        df = pl.DataFrame(schema={"symbol": pl.Utf8, "added_at": pl.Utf8, "note": pl.Utf8})

    new_row = pl.DataFrame({
        "symbol": [symbol],
        "added_at": [datetime.utcnow().isoformat(timespec="seconds")],
        "note": [note],
    })
    out = pl.concat([new_row, df], how="diagonal_relaxed")
    out.write_parquet(p)
    return out.to_dicts()


def remove(symbol: str) -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    df = pl.read_parquet(p)
    df = df.filter(pl.col("symbol") != symbol)
    df.write_parquet(p)
    return df.to_dicts()


def move_to_top(symbol: str) -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    df = pl.read_parquet(p)
    if df.is_empty() or symbol not in df["symbol"].to_list():
        return df.to_dicts()
    target = df.filter(pl.col("symbol") == symbol)
    rest = df.filter(pl.col("symbol") != symbol)
    out = pl.concat([target, rest], how="diagonal_relaxed")
    out.write_parquet(p)
    return out.to_dicts()


def clear() -> int:
    """清空自选列表。返回移除的数量。"""
    p = _path()
    if not p.exists():
        return 0
    df = pl.read_parquet(p)
    count = df.height
    if count > 0:
        pl.DataFrame(schema={"symbol": pl.Utf8, "added_at": pl.Utf8, "note": pl.Utf8}).write_parquet(p)
    return count


def fetch_quotes(symbols: list[str], capset: CapabilitySet, timeout_s: float = 8.0) -> list[dict]:
    """拉取实时行情。

    优先用 quote.batch;否则降级为 quote.by_symbol 单股请求。
    timeout_s: 单批次请求超时(秒)，防止 API 卡死阻塞整个请求。
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    if not symbols:
        return []

    tf = get_client()
    quotes: list[dict] = []

    # 走 batch
    if capset.has(Cap.QUOTE_BATCH):
        batch_size = resolve_limit(capset, Cap.QUOTE_BATCH, default_batch=50).batch
    elif capset.has(Cap.QUOTE_BY_SYMBOL):
        batch_size = resolve_limit(capset, Cap.QUOTE_BY_SYMBOL, default_batch=5).batch
    else:
        # 无任何实时行情能力(none/free 档走 free-api 服务器,不提供实时行情)
        # 提前返回空,避免发起注定失败的请求
        return []

    chunks = chunked(symbols, batch_size)

    # 用线程池为每个批次加超时保护
    pool = ThreadPoolExecutor(max_workers=1)
    for chunk in chunks:
        try:
            future = pool.submit(tf.quotes.get, symbols=chunk, as_dataframe=True)
            raw = future.result(timeout=timeout_s)
            if raw is None or len(raw) == 0:
                continue
            df = pl.from_pandas(raw)
            rename_map = {
                "last_price": "price",
                "ext.change_pct": "pct",
                "ext.name": "name",
            }
            df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})
            quotes.extend(df.to_dicts())
        except FuturesTimeout:
            logger.warning("quote fetch timeout (%.1fs) for %d symbols", timeout_s, len(chunk))
            break  # 超时后不再尝试后续批次
        except Exception as e:  # noqa: BLE001
            logger.warning("quote fetch failed for %d symbols: %s", len(chunk), e)
    pool.shutdown(wait=False)

    return quotes
