"""Instrument dimension sync service.

Fetch normalized instrument metadata through the data_providers abstraction and
persist it to instruments.parquet.

Provider switching is resolved through the registry.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

from app.data_providers.registry import get_active_provider_name, get_provider
from app.services.pinyin_index import add_pinyin_columns

logger = logging.getLogger(__name__)

_EXCHANGES = ["SH", "SZ", "BJ"]


# 数据源 provider 单例缓存
_provider_instance = None


def _get_data_provider():
    """获取当前配置的数据源 provider。

    通过 registry 解析当前 provider。
    """
    global _provider_instance
    if _provider_instance is None:
        provider_name = get_active_provider_name()
        _provider_instance = get_provider(provider_name)
        logger.info("data provider initialized: %s", provider_name)
    return _provider_instance


def sync_instruments(data_dir: Path) -> int:
    """Sync the full instrument dimension table to instruments.parquet.

    The provider returns the normalized schema:
    symbol/name/code/exchange/asset_type/source.

    The persisted table also includes as_of and pinyin search columns. Returns
    the number of rows written.
    """
    provider = _get_data_provider()
    try:
        df = provider.get_instruments("stock")
    except Exception as e:
        logger.warning("get_instruments(stock) failed: %s", e)
        return 0

    if df is None or df.is_empty():
        logger.warning("get_instruments returned empty")
        return 0

    df = add_pinyin_columns(df).with_columns(pl.lit(date.today()).alias("as_of"))

    out = data_dir / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)

    logger.info("instruments synced: %d rows → %s", df.height, out)
    return df.height


def enrich_names_from_quotes(
    data_dir: Path,
    quotes_data: list[dict],
) -> int:
    """Fill missing instrument names from quote responses.

    quotes.get(universes) may include ext.name after the daily pipeline; use it
    to fill missing names and refresh pinyin search columns.
    """
    if not quotes_data:
        return 0

    # 构建 symbol → name 映射
    name_map: dict[str, str] = {}
    for q in quotes_data:
        symbol = q.get("symbol", "")
        ext = q.get("ext") or {}
        name = ext.get("name") or q.get("name", "")
        if symbol and name:
            name_map[symbol] = name

    if not name_map:
        return 0

    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return 0

    df = pl.read_parquet(inst_path)

    # 只更新空 name 的行
    updates = pl.DataFrame({
        "symbol": list(name_map.keys()),
        "_new_name": list(name_map.values()),
    })
    df = df.join(updates, on="symbol", how="left")
    df = df.with_columns(
        pl.when(pl.col("name").is_null() | (pl.col("name") == ""))
        .then(pl.col("_new_name"))
        .otherwise(pl.col("name"))
        .alias("name"),
    ).drop("_new_name")
    df = add_pinyin_columns(df)

    df.write_parquet(inst_path)
    logger.info("instruments name enriched from quotes: %d names", len(name_map))
    return len(name_map)
