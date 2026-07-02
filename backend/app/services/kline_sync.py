"""日 K 同步服务(§7.7 Step 1)。

调度器在 capability 允许下,把符号集合的日 K 批量同步到本地 Parquet。
策略:
  - 日 K 仅使用 `kline.daily.batch`
  - 除权因子仅使用 `adj_factor`

数据获取通过 data_providers 抽象层,支持 provider 切换。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import polars as pl

from app.data_providers.registry import get_active_provider_name, get_provider
from app.indicators.pipeline import filter_halt_days
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)


# 数据源 provider 单例缓存(默认 tickflow,可切到 fquant)
_provider_instance = None


def _get_data_provider():
    """获取当前配置的数据源 provider。

    通过 registry 解析当前 provider,默认 ``tickflow``。
    支持值: ``tickflow`` / ``fquant``。
    """
    global _provider_instance
    if _provider_instance is None:
        provider_name = get_active_provider_name()
        _provider_instance = get_provider(provider_name)
        logger.info("data provider initialized: %s", provider_name)
    return _provider_instance


# 标准列(无论 SDK 返回什么形状,我们把它规范成这套)
CANONICAL_DAILY_COLS = [
    "symbol", "date", "open", "high", "low", "close", "volume", "amount",
]


def _normalize_daily(df_in, default_symbol: str | None = None) -> pl.DataFrame:
    """把 SDK 返回的 pandas/任意 DataFrame 规范成 canonical 列。

    注意: provider.get_daily() 返回的已经是 normalize 过的 Polars DataFrame,
    传入此函数是幂等操作(二次 normalize 无副作用),作为安全网保留。
    """
    if df_in is None or len(df_in) == 0:
        return pl.DataFrame()

    if not isinstance(df_in, pl.DataFrame):
        df = pl.from_pandas(df_in.reset_index() if hasattr(df_in, "reset_index") else df_in)
    else:
        df = df_in

    # 兼容字段名差异
    rename_map = {
        "ts_code": "symbol",
        "trade_date": "date",
        "vol": "volume",
        "amt": "amount",
        "datetime": "date",
    }
    df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})

    if "symbol" not in df.columns and default_symbol is not None:
        df = df.with_columns(pl.lit(default_symbol).alias("symbol"))

    # 类型规范
    if "date" in df.columns and df.schema["date"] != pl.Date:
        df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))

    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
    for col in ("volume", "amount"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))

    # 过滤停牌日 (open/high 为 0; close 可能被填充为前收盘价, 不能用全零判断)
    df = filter_halt_days(df)

    # 只保留 canonical 列
    keep = [c for c in CANONICAL_DAILY_COLS if c in df.columns]
    return df.select(keep)


def sync_daily_batch(symbols: list[str],
                     count: int | None = None,
                     batch_size: int | None = None,
                     rpm: int | None = None,
                     start_time: datetime | None = None,
                     end_time: datetime | None = None,
                     on_chunk_done: Callable[[int, int], None] | None = None) -> pl.DataFrame:
    """批量拉取多股日 K（通过 data_providers 抽象层）。

    优先使用 start_time / end_time 区间 + count=10000,确保覆盖完整时间段。
    仅传 count 时按条数回溯。

    注意: count 参数保留在签名中以保持向后兼容,但 provider 内部自行决定拉取条数。
    """
    provider = _get_data_provider()
    out: list[pl.DataFrame] = []
    interval = (60.0 / rpm) if rpm else 0

    if batch_size is None:
        chunks = [symbols]
    else:
        chunks = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]

    for i, chunk in enumerate(chunks):
        if i > 0 and interval > 0 and len(chunks) > rpm:
            time.sleep(interval)
        try:
            # provider.get_daily 返回已 normalize 的 Polars DataFrame
            # 再过一次 _normalize_daily 作为安全网(幂等)
            raw = provider.get_daily(
                chunk, start_time=start_time, end_time=end_time, asset_type="stock",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("batch fetch failed for %d symbols: %s", len(chunk), e)
            continue

        if raw is not None and not raw.is_empty():
            out.append(_normalize_daily(raw))

        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))

    if not out:
        return pl.DataFrame()
    return pl.concat(out, how="diagonal_relaxed")


def sync_and_persist_daily_batch(
    symbols: list[str],
    repo: KlineRepository,
    capset: CapabilitySet,
    count: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> int:
    """批量同步日 K 并落到 Parquet。返回写入的行数。

    start_date/end_date: 外部传入的时间范围(由 pipeline 根据已有数据计算)。
    未传入时默认拉最近 1 年。
    """
    if not symbols or not capset.has(Cap.KLINE_DAILY_BATCH):
        return 0

    lim = capset.limits(Cap.KLINE_DAILY_BATCH)
    batch_size = lim.batch if lim and lim.batch else 100
    rpm = lim.rpm if lim else None

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    df = sync_daily_batch(
        symbols, count=count, batch_size=batch_size, rpm=rpm,
        start_time=start_time, end_time=end_time,
        on_chunk_done=on_chunk_done,
    )

    if df.is_empty():
        return 0

    repo.append_daily(df)

    try:
        d = repo.store.data_dir.as_posix()
        repo.db.execute(
            f"""CREATE OR REPLACE VIEW kline_daily AS
                SELECT * FROM read_parquet('{d}/kline_daily/**/*.parquet', union_by_name=true)"""
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh view failed: %s", e)

    return df.height


def sync_daily_by_quotes(repo: KlineRepository) -> int:
    """用实时行情接口拉全市场当日数据,覆写 kline_daily 今天分区。

    一个请求覆盖 ~5500 只股票,比 batch K-line 快几个数量级。
    返回写入的行数。
    """
    from datetime import date as _date

    provider = _get_data_provider()
    try:
        df = provider.get_realtime(universes=["CN_Equity_A"])
    except Exception as e:  # noqa: BLE001
        logger.warning("get_realtime failed: %s", e)
        return 0

    if df is None or df.is_empty():
        logger.warning("get_realtime returned empty")
        return 0

    # 从实时行情字段映射到日 K canonical 列
    # TickFlow quotes 返回 last_price; 映射为 close
    if "last_price" in df.columns and "close" not in df.columns:
        df = df.rename({"last_price": "close"})

    # 只保留需要的列
    keep = ["symbol", "open", "high", "low", "close", "volume", "amount"]
    available = [c for c in keep if c in df.columns]
    if not available:
        logger.warning("get_realtime: no usable OHLCV columns")
        return 0
    df = df.select(available)

    today = _date.today()
    daily_df = df.with_columns(pl.lit(today).cast(pl.Date).alias("date"))

    # 过滤停牌 (open/high 为 0; close 可能被填充为前收盘价, 不能用全零判断)
    daily_df = filter_halt_days(daily_df)

    repo.flush_live_daily(daily_df)
    logger.info("sync_daily_by_quotes: %d symbols flushed for %s", daily_df.height, today)
    return daily_df.height


def _normalize_adj_factor(raw) -> pl.DataFrame:
    """Normalize SDK/provider ex_factors response to symbol/trade_date/ex_factor.

    注意: provider.get_adj_factors() 返回的已经是 normalize 过的 Polars DataFrame,
    传入此函数是幂等操作(二次 normalize 无副作用),作为安全网保留。
    """
    if raw is None or len(raw) == 0:
        return pl.DataFrame()
    if isinstance(raw, dict):
        rows: list[dict] = []
        for sym, values in raw.items():
            for item in values or []:
                row = dict(item or {})
                row.setdefault("symbol", sym)
                rows.append(row)
        df = pl.DataFrame(rows) if rows else pl.DataFrame()
    elif isinstance(raw, pl.DataFrame):
        df = raw
    else:
        df = pl.from_pandas(raw.reset_index() if hasattr(raw, "reset_index") else raw)
    if df.is_empty():
        return df
    # rename: timestamp/date → trade_date, adj_factor → ex_factor
    # 注意: 新版 SDK 可能同时返回 timestamp 和 trade_date (或 adj_factor 和 ex_factor),
    # 直接 rename 会产生重复列报错。仅当目标列不存在时才 rename。
    rename_map: dict[str, str] = {}
    for src, dst in (("timestamp", "trade_date"), ("date", "trade_date"), ("adj_factor", "ex_factor")):
        if src in df.columns and dst not in df.columns:
            rename_map[src] = dst
    df = df.rename(rename_map)
    if "trade_date" in df.columns:
        if df.schema["trade_date"] in {pl.Int64, pl.Int32, pl.UInt64, pl.UInt32, pl.Float64, pl.Float32}:
            df = df.with_columns(
                pl.from_epoch(pl.col("trade_date").cast(pl.Int64), time_unit="ms").dt.date().alias("trade_date")
            )
        else:
            df = df.with_columns(pl.col("trade_date").cast(pl.Date, strict=False))
    if "ex_factor" in df.columns:
        df = df.with_columns(pl.col("ex_factor").cast(pl.Float64, strict=False))
    cols = [c for c in ["symbol", "trade_date", "ex_factor"] if c in df.columns]
    if len(cols) < 3:
        return pl.DataFrame()
    return df.select(cols).drop_nulls()


def sync_adj_factor(symbols: list[str], repo: KlineRepository,
                    capset: CapabilitySet,
                    start_time: datetime | None = None,
                    end_time: datetime | None = None,
                    on_chunk_done: Callable[[int, int], None] | None = None,
                    asset_type: str = "stock") -> tuple[int, list[str]]:
    """同步除权因子(Starter+)。

    通过 data_providers 抽象层获取除权因子数据。

    支持增量: 传 start_time/end_time 只拉取该时间范围内的新除权事件。
    返回 (写入行数, 受影响的 symbol 列表) — 供 enriched 局部重算使用。
    """
    if not capset.has(Cap.ADJ_FACTOR) or not symbols:
        return 0, []

    provider = _get_data_provider()
    lim = capset.limits(Cap.ADJ_FACTOR)
    batch_size = lim.batch if lim and lim.batch else 50
    rpm = lim.rpm if lim else 30
    interval = 60.0 / rpm if rpm else 0

    chunks = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
    all_dfs: list[pl.DataFrame] = []

    for i, chunk in enumerate(chunks):
        if i > 0 and interval > 0 and len(chunks) > rpm:
            time.sleep(interval)
        try:
            # provider.get_adj_factors 返回已 normalize 的 Polars DataFrame
            # 再过一次 _normalize_adj_factor 作为安全网(幂等)
            raw = provider.get_adj_factors(
                chunk, start_time=start_time, end_time=end_time, asset_type=asset_type,
            )
            normalized = _normalize_adj_factor(raw)
            if not normalized.is_empty():
                all_dfs.append(normalized)
            logger.debug("adj_factor chunk %d/%d: %d symbols", i + 1, len(chunks), len(chunk))
        except Exception as e:  # noqa: BLE001
            logger.warning("adj_factor chunk %d failed: %s", i + 1, e)

        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))

    if not all_dfs:
        return 0, []

    new_data = pl.concat(all_dfs, how="diagonal_relaxed") if len(all_dfs) > 1 else all_dfs[0]

    # 提取受影响的 symbol 列表(合并前)
    affected = new_data["symbol"].unique().to_list()

    factor_dir = "adj_factor_etf" if asset_type == "etf" else "adj_factor"
    out = repo.store.data_dir / factor_dir / "all.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)

    if out.exists():
        existing = pl.read_parquet(out)
        before = existing.height
        merged = pl.concat([existing, new_data]).unique(
            subset=["symbol", "trade_date"], keep="last",
        ).sort(["symbol", "trade_date"])
        merged.write_parquet(out)
        added = merged.height - before
        logger.info("adj_factor merged: %d total (+%d new), %d/%d symbols",
                     merged.height, added, new_data.height, len(symbols))
        return added, affected
    else:
        new_data.sort(["symbol", "trade_date"]).write_parquet(out)
        logger.info("adj_factor synced: %d rows (%d symbols)", new_data.height, len(symbols))
        return new_data.height, affected


# ===== 分钟 K 同步 =====

CANONICAL_MINUTE_COLS = [
    "symbol", "datetime", "open", "high", "low", "close", "volume", "amount",
]


def _normalize_minute(df_in, default_symbol: str | None = None) -> pl.DataFrame:
    """把 SDK/provider 返回的分钟 K 数据规范成 canonical 列。

    注意: provider.get_minute() 返回的已经是 normalize 过的 Polars DataFrame
    (含 MINUTE_COLUMNS), 传入此函数会额外过滤掉 asset_type/source/freq 等列,
    只保留 CANONICAL_MINUTE_COLS,是幂等安全操作。
    """
    if df_in is None or len(df_in) == 0:
        return pl.DataFrame()

    if not isinstance(df_in, pl.DataFrame):
        df = pl.from_pandas(df_in.reset_index() if hasattr(df_in, "reset_index") else df_in)
    else:
        df = df_in

    rename_map = {
        "ts_code": "symbol",
        "vol": "volume",
        "amt": "amount",
    }
    df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})

    # datetime 列:优先用 timestamp(毫秒精度),其次 trade_time
    if "timestamp" in df.columns:
        df = df.with_columns(
            pl.from_epoch("timestamp", time_unit="ms").alias("datetime"),
        ).drop("timestamp")
        for drop_col in ("trade_time", "trade_date"):
            if drop_col in df.columns:
                df = df.drop(drop_col)
    elif "trade_time" in df.columns:
        df = df.rename({"trade_time": "datetime"})
        if "trade_date" in df.columns:
            df = df.drop("trade_date")
    elif "trade_date" in df.columns:
        df = df.rename({"trade_date": "datetime"})

    if "symbol" not in df.columns and default_symbol is not None:
        df = df.with_columns(pl.lit(default_symbol).alias("symbol"))

    # 类型规范:统一转 Datetime('us')
    if "datetime" in df.columns:
        dt_type = df.schema["datetime"]
        if not isinstance(dt_type, pl.Datetime) or dt_type.time_unit != "us":
            df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))

    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
    for col in ("volume", "amount"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))

    keep = [c for c in CANONICAL_MINUTE_COLS if c in df.columns]
    return df.select(keep)


def _datetime_to_ms(dt: datetime) -> int:
    """datetime → 毫秒时间戳 (供 SDK start_time / end_time 使用)。

    保留此函数: TickFlowProvider 内部通过 lazy import 调用它。
    """
    return int(dt.timestamp() * 1000)


def sync_minute_batch(
    symbols: list[str],
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    count: int | None = None,
    batch_size: int | None = None,
    rpm: int | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> pl.DataFrame:
    """批量拉取多股分钟 K（通过 data_providers 抽象层）。

    优先使用 start_time / end_time 区间, 确保所有标的覆盖同一时间段。
    count 仅作为 fallback 保留。
    on_chunk_done(current, total) 每个 chunk 完成后回调。

    注意: count 参数保留在签名中以保持向后兼容,但 provider 内部自行决定拉取条数。
    """
    provider = _get_data_provider()
    out: list[pl.DataFrame] = []
    interval = (60.0 / rpm) if rpm else 0

    if batch_size is None:
        chunks = [symbols]
    else:
        chunks = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]

    for i, chunk in enumerate(chunks):
        if i > 0 and interval > 0 and len(chunks) > rpm:
            time.sleep(interval)
        try:
            # provider.get_minute 返回已 normalize 的 Polars DataFrame
            # 再过一次 _normalize_minute 作为安全网(幂等)
            raw = provider.get_minute(
                chunk, start_time=start_time, end_time=end_time,
                asset_type="stock", freq="1m",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("minute batch fetch failed for %d symbols: %s", len(chunk), e)
            continue

        if raw is not None and not raw.is_empty():
            out.append(_normalize_minute(raw))

        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))

    if not out:
        return pl.DataFrame()
    return pl.concat(out, how="diagonal_relaxed")


def fetch_minute_single(symbol: str, trade_date: date) -> pl.DataFrame:
    """从数据源实时拉取单股单日分钟 K（不写入本地）。

    通过 data_providers 抽象层获取。
    """
    from datetime import datetime
    start_time = datetime(trade_date.year, trade_date.month, trade_date.day, 9, 25, 0)
    end_time = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 5, 0)
    provider = _get_data_provider()
    try:
        raw = provider.get_minute(
            [symbol], start_time=start_time, end_time=end_time,
            asset_type="stock", freq="1m",
        )
    except Exception as e:
        logger.warning("fetch_minute_single(%s, %s) failed: %s", symbol, trade_date, e)
        return pl.DataFrame()

    if raw is not None and not raw.is_empty():
        return _normalize_minute(raw)
    return pl.DataFrame()


def fetch_adj_factor_single(symbol: str) -> pl.DataFrame:
    """从数据源实时拉取单股除权因子(不写入本地), 用于单股 K 线即时前复权。

    通过 data_providers 抽象层获取。

    返回结构: symbol, trade_date, ex_factor (空 DataFrame 表示无除权事件或拉取失败)。
    与 _apply_adj_factor / compute_enriched 的 factors 参数格式一致。
    """
    provider = _get_data_provider()
    try:
        raw = provider.get_adj_factors(
            [symbol], start_time=None, end_time=None, asset_type="stock",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("fetch_adj_factor_single(%s) failed: %s", symbol, e)
        return pl.DataFrame()
    return _normalize_adj_factor(raw)


def _latest_minute_datetime(repo: KlineRepository) -> datetime | None:
    """本地分钟 K 数据的最新时间。"""
    try:
        res = repo.execute_one("SELECT max(datetime) FROM kline_minute")
        if res and res[0]:
            d = res[0]
            if isinstance(d, datetime):
                return d
            return datetime.fromisoformat(str(d))
    except Exception:  # noqa: BLE001
        pass
    return None


def _cleanup_null_datetime_minute(repo: KlineRepository) -> None:
    """检测并清除 datetime 全为 null 的旧版分钟 K 数据(迁移用)。"""
    minute_dir = repo.store.data_dir / "kline_minute"
    if not minute_dir.exists():
        return
    try:
        row = repo.execute_one(
            "SELECT count(*) AS total, count(datetime) AS non_null FROM kline_minute"
        )
        if row and row[0] > 0 and (row[1] is None or row[1] == 0):
            # 全部 datetime 为 null — 清除所有分钟 K parquet
            n = 0
            for f in minute_dir.rglob("*.parquet"):
                f.unlink()
                n += 1
            logger.info("cleaned %d corrupted minute-K parquet files (null datetime)", n)
    except Exception as e:  # noqa: BLE001
        logger.debug("minute cleanup check failed: %s", e)


def _migrate_symbol_to_date_partition(repo: KlineRepository) -> None:
    """将旧版 symbol= 分区迁移为 date= 分区。迁移完成后删除旧目录。"""
    minute_dir = repo.store.data_dir / "kline_minute"
    if not minute_dir.exists():
        return

    old_dirs = [d for d in minute_dir.iterdir() if d.is_dir() and d.name.startswith("symbol=")]
    if not old_dirs:
        return

    logger.info("migrating %d symbol-partitioned minute-K dirs to date partition…", len(old_dirs))

    all_frames: list[pl.DataFrame] = []
    for sym_dir in old_dirs:
        for pq in sym_dir.glob("*.parquet"):
            try:
                df = pl.read_parquet(pq)
                if "datetime" in df.columns:
                    df = df.filter(pl.col("datetime").is_not_null())
                if not df.is_empty():
                    all_frames.append(df)
            except Exception:  # noqa: BLE001
                pass

    if not all_frames:
        # 数据全部不可用，直接删旧目录
        for d in old_dirs:
            d.mkdir(parents=True, exist_ok=True)
            for f in d.rglob("*"):
                if f.is_file():
                    f.unlink()
            d.rmdir()
        return

    combined = pl.concat(all_frames, how="diagonal_relaxed")
    combined = combined.unique(subset=["symbol", "datetime"], keep="last")

    # 按日期写新分区
    combined = combined.with_columns(pl.col("datetime").dt.date().alias("_trade_date"))
    for day_df in combined.partition_by("_trade_date"):
        trade_date = day_df["_trade_date"][0]
        out = minute_dir / f"date={trade_date}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        day_df = day_df.drop("_trade_date").sort("symbol", "datetime")
        day_df.write_parquet(out)

    # 删旧目录
    for d in old_dirs:
        for f in d.rglob("*"):
            if f.is_file():
                f.unlink()
        # 移除空目录
        try:
            d.rmdir()
        except OSError:
            pass

    logger.info("minute-K migration done: %d rows migrated", combined.height)


def sync_and_persist_minute(
    symbols: list[str],
    repo: KlineRepository,
    capset: CapabilitySet,
    days: int = 5,
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> int:
    """同步分钟 K 并存到 Parquet(仅 raw,不前复权)。返回写入行数。

    使用 start_time / end_time 区间拉取, 确保所有标的覆盖同一时间段。
    on_chunk_done(current, total) 每个 chunk 完成后回调。
    """
    if not symbols or not capset.has(Cap.KLINE_MINUTE_BATCH):
        return 0

    # 迁移:旧版 _normalize_minute 未转换 timestamp→datetime,导致全部 datetime 为 null
    # 检测到后直接清除(这些数据无法使用)
    _cleanup_null_datetime_minute(repo)

    # 迁移:旧版按 symbol= 分区转为 date= 分区
    _migrate_symbol_to_date_partition(repo)

    now = datetime.now()

    # 计算时间区间: 首次拉取回溯 N 天, 增量从最后数据时间开始
    last_dt = _latest_minute_datetime(repo)
    if last_dt:
        start_time = last_dt
    else:
        start_time = now - timedelta(days=days)
    end_time = now

    lim = capset.limits(Cap.KLINE_MINUTE_BATCH)
    batch_size = lim.batch if lim and lim.batch else 100
    rpm = lim.rpm if lim else 30

    df = sync_minute_batch(symbols, start_time=start_time, end_time=end_time,
                           batch_size=batch_size, rpm=rpm,
                           on_chunk_done=on_chunk_done)
    if df.is_empty():
        return 0

    # 按日期分区写: data/kline_minute/date={YYYY-MM-DD}/part.parquet
    df = df.with_columns(
        pl.col("datetime").dt.date().alias("_trade_date")
    )
    written = 0
    for day_df in df.partition_by("_trade_date"):
        trade_date = day_df["_trade_date"][0]
        out = repo.store.data_dir / "kline_minute" / f"date={trade_date}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            existing = pl.read_parquet(out)
            if "datetime" in existing.columns:
                existing = existing.filter(pl.col("datetime").is_not_null())
            day_df = pl.concat([existing, day_df.drop("_trade_date")]).unique(
                subset=["symbol", "datetime"], keep="last",
            )
        else:
            day_df = day_df.drop("_trade_date")
        day_df = day_df.sort("symbol", "datetime")
        day_df.write_parquet(out)
        written += day_df.height

    # 刷新视图
    try:
        d = repo.store.data_dir.as_posix()
        repo.db.execute(
            f"""CREATE OR REPLACE VIEW kline_minute AS
                SELECT * FROM read_parquet('{d}/kline_minute/**/*.parquet', union_by_name=true)"""
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh kline_minute view failed: %s", e)

    logger.info("minute K synced: %d rows (%d symbols)", written, len(symbols))
    return written
