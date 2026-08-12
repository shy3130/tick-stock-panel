"""指数 / ETF 数据同步服务。

标的列表优先用免费的 exchanges.get_instruments(type=index/etf) 拉取
(None/Free 档均可用,无需 quote.pool 权限);付费档可额外用
quotes.get_by_universes 作为补充来源。日K统一走 klines.batch。
"""
from __future__ import annotations

import gc
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import polars as pl

from app.indicators.pipeline import compute_enriched
from app.services import kline_sync, preferences
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.client import get_client
from app.tickflow.rate_limits import chunked, min_batch, resolve_limit, sleep_between_batches
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

# exchanges.get_instruments 查询的交易所(沪深京)
_EXCHANGES = ["SH", "SZ", "BJ"]


def _quotes_to_index_instruments(resp) -> pl.DataFrame:
    """将 TickFlow quotes 响应(get_by_universes)规范为指数 instruments。

    付费档(Starter+)的补充来源,免费档用不到。
    """
    if resp is None:
        return pl.DataFrame()

    if isinstance(resp, pl.DataFrame):
        df = resp
    elif hasattr(resp, "columns"):
        df = pl.from_pandas(resp.reset_index() if hasattr(resp, "reset_index") else resp)
    else:
        rows: list[dict] = []
        for q in resp or []:
            item = q if isinstance(q, dict) else {}
            ext = item.get("ext") or {}
            symbol = item.get("symbol")
            if not symbol:
                continue
            rows.append({
                "symbol": str(symbol),
                "name": ext.get("name") or item.get("name") or str(symbol),
            })
        df = pl.DataFrame(rows)

    if df.is_empty() or "symbol" not in df.columns:
        return pl.DataFrame()

    rename = {"ts_code": "symbol"}
    df = df.rename({k: v for k, v in rename.items() if k in df.columns})

    if "name" not in df.columns:
        if "ext" in df.columns:
            df = df.with_columns(pl.col("symbol").cast(pl.Utf8).alias("name"))
        else:
            df = df.with_columns(pl.col("symbol").cast(pl.Utf8).alias("name"))

    result = df.select([
        pl.col("symbol").cast(pl.Utf8),
        pl.col("name").cast(pl.Utf8),
    ]).with_columns([
        pl.col("symbol").str.split(".").list.first().alias("code"),
        pl.lit("index").alias("asset_type"),
    ])
    return result.unique(subset=["symbol"], keep="last").sort("symbol")


def _fetch_instruments_by_type(instrument_type: str, asset_type_label: str) -> pl.DataFrame:
    """用免费的 exchanges.get_instruments 拉取指定类型的标的列表。

    None/Free 档均可使用(标的信息查询免费开放)。
    instrument_type: 'index' / 'etf'
    asset_type_label: 写入 instruments 表的 asset_type 标记('index' / 'etf')
    """
    tf = get_client()
    rows: list[dict] = []
    for ex in _EXCHANGES:
        try:
            items = tf.exchanges.get_instruments(ex, instrument_type=instrument_type)
            for it in items or []:
                item = it if isinstance(it, dict) else {}
                symbol = item.get("symbol")
                if not symbol:
                    continue
                rows.append({
                    "symbol": str(symbol),
                    "name": item.get("name") or str(symbol),
                })
        except Exception as e:
            logger.warning("get_instruments(%s, type=%s) failed: %s", ex, instrument_type, e)

    if not rows:
        return pl.DataFrame()

    return (
        pl.DataFrame(rows)
        .with_columns([
            pl.col("symbol").str.split(".").list.first().alias("code"),
            pl.lit(asset_type_label).alias("asset_type"),
        ])
        .unique(subset=["symbol"], keep="last")
        .sort("symbol")
    )


def sync_index_instruments(
    repo: KlineRepository,
    pull_index: bool = True,
    pull_etf: bool = True,
) -> int:
    """同步指数 / ETF 标的维表,返回标的总数。

    新版物理分开保存: 指数写 instruments_index, ETF 写 instruments_etf。
    读取层仍兼容旧版 instruments_index 中 asset_type='etf' 的历史数据。
    """
    index_parts: list[pl.DataFrame] = []
    etf_parts: list[pl.DataFrame] = []

    # 1) 免费通道:按开关分别拉 index / etf
    if pull_index:
        index_df = _fetch_instruments_by_type("index", "index")
        if not index_df.is_empty():
            index_parts.append(index_df)
    if pull_etf:
        etf_df = _fetch_instruments_by_type("etf", "etf")
        if not etf_df.is_empty():
            etf_parts.append(etf_df)

    # 2) 付费补充:Starter+ 用 get_by_universes 补指数(仅当开启指数拉取)
    if pull_index:
        capset = None
        try:
            from app.tickflow import policy
            capset = policy.detect_capabilities(force=False)
        except Exception:
            pass
        if capset is not None and capset.has(Cap.QUOTE_POOL):
            tf = get_client()
            for kwargs in (
                {"universes": ["CN_Index"]},
                {"universes": ["CN_Index"], "as_dataframe": False},
            ):
                try:
                    resp = tf.quotes.get_by_universes(**kwargs)
                    if resp is not None and len(resp) > 0:
                        sup = _quotes_to_index_instruments(resp)
                        if not sup.is_empty():
                            index_parts.append(sup)
                        break
                except Exception as e:
                    logger.debug("CN_Index universe supplement failed: %s", e)

    total = 0
    if index_parts:
        index_inst = pl.concat(index_parts, how="diagonal_relaxed").unique(subset=["symbol"], keep="last").sort("symbol")
        if not index_inst.is_empty():
            repo.save_index_instruments(index_inst)
            total += index_inst.height
    if etf_parts:
        etf_inst = pl.concat(etf_parts, how="diagonal_relaxed").unique(subset=["symbol"], keep="last").sort("symbol")
        if not etf_inst.is_empty():
            repo.save_etf_instruments(etf_inst)
            total += etf_inst.height

    if total == 0:
        logger.warning("指数/ETF 标的列表为空(pull_index=%s, pull_etf=%s)", pull_index, pull_etf)
        return 0
    repo.refresh_index_views()
    logger.info("指数/ETF 标的同步完成: %d 只", total)
    return total


def sync_etf_instruments(repo: KlineRepository) -> int:
    """单独同步 ETF 标的维表(返回 ETF 数量)。"""
    etf_df = _fetch_instruments_by_type("etf", "etf")
    if etf_df.is_empty():
        return 0
    repo.save_etf_instruments(etf_df)
    repo.refresh_index_views()
    return etf_df.height


def sync_and_persist_index_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    count: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    symbols_override: list[str] | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> int:
    """同步指数/ETF 日K到独立 parquet,并计算 enriched。

    symbols_override 非空时,只拉这些代码(跳过 instruments 表),用于自定义范围。
    否则取 index_instruments 表全量(指数+ETF 合并存储)。
    on_chunk_done(current, total) 每个批次完成后回调。
    """
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return 0

    if symbols_override:
        symbols = sorted(set(s for s in symbols_override if s))
        if not symbols:
            return 0
    else:
        instruments = repo.get_index_instruments()
        if instruments.is_empty():
            sync_index_instruments(repo, pull_index=True, pull_etf=False)
            instruments = repo.get_index_instruments()
        if not instruments.is_empty() and "asset_type" in instruments.columns:
            instruments = instruments.filter(pl.col("asset_type") != "etf")
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return 0
        symbols = sorted(set(instruments["symbol"].to_list()))
    limit = resolve_limit(capset, Cap.KLINE_DAILY_BATCH)
    batch_size = min_batch(preferences.get_index_daily_batch_size(), limit)

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    total_rows = 0
    chunks = chunked(symbols, batch_size)
    for i, chunk in enumerate(chunks):
        sleep_between_batches(i, limit.rpm)
        raw = kline_sync.sync_daily_batch(
            chunk,
            count=count,
            batch_size=None,
            start_time=start_time,
            end_time=end_time,
        )
        if raw.is_empty():
            continue

        repo.append_index_daily(raw)
        enriched = compute_enriched(raw, factors=None, instruments=None)
        repo.append_index_enriched(enriched)
        total_rows += raw.height
        logger.info("index/etf daily synced: %d/%d chunks, +%d rows", i + 1, len(chunks), raw.height)
        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))
        del raw, enriched
        gc.collect()
    repo.refresh_index_views()
    return total_rows


def _load_etf_factors(repo: KlineRepository) -> pl.DataFrame:
    factor_path = repo.store.data_dir / "adj_factor_etf" / "all.parquet"
    if not factor_path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(factor_path)
    except Exception as e:
        logger.warning("ETF 复权因子读取失败: %s", e)
        return pl.DataFrame()


def sync_etf_adj_factor(
    symbols: list[str],
    repo: KlineRepository,
    capset: CapabilitySet,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    on_chunk_done=None,
) -> tuple[int, list[str]]:
    """同步 ETF 复权因子；失败由调用方降级为 warning。"""
    return kline_sync.sync_adj_factor(
        symbols,
        repo,
        capset,
        start_time=start_time,
        end_time=end_time,
        on_chunk_done=on_chunk_done,
        asset_type="etf",
    )


def sync_and_persist_etf_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    count: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    symbols_override: list[str] | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> int:
    """同步 ETF 日K到独立 kline_etf_* parquet,并计算 ETF enriched。
    on_chunk_done(current, total) 每个批次完成后回调。
    """
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return 0

    if symbols_override:
        symbols = sorted(set(s for s in symbols_override if s))
    else:
        instruments = repo.get_etf_instruments()
        if instruments.is_empty():
            sync_etf_instruments(repo)
            instruments = repo.get_etf_instruments()
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return 0
        symbols = sorted(set(instruments["symbol"].to_list()))
    if not symbols:
        return 0

    limit = resolve_limit(capset, Cap.KLINE_DAILY_BATCH)
    batch_size = min_batch(preferences.get_index_daily_batch_size(), limit)

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    total_rows = 0
    chunks = chunked(symbols, batch_size)
    factors = _load_etf_factors(repo)
    for i, chunk in enumerate(chunks):
        sleep_between_batches(i, limit.rpm)
        raw = kline_sync.sync_daily_batch(
            chunk,
            count=count,
            batch_size=None,
            start_time=start_time,
            end_time=end_time,
        )
        if raw.is_empty():
            continue

        repo.append_etf_daily(raw)
        batch_factors = factors.filter(pl.col("symbol").is_in(chunk)) if not factors.is_empty() else factors
        # ETF 使用复权和通用技术指标；不传 instruments，避免套用 A股涨跌停/连板逻辑。
        enriched = compute_enriched(raw, factors=batch_factors, instruments=None)
        repo.append_etf_enriched(enriched)
        total_rows += raw.height
        logger.info("etf daily synced: %d/%d chunks, +%d rows", i + 1, len(chunks), raw.height)
        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))
        del raw, enriched
        gc.collect()
    repo.refresh_index_views()
    return total_rows
