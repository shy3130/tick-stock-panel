"""日 K 同步服务(§7.7 Step 1)。

调度器在 capability 允许下,把符号集合的日 K 批量同步到本地 Parquet。
策略:
  - 日 K 仅使用 `kline.daily.batch`
  - 除权因子仅使用 `adj_factor`
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers.base import AssetType
from app.indicators.pipeline import filter_halt_days
from app.market_time import cn_now
from app.services import preferences
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.client import get_client
from app.tickflow.rate_limits import chunked, resolve_limit, sleep_between_batches
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)


def _atomic_write_parquet(df: pl.DataFrame, out) -> None:
    """先写临时文件再原子替换, 避免进程中断留下损坏的 parquet。

    与 repository._atomic_write_parquet 同语义。adj_factor 的 all.parquet 是全市场
    单文件、每次「读→concat→原地写」, 直接 write_parquet(out) 在进程被 kill
    (dev.sh 清端口用 kill -9)、reap 超时或断电时会留下半截文件, 之后复权视图
    scan_parquet 整条链路报错、enriched 全市场重算不出。临时文件后缀 .tmp 不匹配
    *.parquet glob, 不会被扫描误读。
    """
    tmp = out.with_name(out.name + ".tmp")
    df.write_parquet(tmp)
    tmp.replace(out)  # 同目录 rename, POSIX/NTFS 均为原子操作


# 标准列(无论 SDK 返回什么形状,我们把它规范成这套)
CANONICAL_DAILY_COLS = [
    "symbol", "date", "open", "high", "low", "close", "volume", "amount",
]


def _normalize_daily(df_in, default_symbol: str | None = None) -> pl.DataFrame:
    """把 SDK 返回的 pandas/任意 DataFrame 规范成 canonical 列。"""
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
                     on_chunk_done: Callable[[int, int], None] | None = None,
                     failed_out: list[str] | None = None) -> pl.DataFrame:
    """批量拉取多股日 K。

    优先使用 start_time / end_time 区间 + count=10000,确保覆盖完整时间段。
    仅传 count 时按条数回溯。

    failed_out: 可选出参。拉取失败的分块标的会追加进该 list, 供上层判定「部分失败」
                而非静默当成功(某分块断网 → 这些标的本轮未更新, 保持旧数据)。
    """
    tf = get_client()
    out: list[pl.DataFrame] = []
    chunks = chunked(symbols, batch_size)
    failed_syms: list[str] = []

    for i, chunk in enumerate(chunks):
        sleep_between_batches(i, rpm)
        try:
            if start_time and end_time:
                raw = tf.klines.batch(
                    chunk, period="1d", adjust="none",
                    start_time=_datetime_to_ms(start_time),
                    end_time=_datetime_to_ms(end_time),
                    count=10000,
                    as_dataframe=True, show_progress=False,
                )
            else:
                raw = tf.klines.batch(chunk, period="1d", count=count or 250, adjust="none",
                                      as_dataframe=True, show_progress=False)
        except Exception as e:  # noqa: BLE001
            logger.warning("batch fetch failed for %d symbols (chunk %d/%d): %s",
                           len(chunk), i + 1, len(chunks), e)
            failed_syms.extend(chunk)
            continue

        # 兼容两种形态:dict[sym → df] 和扁平 df
        if isinstance(raw, dict):
            for sym, sub in raw.items():
                if sub is None or len(sub) == 0:
                    continue
                out.append(_normalize_daily(sub, default_symbol=sym))
        elif raw is not None and len(raw) > 0:
            out.append(_normalize_daily(raw))

        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))

    # 部分失败可见化: 聚合一条 WARNING(而非只有逐块 debug/warning), 并回传出参。
    if failed_syms:
        logger.warning("日K批量同步部分失败: %d/%d 标的未获取, 本轮保持旧数据 (样例: %s)",
                       len(failed_syms), len(symbols), failed_syms[:10])
        if failed_out is not None:
            failed_out.extend(failed_syms)

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
    if not symbols:
        return 0

    provider_name = preferences.get_daily_data_provider()
    if provider_name != "tickflow":
        from app.data_providers import custom as custom_sources
        if custom_sources.provider_has_dataset(provider_name, "daily"):
            provider = custom_sources.get_provider(provider_name)
            end_time = end_date or datetime.now()
            days = count or 365
            start_time = start_date or (end_time - timedelta(days=days))
            df = provider.get_daily(
                symbols,
                start_time=start_time,
                end_time=end_time,
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
        # 自定义源未配置 daily → 回退 TickFlow

    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return 0

    limit = resolve_limit(capset, Cap.KLINE_DAILY_BATCH, default_batch=100)

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    df = sync_daily_batch(
        symbols, count=count, batch_size=limit.batch, rpm=limit.rpm,
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

    from app.tickflow.client import get_client

    tf = get_client()
    try:
        resp = tf.quotes.get_by_universes(universes=["CN_Equity_A"])
    except Exception as e:
        logger.warning("get_by_universes failed: %s", e)
        return 0

    if not resp:
        logger.warning("get_by_universes returned empty")
        return 0

    records = []
    for q in resp:
        ext = q.get("ext") or {}
        records.append({
            "symbol": q.get("symbol"),
            "open": q.get("open"),
            "high": q.get("high"),
            "low": q.get("low"),
            "close": q.get("last_price"),
            "volume": q.get("volume"),
            "amount": q.get("amount"),
        })

    df = pl.DataFrame(records)
    if df.is_empty():
        return 0

    today = _date.today()
    daily_df = df.with_columns(pl.lit(today).cast(pl.Date).alias("date"))

    # 过滤停牌 (open/high 为 0; close 可能被填充为前收盘价, 不能用全零判断)
    daily_df = filter_halt_days(daily_df)

    repo.flush_live_daily(daily_df)
    logger.info("sync_daily_by_quotes: %d symbols flushed for %s", daily_df.height, today)
    return daily_df.height


def _normalize_adj_factor(raw) -> pl.DataFrame:
    """Normalize SDK ex_factors response to symbol/trade_date/ex_factor."""
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
    """同步除权因子(Starter+)。SDK 接口:`tf.klines.ex_factors(symbols=...)`。

    支持增量: 传 start_time/end_time 只拉取该时间范围内的新除权事件。
    返回 (写入行数, 受影响的 symbol 列表) — 供 enriched 局部重算使用。
    """
    if not symbols:
        return 0, []

    provider_name = preferences.get_adj_factor_provider()
    if provider_name == "same_as_daily":
        provider_name = preferences.get_daily_data_provider()
    if provider_name != "tickflow":
        from app.data_providers import custom as custom_sources
        if custom_sources.provider_has_dataset(provider_name, "adj_factor"):
            provider = custom_sources.get_provider(provider_name)
            new_data = provider.get_adj_factors(
                symbols,
                start_time=start_time,
                end_time=end_time,
                asset_type=asset_type,
                on_chunk_done=on_chunk_done,
            )
            if new_data.is_empty():
                return 0, []
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
                _atomic_write_parquet(merged, out)
                return merged.height - before, affected
            _atomic_write_parquet(new_data.sort(["symbol", "trade_date"]), out)
            return new_data.height, affected
        # 自定义源未配置 adj_factor → 回退 TickFlow

    if not capset.has(Cap.ADJ_FACTOR):
        return 0, []

    tf = get_client()
    limit = resolve_limit(
        capset,
        Cap.ADJ_FACTOR,
        default_batch=50,
        default_rpm=30,
        default_rpm_when_unset=False,
    )

    # 构建 SDK 参数
    sdk_kwargs: dict = {"as_dataframe": True, "batch_size": limit.batch, "show_progress": False}
    if start_time:
        sdk_kwargs["start_time"] = _datetime_to_ms(start_time)
    if end_time:
        sdk_kwargs["end_time"] = _datetime_to_ms(end_time)

    chunks = chunked(symbols, limit.batch)
    all_dfs: list[pl.DataFrame] = []
    failed_syms: list[str] = []

    for i, chunk in enumerate(chunks):
        sleep_between_batches(i, limit.rpm)
        try:
            raw = tf.klines.ex_factors(chunk, **sdk_kwargs)
            normalized = _normalize_adj_factor(raw)
            if not normalized.is_empty():
                all_dfs.append(normalized)
            logger.debug("adj_factor chunk %d/%d: %d symbols", i + 1, len(chunks), len(chunk))
        except Exception as e:  # noqa: BLE001
            logger.warning("adj_factor chunk %d/%d failed: %s", i + 1, len(chunks), e)
            failed_syms.extend(chunk)

        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))

    # 部分失败可见化: 失败分块的标的不在 affected 里 → enriched 不会重算它们,
    # 它们会保持**旧的前复权价**直到下次成功同步。聚合一条 WARNING 让其可见。
    if failed_syms:
        logger.warning("adj_factor 同步部分失败: %d/%d 标的未获取复权因子, 将保持旧复权价 (样例: %s)",
                       len(failed_syms), len(symbols), failed_syms[:10])

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
        _atomic_write_parquet(merged, out)
        added = merged.height - before
        logger.info("adj_factor merged: %d total (+%d new), %d/%d symbols",
                     merged.height, added, new_data.height, len(symbols))
        return added, affected
    else:
        _atomic_write_parquet(new_data.sort(["symbol", "trade_date"]), out)
        logger.info("adj_factor synced: %d rows (%d symbols)", new_data.height, len(symbols))
        return new_data.height, affected


# ===== 分钟 K 同步 =====

CANONICAL_MINUTE_COLS = [
    "symbol", "datetime", "open", "high", "low", "close", "volume", "amount",
]


def _normalize_minute(df_in, default_symbol: str | None = None) -> pl.DataFrame:
    """把 SDK 返回的分钟 K 数据规范成 canonical 列。"""
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
    """datetime → 毫秒时间戳 (供 SDK start_time / end_time 使用)。"""
    return int(dt.timestamp() * 1000)


def _write_minute_partition(df: pl.DataFrame, minute_dir) -> int:
    """按 _trade_date 分区落盘分钟 K (读旧→concat→unique→原子写)。返回写入行数。

    抽自原 sync_and_persist_minute 末尾的循环, 供流式落盘 (每段一次) 与一次性迁移共用。
    """
    if df.is_empty():
        return 0
    df = df.with_columns(pl.col("datetime").dt.date().alias("_trade_date"))
    written = 0
    for day_df in df.partition_by("_trade_date"):
        trade_date = day_df["_trade_date"][0]
        out = minute_dir / f"date={trade_date}" / "part.parquet"
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
        _atomic_write_parquet(day_df, out)
        written += day_df.height
    return written


def _resolve_minute_provider(
    provider_name: str,
) -> tuple[object | None, bool, str | None]:
    """统一解析 custom minute provider, 把所有 resolver 调用纳入同一异常边界。

    供 _try_custom_minute 和 sync_and_persist_minute 共用, 避免两处分别调
    provider_has_dataset / get_provider 时漏掉异常边界 (Issue 2 加固项)。

    返回 (provider, should_fallback_to_tickflow, error_msg):
      - provider_name == "tickflow" 或未配 minute dataset → (None, True, None)  静默降级
      - resolver 异常 (registry 损坏 / 插件失效 / provider name 不存在) → (None, True, str(e))
      - 成功 → (provider, False, None)

    上层依据 error_msg 决定是否 logger.warning (区分"未配"与"异常")。
    注意: provider.get_minute() 仍由调用方在自身 try 块内调用 (业务异常, 非解析异常)。
    """
    if provider_name == "tickflow":
        return (None, True, None)
    from app.data_providers import custom as custom_sources
    try:
        if not custom_sources.provider_has_dataset(provider_name, "minute"):
            return (None, True, None)
        provider = custom_sources.get_provider(provider_name)
        return (provider, False, None)
    except Exception as e:  # noqa: BLE001
        return (None, True, str(e))


def _try_custom_minute(
    symbols: list[str],
    start_time: datetime | None,
    end_time: datetime | None,
    asset_type: AssetType,
    freq: str = "1m",
    on_chunk_done: Callable[[int, int, str], None] | None = None,
) -> tuple[pl.DataFrame | None, bool]:
    """尝试从自定义分钟源拉取。返回 (df, should_fallback_to_tickflow)。

    返回契约:
      (None, True)   → 未配自定义源 / 未配 minute dataset / 自定义源异常 → 走 TickFlow
      (df, False)    → 自定义源成功(含空 df) → 直接用, 不回退

    降级策略 (C): 自定义源异常时无条件 fall through 到 TickFlow,
    由 TickFlow 路径自身 try/except 兜底。Pro+ 用户 TickFlow 成功返回数据,
    None 档用户 TickFlow 失败返回空。不显式判断 tier, 避免 #126 augmented
    capability 逻辑干扰。

    resolver 异常边界由 _resolve_minute_provider 统一兜底; 业务调用
    (provider.get_minute) 仍在本函数 try 块内, 与 resolver 异常分离
    便于日志区分 ("resolution failed" vs "call failed")。

    on_chunk_done 适配: 上层回调是 3 参 (cur, total, seg_label), provider
    实现内部以 2 参 (cur, total) 调用。这里包装一层, provider 调 2 参时补
    默认 seg_label="custom" 转发给上层, 保证进度展示不降级。
    """
    provider_name = preferences.get_minute_data_provider()
    provider, fallback, err = _resolve_minute_provider(provider_name)
    if fallback:
        if err is not None:
            logger.warning("custom minute provider %s resolution failed, falling back to TickFlow: %s",
                           provider_name, err)
        return (None, True)

    # 包装 on_chunk_done: provider 调 2 参 → 补 seg_label="custom" → 转发上层 3 参
    wrapped_cb: Callable[[int, int], None] | None = None
    if on_chunk_done is not None:
        def _wrapped_cb(cur: int, total: int) -> None:
            on_chunk_done(cur, total, "custom")
        wrapped_cb = _wrapped_cb

    try:
        df = provider.get_minute(
            symbols, start_time=start_time, end_time=end_time,
            asset_type=asset_type, freq=freq, on_chunk_done=wrapped_cb,
        )
        return (df, False)
    except Exception as e:  # noqa: BLE001
        logger.warning("custom minute provider %s call failed, falling back to TickFlow: %s",
                       provider_name, e)
        return (None, True)


def sync_minute_batch(
    symbols: list[str],
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    count: int | None = None,
    batch_size: int | None = None,
    rpm: int | None = None,
    on_chunk_done: Callable[[int, int, str], None] | None = None,
    segment_trading_days: int = 20,
    on_segment: Callable[[pl.DataFrame], None] | None = None,
    asset_type: AssetType = "stock",
) -> pl.DataFrame:
    """批量拉取多股分钟 K。

    优先使用 start_time / end_time 区间, 确保所有标的覆盖同一时间段。
    count 仅作为 fallback 保留。
    on_chunk_done(current, total) 每个 chunk 完成后回调。

    segment_trading_days: 单段大小 (交易日), 控制每次 SDK 请求覆盖的天数。
        TickFlow count 上限 10000 根/股, 1 天 240 根 → 物理上限 ~41 交易日;
        默认 20 (4800 根, 安全余量足), 范围建议 [5, 30]。
        段越小: 单次内存峰值越低 (适合小内存机器), 但总批数↑ → 限速 sleep↑ → 更慢。
        段越大: 速度越快, 内存峰值越高。
    on_segment: 每个时间段拉完后回调 (传入该段拼接后的 DataFrame)。
        传入时走「流式落盘」: 段内结果累积到 seg_out, 段末 concat 后回调并清空,
        不进入全局 out → 内存峰值从「全量」降到「单段」。适用于 sync_and_persist_minute。
        不传时 (如 get_minute_batch 的实时补拉) 保持原契约: 累积进 out 末尾一次性返回。
    """
    df, fallback = _try_custom_minute(
        symbols, start_time=start_time, end_time=end_time,
        asset_type=asset_type, freq="1m", on_chunk_done=on_chunk_done,
    )
    if not fallback:
        # 自定义源成功: 遵守与 TickFlow 路径一致的 on_segment 契约。
        # 传了 on_segment (如 sync_and_persist_minute 流式落盘) → 调 on_segment, 返回空 df;
        # 未传 on_segment (如 fetch_minute_single 实时补拉) 或空 df → 原样返回 df。
        df = df if df is not None else pl.DataFrame()
        if on_segment and not df.is_empty():
            # 空 df 不调 on_segment, 与 TickFlow 路径 `if seg_out:` (L684) 对称
            on_segment(df)
            return pl.DataFrame()
        return df

    tf = get_client()

    # TickFlow count 上限 10000 根/股, 1 天 240 根 → 单次最多约 41 个交易日。
    # 按 segment_trading_days 交易日分段 (交易日→自然日 ×7/5 换算, 含节假日余量)。
    seg_calendar_days = max(1, int(segment_trading_days * 7 / 5))
    SEG_CHUNK = timedelta(days=seg_calendar_days)
    time_segments: list[tuple[datetime | None, datetime | None]] = []
    if start_time and end_time:
        seg_start = start_time
        while seg_start < end_time:
            seg_end = min(seg_start + SEG_CHUNK, end_time)
            time_segments.append((seg_start, seg_end))
            seg_start = seg_end
    else:
        time_segments = [(None, None)]  # fallback: 用 count 模式

    total_steps = len(time_segments) * len(chunked(symbols, batch_size))
    step = 0
    # 全局累积 (仅 on_segment=None 时使用, 末尾一次性 concat 返回)
    out: list[pl.DataFrame] = []
    # 段内累积: 每段拉完即 flush, 避免全量攒内存 (OOM 根因)
    seg_out: list[pl.DataFrame] = []

    for seg_idx, (cur_start, cur_end) in enumerate(time_segments):
        # 当前的日期段描述 (供进度展示)
        if cur_start and cur_end:
            seg_label = f"{cur_start.strftime('%m-%d')}~{cur_end.strftime('%m-%d')}"
        else:
            seg_label = "最新"
        seg_total = len(time_segments)
        chunks = chunked(symbols, batch_size)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(step, rpm)
            step += 1
            try:
                if cur_start and cur_end:
                    raw = tf.klines.batch(
                        chunk, period="1m",
                        start_time=_datetime_to_ms(cur_start),
                        end_time=_datetime_to_ms(cur_end),
                        count=10000,
                        adjust="forward",
                        as_dataframe=True, show_progress=False,
                    )
                else:
                    raw = tf.klines.batch(chunk, period="1m", count=count or 1200,
                                          adjust="forward",
                                          as_dataframe=True, show_progress=False)
            except Exception as e:  # noqa: BLE001
                logger.warning("minute batch fetch failed for %d symbols: %s", len(chunk), e)
                continue

            if isinstance(raw, dict):
                for sym, sub in raw.items():
                    if sub is None or len(sub) == 0:
                        continue
                    seg_out.append(_normalize_minute(sub, default_symbol=sym))
            elif raw is not None and len(raw) > 0:
                seg_out.append(_normalize_minute(raw))

            if on_chunk_done:
                on_chunk_done(step, total_steps, seg_label)

        # 段末 flush: 流式落盘回调 或 并入全局 out
        if seg_out:
            if on_segment:
                on_segment(pl.concat(seg_out, how="diagonal_relaxed"))
            else:
                out.extend(seg_out)
            seg_out = []

    if not out:
        return pl.DataFrame()
    return pl.concat(out, how="diagonal_relaxed")


def intraday_monitor_support(capset: CapabilitySet | None) -> dict[str, object]:
    """返回分时信号监控可用的数据能力和单轮标的上限。"""
    provider_name = preferences.get_minute_data_provider()
    _, fallback, error = _resolve_minute_provider(provider_name)
    if not fallback:
        return {
            "available": True, "source": "custom_minute", "max_symbols": 100,
            "reason": "使用已配置的分钟数据插件",
        }
    if error is not None:
        logger.warning("minute provider resolution failed while checking monitor support: %s", error)
    if capset is None:
        return {
            "available": False, "source": None, "max_symbols": 0,
            "reason": "需要分钟 K 或日内分时数据权限",
        }
    for cap, source in (
        (Cap.INTRADAY_BATCH, "intraday_batch"),
        (Cap.KLINE_MINUTE_BATCH, "minute_batch"),
    ):
        if capset.has(cap):
            limits = capset.limits(cap)
            return {
                "available": True, "source": source,
                "max_symbols": max(1, int(limits.batch or 100)) if limits else 100,
                "reason": "日内分时数据可用" if cap == Cap.INTRADAY_BATCH else "分钟 K 数据可用",
            }
    for cap, source in (
        (Cap.INTRADAY, "intraday_single"),
        (Cap.KLINE_MINUTE_BY_SYMBOL, "minute_single"),
    ):
        if capset.has(cap):
            return {
                "available": True, "source": source, "max_symbols": 1,
                "reason": "当前权限仅支持单标的分时监控",
            }
    return {
        "available": False, "source": None, "max_symbols": 0,
        "reason": "需要分钟 K 或日内分时数据权限",
    }


def _normalize_intraday_raw(raw, default_symbol: str | None = None) -> list[pl.DataFrame]:
    frames: list[pl.DataFrame] = []
    if isinstance(raw, dict):
        for symbol, sub in raw.items():
            if sub is not None and len(sub) > 0:
                frames.append(_normalize_minute(sub, default_symbol=str(symbol)))
    elif raw is not None and len(raw) > 0:
        frames.append(_normalize_minute(raw, default_symbol=default_symbol))
    return [frame for frame in frames if not frame.is_empty()]


def fetch_intraday_monitor_batch(
    symbols: list[str], capset: CapabilitySet | None, *, now: datetime | None = None,
) -> pl.DataFrame:
    """按当前能力获取分时信号所需的当日分钟数据，不落盘。"""
    if not symbols:
        return pl.DataFrame()
    support = intraday_monitor_support(capset)
    if not support["available"] or len(symbols) > int(support["max_symbols"]):
        return pl.DataFrame()

    now = now or cn_now()
    start_time = now.replace(hour=9, minute=25, second=0, microsecond=0)
    source = support["source"]
    if source in {"custom_minute", "minute_batch"}:
        limits = capset.limits(Cap.KLINE_MINUTE_BATCH) if capset and capset.has(Cap.KLINE_MINUTE_BATCH) else None
        return sync_minute_batch(
            symbols, start_time=start_time, end_time=now,
            batch_size=limits.batch if limits else None,
            rpm=limits.rpm if limits else None,
        )

    tf = get_client()
    frames: list[pl.DataFrame] = []
    try:
        if source == "intraday_batch":
            limits = capset.limits(Cap.INTRADAY_BATCH) if capset else None
            raw = tf.klines.intraday_batch(
                symbols, count=300, as_dataframe=True, show_progress=False,
                batch_size=limits.batch if limits and limits.batch else 100,
            )
            frames.extend(_normalize_intraday_raw(raw))
        elif source == "intraday_single":
            raw = tf.klines.intraday(symbols[0], count=300, as_dataframe=True)
            frames.extend(_normalize_intraday_raw(raw, default_symbol=symbols[0]))
        elif source == "minute_single":
            raw = tf.klines.get(symbols[0], period="1m", count=300, as_dataframe=True)
            frames.extend(_normalize_intraday_raw(raw, default_symbol=symbols[0]))
    except Exception as e:  # noqa: BLE001
        logger.warning("intraday monitor fetch failed (%s, %d symbols): %s", source, len(symbols), e)
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


def fetch_minute_single(
    symbol: str,
    trade_date: date,
    asset_type: AssetType = "stock",
) -> pl.DataFrame:
    """实时拉取单股单日分钟 K(不写入本地)。优先自定义分钟源, 回退 TickFlow。"""
    from datetime import datetime
    start_time = datetime(trade_date.year, trade_date.month, trade_date.day, 9, 25, 0)
    end_time = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 5, 0)

    # 自定义数据源分流: 与 sync_minute_batch 一致, 配了自定义分钟源时走 custom provider,
    # 避免无 TickFlow Pro+ 权限的用户分时图首次打开(本地无数据)时补拉失败返回空。
    df, fallback = _try_custom_minute(
        [symbol], start_time=start_time, end_time=end_time,
        asset_type=asset_type, freq="1m",
    )
    if not fallback:
        # 见 sync_minute_batch 同分支注释: df 在此必非 None。
        return df if df is not None else pl.DataFrame()

    tf = get_client()
    try:
        raw = tf.klines.batch(
            [symbol], period="1m",
            start_time=_datetime_to_ms(start_time),
            end_time=_datetime_to_ms(end_time),
            count=10000,
            adjust="forward",
            as_dataframe=True, show_progress=False,
        )
    except Exception as e:
        logger.warning("fetch_minute_single(%s, %s) failed: %s", symbol, trade_date, e)
        return pl.DataFrame()

    if isinstance(raw, dict):
        sub = raw.get(symbol)
        return _normalize_minute(sub) if sub is not None and len(sub) > 0 else pl.DataFrame()
    if raw is not None and len(raw) > 0:
        return _normalize_minute(raw)
    return pl.DataFrame()


def fetch_adj_factor_single(symbol: str) -> pl.DataFrame:
    """从 TickFlow 实时拉取单股除权因子(不写入本地), 用于单股 K 线即时前复权。

    返回结构: symbol, trade_date, ex_factor (空 DataFrame 表示无除权事件或拉取失败)。
    与 _apply_adj_factor / compute_enriched 的 factors 参数格式一致。
    """
    tf = get_client()
    try:
        raw = tf.klines.ex_factors([symbol], as_dataframe=True, show_progress=False)
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


def _earliest_minute_datetime(repo: KlineRepository) -> datetime | None:
    """本地分钟 K 数据的最早时间 (用于向前扩展的起点)。"""
    try:
        res = repo.execute_one("SELECT min(datetime) FROM kline_minute")
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
        _atomic_write_parquet(day_df, out)

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
    on_chunk_done: Callable[[int, int, str], None] | None = None,
    extend_backward: bool = False,
) -> int:
    """同步分钟 K 并存到 Parquet(前复权价格, SDK 端 adjust=qfq)。返回写入行数。

    使用 start_time / end_time 区间拉取, 确保所有标的覆盖同一时间段。
    on_chunk_done(current, total) 每个 chunk 完成后回调。
    """
    minute_provider = preferences.get_minute_data_provider()
    # resolver 调用统一走 _resolve_minute_provider, 与 _try_custom_minute 共用异常边界。
    # resolver 异常时视为非 custom (minute_is_custom=False), 走 capset 检查 →
    # sync_minute_batch 内 _try_custom_minute 会再次 resolver 异常 → fallback TickFlow。
    _, fallback, resolve_err = _resolve_minute_provider(minute_provider)
    minute_is_custom = not fallback
    if resolve_err is not None:
        logger.warning("custom minute provider %s resolution failed at sync_and_persist_minute, treating as non-custom: %s",
                       minute_provider, resolve_err)
    if not symbols:
        return 0
    if not minute_is_custom and not capset.has(Cap.KLINE_MINUTE_BATCH):
        return 0

    # 迁移:旧版 _normalize_minute 未转换 timestamp→datetime,导致全部 datetime 为 null
    # 检测到后直接清除(这些数据无法使用)
    _cleanup_null_datetime_minute(repo)

    # 迁移:旧版按 symbol= 分区转为 date= 分区
    _migrate_symbol_to_date_partition(repo)

    now = datetime.now()

    if extend_backward:
        # 向前扩展模式: 从本地最早数据往前补, 叠加已有数据避免缺口。
        earliest_dt = _earliest_minute_datetime(repo)
        # 按交易日换算自然日 (7/5 系数)。>41 交易日时 +10 天余量覆盖节假日。
        # (分段由 sync_minute_batch 的 segment_trading_days 控制, 与此处的区间天数独立。)
        calendar_days = int(days * 7 / 5) + (10 if days > 41 else 0)
        if earliest_dt:
            end_time = earliest_dt
            start_time = end_time - timedelta(days=calendar_days)
        else:
            # 本地无数据 → 从今天往前拉
            start_time = now - timedelta(days=calendar_days)
            end_time = now
    else:
        # 默认增量模式: 首次拉取回溯 N 天, 已有数据则从最新时间增量补到今天
        last_dt = _latest_minute_datetime(repo)
        if last_dt:
            start_time = last_dt
        else:
            start_time = now - timedelta(days=days)
        end_time = now

    limit = resolve_limit(
        capset,
        Cap.KLINE_MINUTE_BATCH,
        default_batch=100,
        default_rpm=30,
        default_rpm_when_unset=False,
    )

    # 流式落盘: 每段拉完立即写盘, 内存峰值 = 单段 (而非全量)。
    # 全量攒内存曾导致 1 年全市场分钟 K OOM 卡死 (3 亿行 / 数十 GB)。
    minute_dir = repo.store.data_dir / "kline_minute"
    written_box = [0]  # list 闭包, 绕过 Python 闭包外层赋值

    def _persist(seg_df: pl.DataFrame) -> None:
        written_box[0] += _write_minute_partition(seg_df, minute_dir)

    segment_days = preferences.get_minute_sync_segment_days()
    sync_minute_batch(
        symbols, start_time=start_time, end_time=end_time,
        batch_size=limit.batch, rpm=limit.rpm,
        on_chunk_done=on_chunk_done,
        segment_trading_days=segment_days,
        on_segment=_persist,
        asset_type="stock",
    )

    if written_box[0] == 0:
        return 0
    written = written_box[0]

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
