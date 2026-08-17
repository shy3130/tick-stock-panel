"""自选股服务(§6.1)。

存储:`data/user_data/watchlist.parquet`,字段 symbol + added_at + note + tags。
tags 为逗号分隔的 Utf8 字符串(空串 = 无标签), 支持多对多标注。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

import polars as pl

from app.config import settings
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.client import get_client
from app.tickflow.rate_limits import chunked, resolve_limit

logger = logging.getLogger(__name__)

_SCHEMA = {"symbol": pl.Utf8, "added_at": pl.Utf8, "note": pl.Utf8, "tags": pl.Utf8}

_MAX_TAG_LEN = 20

# 进程内互斥: parquet 是 read-modify-write, 防并发请求丢更新(如快速连续打标签)
_write_lock = threading.Lock()


def _path() -> Path:
    p = settings.data_dir / "user_data" / "watchlist.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read() -> pl.DataFrame:
    """读取自选表, 兼容旧文件(缺 tags 列时补空列, null 标签归一化为空串)。"""
    p = _path()
    df = pl.read_parquet(p) if p.exists() else pl.DataFrame(schema=_SCHEMA)
    for col, dtype in _SCHEMA.items():
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(col))
    # 上面循环已保证 tags 列存在, 只把 null 归一化为空串
    df = df.with_columns(pl.col("tags").fill_null(""))
    return df


def _write(df: pl.DataFrame) -> None:
    df.write_parquet(_path())


def list_symbols() -> list[dict]:
    df = _read()
    if df.is_empty():
        return []
    return df.to_dicts()


def add(symbol: str, note: str = "") -> list[dict]:
    with _write_lock:
        df = _read()
        existing_tags = ""
        if symbol in df["symbol"].to_list():
            existing_tags = df.filter(pl.col("symbol") == symbol)["tags"].first()
            df = df.filter(pl.col("symbol") != symbol)

        new_row = pl.DataFrame({
            "symbol": [symbol],
            "added_at": [datetime.utcnow().isoformat(timespec="seconds")],
            "note": [note],
            "tags": [existing_tags],
        })
        out = pl.concat([new_row, df], how="diagonal_relaxed")
        _write(out)
        return out.to_dicts()


def remove(symbol: str) -> list[dict]:
    with _write_lock:
        df = _read()
        df = df.filter(pl.col("symbol") != symbol)
        _write(df)
        return df.to_dicts()


def move_to_top(symbol: str) -> list[dict]:
    with _write_lock:
        df = _read()
        if df.is_empty() or symbol not in df["symbol"].to_list():
            return df.to_dicts()
        target = df.filter(pl.col("symbol") == symbol)
        rest = df.filter(pl.col("symbol") != symbol)
        out = pl.concat([target, rest], how="diagonal_relaxed")
        _write(out)
        return out.to_dicts()


def clear() -> int:
    """清空自选列表。返回移除的数量。"""
    with _write_lock:
        df = _read()
        count = df.height
        if count > 0:
            _write(pl.DataFrame(schema=_SCHEMA))
        return count


def _normalize_tags(tags: list[str]) -> list[str]:
    """规范化标签: trim / 去空 / 去重 / 剥离中英文逗号(防分隔符碰撞), 超长截断到 _MAX_TAG_LEN。"""
    cleaned = (str(t).strip().replace(",", "").replace("，", "")[:_MAX_TAG_LEN] for t in tags or [])  # noqa: RUF001 有意剥离全角逗号
    return list(dict.fromkeys(t for t in cleaned if t))


def set_tags(symbol: str, tags: list[str]) -> list[dict]:
    """整体替换某自选标的的标签。symbol 不在自选则原样返回(由 API 层决定 404)。"""
    with _write_lock:
        df = _read()
        if symbol not in df["symbol"].to_list():
            return df.to_dicts()
        cleaned = _normalize_tags(tags)
        df = df.with_columns(
            pl.when(pl.col("symbol") == symbol)
              .then(pl.lit(",".join(cleaned)))
              .otherwise(pl.col("tags"))
              .alias("tags")
        )
        _write(df)
        return df.to_dicts()


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
