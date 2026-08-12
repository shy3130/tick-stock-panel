"""指数 / ETF 数据同步服务。

标的列表优先用 provider.get_instruments(type=index/etf) 拉取；
provider.get_by_universes 可作为补充来源。日K统一走 provider.get_daily。

数据获取通过 data_providers 抽象层,支持 provider 切换。
universes 补充也走 provider 抽象层（FQuantProvider 走 fstore chengfen_gu）。
"""
from __future__ import annotations

import gc
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta

import polars as pl

from app.capabilities import Cap, CapabilitySet
from app.indicators.pipeline import compute_enriched
from app.services import kline_sync, preferences
from app.storage.repository import KlineRepository

logger = logging.getLogger(__name__)

# 复用 kline_sync 的 provider 工厂
_get_data_provider = kline_sync._get_data_provider

# exchanges.get_instruments 查询的交易所(沪深京)
_EXCHANGES = ["SH", "SZ", "BJ"]

# 看板、回测与交易复盘直接依赖的指数必须保留足够长的 canonical 历史。
# 新安装默认只同步近一年；这些小规模基准单独补齐，避免长窗口分析静默截断。
REQUIRED_INDEX_HISTORY_STARTS: dict[str, date] = {
    "000001.INDEX": date(2015, 1, 1),
    "000300.INDEX": date(2015, 1, 1),
    "000905.INDEX": date(2015, 1, 1),
    "399001.INDEX": date(2015, 1, 1),
    "399006.INDEX": date(2015, 1, 1),
    "000688.INDEX": date(2019, 12, 1),
    "000680.INDEX": date(2020, 1, 1),
}


def _quotes_to_index_instruments(resp) -> pl.DataFrame:
    """将 provider universe 响应规范为指数 instruments。

    补充来源为空时返回空 DataFrame。
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


def _fetch_instruments_by_type(
    instrument_type: str,
    asset_type_label: str,
    extra_cols: tuple[str, ...] = (),
) -> pl.DataFrame:
    """通过 data_providers 抽象层拉取指定类型的标的列表。

    None/Free 档均可使用(标的信息查询免费开放)。
    instrument_type: 'index' / 'etf' / 'hk'  (用作 provider.get_instruments 的 asset_type 参数)
    asset_type_label: 写入 instruments 表的 asset_type 标记('index' / 'etf' / 'hk')
    extra_cols: 除 symbol/name/code 外额外保留的列。index/ETF 默认不需要 ——
        它们的 enriched 计算不依赖 instruments 算派生指标。港股需要
        total_shares/float_shares 来算换手率(_attach_turnover_rate),
        原来这里全部裁掉是港股换手率一直算不出来的根因。
    """
    provider = _get_data_provider()
    try:
        df = provider.get_instruments(instrument_type)  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001
        logger.warning("provider.get_instruments(%s) failed: %s", instrument_type, e)
        return pl.DataFrame()

    if df.is_empty() or "symbol" not in df.columns:
        return pl.DataFrame()

    # provider 返回 INSTRUMENT_COLS: symbol/name/code/exchange/asset_type/source(+可选股本列)
    # 这里只取需要的列,并把 asset_type 覆盖为调用方指定的标记
    cols = [
        pl.col("symbol").cast(pl.Utf8),
        pl.col("name").cast(pl.Utf8),
        pl.col("code").cast(pl.Utf8),
    ]
    for c in extra_cols:
        if c in df.columns:
            cols.append(pl.col(c))
    out = df.select(cols).with_columns(pl.lit(asset_type_label).alias("asset_type"))

    return out.unique(subset=["symbol"], keep="last").sort("symbol")


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

    # 2) 用 get_by_universes 补指数(仅当开启指数拉取)
    if pull_index:
        try:
            provider = _get_data_provider()
            # 顶层失败兜底（capabilities 检查 + provider 实现不可用都算）
            if getattr(provider, "capabilities", None) is not None and not provider.capabilities.universes:
                logger.debug("当前 provider 不支持 universes,跳过付费补充")
            else:
                sup = provider.get_by_universes(universes=["CN_Index"], asset_type="index")
                if sup is not None and not sup.is_empty():
                    index_parts.append(sup)
                else:
                    logger.debug("provider.get_by_universes(['CN_Index']) 返回空")
        except Exception as e:  # noqa: BLE001
            logger.warning("provider.get_by_universes(['CN_Index']) 失败: %s", e)

    total = 0
    if index_parts:
        from app.data_providers.fquant.symbols import canonical_index_symbol

        index_inst = (
            pl.concat(index_parts, how="diagonal_relaxed")
            .with_columns(
                pl.col("symbol")
                .cast(pl.Utf8)
                .map_elements(canonical_index_symbol, return_dtype=pl.Utf8)
            )
            .unique(subset=["symbol"], keep="last")
            .sort("symbol")
        )
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
    """单独同步 ETF 标的维表(返回 ETF 数量)。

    带 total_shares/float_shares(extra_cols)—— enriched 计算换手率要用,
    与港股同一个修复(原先 _fetch_instruments_by_type 默认裁剪掉这两列,
    导致 ETF 换手率一直算不出来)。
    """
    etf_df = _fetch_instruments_by_type("etf", "etf", extra_cols=("total_shares", "float_shares"))
    if etf_df.is_empty():
        return 0
    repo.save_etf_instruments(etf_df)
    repo.refresh_index_views()
    return etf_df.height


def sync_hk_instruments(repo: KlineRepository) -> int:
    """单独同步港股标的维表(返回港股数量)。

    带 total_shares/float_shares(extra_cols)—— enriched 计算换手率要用,
    与 index/ETF 的默认窄投影不同,见 _fetch_instruments_by_type 的参数说明。
    """
    hk_df = _fetch_instruments_by_type("hk", "hk", extra_cols=("total_shares", "float_shares"))
    if hk_df.is_empty():
        return 0
    repo.save_hk_instruments(hk_df)
    repo.refresh_index_views()
    return hk_df.height


def sync_and_persist_hk_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    count: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    symbols_override: list[str] | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> int:
    """同步港股日K到独立 kline_hk_* parquet,并计算港股 enriched。

    与 ETF 的关键差别:
      - factors=None(不复权)—— 本地无港股除权数据源(chuquan_chuxi 只有 6 位 A 股
        代码,tdx-hk 无 xdxr 表,已实测确认)。raw_close 因此等于 close。
      - instruments=hk_instruments, asset_type="hk"(而非 ETF 那样传 None)——
        港股 instruments 带 float_shares,要让 compute_all 算出换手率;
        asset_type="hk" 保证 compute_all 走 `else` 分支只挂换手率、不套 A 股
        涨跌停/连板逻辑(该逻辑只在 asset_type=="stock" 时触发)。
    """
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return 0

    if symbols_override:
        symbols = sorted(set(s for s in symbols_override if s))
        instruments = repo.get_hk_instruments()
    else:
        instruments = repo.get_hk_instruments()
        if instruments.is_empty():
            sync_hk_instruments(repo)
            instruments = repo.get_hk_instruments()
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return 0
        symbols = sorted(set(instruments["symbol"].to_list()))
    if not symbols:
        return 0

    lim = capset.limits(Cap.KLINE_DAILY_BATCH)
    batch_size = preferences.get_index_daily_batch_size()
    if lim and lim.batch:
        batch_size = min(batch_size, lim.batch)
    rpm = lim.rpm if lim else None

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    total_rows = 0
    interval = (60.0 / rpm) if rpm else 0
    chunks = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
    failed_symbols: list[str] = []
    for i, chunk in enumerate(chunks):
        if i > 0 and interval > 0 and len(chunks) > rpm:
            import time
            time.sleep(interval)
        try:
            raw = kline_sync.sync_daily_batch(
                chunk,
                count=count,
                batch_size=None,
                start_time=start_time,
                end_time=end_time,
                asset_type="hk",
            )
            if raw.is_empty():
                continue

            repo.append_hk_daily(raw)
            chunk_instruments = instruments.filter(pl.col("symbol").is_in(chunk)) if not instruments.is_empty() else instruments
            enriched = compute_enriched(raw, factors=None, instruments=chunk_instruments, asset_type="hk")
            repo.append_hk_enriched(enriched)
            total_rows += raw.height
            logger.info("hk daily synced: %d/%d chunks, +%d rows", i + 1, len(chunks), raw.height)
        except Exception:
            failed_symbols.extend(chunk)
            logger.exception(
                "hk daily sync failed for chunk %d/%d (%d symbols, e.g. %s) — "
                "raw 可能已落盘但 enriched 未写入,跳过该批继续处理剩余标的",
                i + 1, len(chunks), len(chunk), chunk[:5],
            )
            continue
        finally:
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
            gc.collect()
    if failed_symbols:
        logger.warning("hk daily sync: %d symbols failed, %s", len(failed_symbols), failed_symbols[:20])
    repo.refresh_index_views()
    return total_rows


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

    from app.data_providers.fquant.symbols import canonical_index_symbol

    if symbols_override:
        symbols = sorted({canonical_index_symbol(s) for s in symbols_override if s})
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
        symbols = sorted({canonical_index_symbol(s) for s in instruments["symbol"].to_list()})
    lim = capset.limits(Cap.KLINE_DAILY_BATCH)
    batch_size = preferences.get_index_daily_batch_size()
    if lim and lim.batch:
        batch_size = min(batch_size, lim.batch)
    rpm = lim.rpm if lim else None

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    total_rows = 0
    interval = (60.0 / rpm) if rpm else 0
    chunks = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
    failed_symbols: list[str] = []
    for i, chunk in enumerate(chunks):
        if i > 0 and interval > 0 and len(chunks) > rpm:
            import time
            time.sleep(interval)
        try:
            raw = kline_sync.sync_daily_batch(
                chunk,
                count=count,
                batch_size=None,
                start_time=start_time,
                end_time=end_time,
                asset_type="index",
            )
            if raw.is_empty():
                continue

            repo.append_index_daily(raw)
            enriched = compute_enriched(raw, factors=None, instruments=None)
            repo.append_index_enriched(enriched)
            total_rows += raw.height
            logger.info("index/etf daily synced: %d/%d chunks, +%d rows", i + 1, len(chunks), raw.height)
        except Exception:
            failed_symbols.extend(chunk)
            logger.exception(
                "index/etf daily sync failed for chunk %d/%d (%d symbols, e.g. %s) — "
                "raw 可能已落盘但 enriched 未写入,跳过该批继续处理剩余标的",
                i + 1, len(chunks), len(chunk), chunk[:5],
            )
            continue
        finally:
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
            gc.collect()
    if failed_symbols:
        logger.warning("index/etf daily sync: %d symbols failed, %s", len(failed_symbols), failed_symbols[:20])
    repo.refresh_index_views()
    return total_rows


def ensure_required_index_history(
    repo: KlineRepository,
    capset: CapabilitySet,
    *,
    end_date: datetime | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> int:
    """补齐关键指数 canonical 长历史；已有早期历史时零网络请求。"""
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return 0

    missing: list[str] = []
    for symbol, required_start in REQUIRED_INDEX_HISTORY_STARTS.items():
        probe_end = required_start + timedelta(days=45)
        try:
            probe = repo.get_index_daily(
                symbol,
                required_start,
                probe_end,
                columns=["date"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("probe index history failed for %s: %s", symbol, exc)
            probe = pl.DataFrame()
        if probe is None or probe.is_empty():
            missing.append(symbol)

    if not missing:
        return 0

    start = min(REQUIRED_INDEX_HISTORY_STARTS[symbol] for symbol in missing)
    logger.info(
        "backfilling canonical index history: symbols=%s start=%s",
        missing,
        start,
    )
    return sync_and_persist_index_daily(
        repo,
        capset,
        symbols_override=missing,
        start_date=datetime.combine(start, datetime.min.time()),
        end_date=end_date or datetime.now(),
        on_chunk_done=on_chunk_done,
    )


def _load_etf_factors(repo: KlineRepository) -> pl.DataFrame:
    factor_path = repo.store.data_dir / "adj_factor_etf" / "all.parquet"
    if not factor_path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(factor_path)
    except Exception as e:  # noqa: BLE001
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
        instruments = repo.get_etf_instruments()
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

    lim = capset.limits(Cap.KLINE_DAILY_BATCH)
    batch_size = preferences.get_index_daily_batch_size()
    if lim and lim.batch:
        batch_size = min(batch_size, lim.batch)
    rpm = lim.rpm if lim else None

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    total_rows = 0
    interval = (60.0 / rpm) if rpm else 0
    chunks = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
    factors = _load_etf_factors(repo)
    failed_symbols: list[str] = []
    for i, chunk in enumerate(chunks):
        if i > 0 and interval > 0 and len(chunks) > rpm:
            import time
            time.sleep(interval)
        try:
            raw = kline_sync.sync_daily_batch(
                chunk,
                count=count,
                batch_size=None,
                start_time=start_time,
                end_time=end_time,
                asset_type="etf",
            )
            if raw.is_empty():
                continue

            repo.append_etf_daily(raw)
            batch_factors = factors.filter(pl.col("symbol").is_in(chunk)) if not factors.is_empty() else factors
            # ETF 使用复权和通用技术指标；instruments+asset_type="etf" 只触发换手率
            # 计算(_attach_turnover_rate),不会套用 A 股涨跌停/连板逻辑 —— 那只在
            # asset_type=="stock" 时才触发(compute_all 的分支判断)。
            # 换手率原先算不出来是因为这里传 instruments=None 整个跳过了该分支,
            # 与港股同一个根因,修法也相同(见 sync_and_persist_hk_daily)。
            chunk_instruments = instruments.filter(pl.col("symbol").is_in(chunk)) if not instruments.is_empty() else instruments
            enriched = compute_enriched(raw, factors=batch_factors, instruments=chunk_instruments, asset_type="etf")
            repo.append_etf_enriched(enriched)
            total_rows += raw.height
            logger.info("etf daily synced: %d/%d chunks, +%d rows", i + 1, len(chunks), raw.height)
        except Exception:
            failed_symbols.extend(chunk)
            logger.exception(
                "etf daily sync failed for chunk %d/%d (%d symbols, e.g. %s) — "
                "raw 可能已落盘但 enriched 未写入,跳过该批继续处理剩余标的",
                i + 1, len(chunks), len(chunk), chunk[:5],
            )
            continue
        finally:
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
            gc.collect()
    if failed_symbols:
        logger.warning("etf daily sync: %d symbols failed, %s", len(failed_symbols), failed_symbols[:20])
    repo.refresh_index_views()
    return total_rows
