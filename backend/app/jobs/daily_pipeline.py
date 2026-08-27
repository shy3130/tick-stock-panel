"""盘后管道 + 盘前维表同步。

调度:
  09:10 盘前 — 同步个股维表 instruments (全量覆盖)
  15:30 盘后 — 日K同步 + 增量除权因子 + enriched 计算 + 刷新视图

盘后同步策略:
  日 K: QuoteService 交易时段已实时落盘 → 有数据时跳过 batch,首次拉 1 年区间
  除权因子: 从已有数据最新日期的下一天开始增量获取,避免重复拉取和计算
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable
from datetime import date as date_type
from pathlib import Path

import polars as pl
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.indicators.pipeline import run_pipeline, run_pipeline_local, run_pipeline_local_incremental
from app.config import settings
from app.services import index_sync, instrument_sync, kline_sync, preferences as _prefs
from app.services.data_mode import is_local_daily_mode
from app.capabilities import Cap, CapabilitySet
from app.data_providers.fquant.catalog_resolver import CatalogError
from app.storage.repository import KlineRepository
from app.jobs.backtest_favorite_rerun import (
    BACKTEST_RERUN_JOB_ID,
    get_backtest_auto_rerun,
)

logger = logging.getLogger(__name__)

ProgressCb = Callable[..., None]

_DEMO_SYMBOLS = (
    "600000.SH",
    "600036.SH",
    "600519.SH",
    "601318.SH",
    "601398.SH",
    "000001.SZ",
    "000333.SZ",
    "000651.SZ",
    "000858.SZ",
    "002594.SZ",
)

_BOOTSTRAP_SAMPLE_SYMBOLS = (
    "600519.SH",
    "000001.SZ",
    "300750.SZ",
    "601318.SH",
    "000333.SZ",
    "600036.SH",
    "002594.SZ",
    "600000.SH",
    "000651.SZ",
    "600030.SH",
)
_BOOTSTRAP_MIN_SAMPLE_COVERAGE = 0.8
_BOOTSTRAP_MIN_PARTITION_COVERAGE = 0.9


def _noop(stage: str, pct: int, msg: str, **kwargs) -> None:  # noqa: ARG001
    pass


def _invalidate(table: str | None = None) -> None:
    """stage 写完调用,让 /api/data/status 只重算被影响的那张表。

    同步失效总览聚合缓存,避免 QuoteService 未运行时 300s 静态窗口继续返回旧聚合。
    """
    from app.api.data import invalidate_data_cache

    invalidate_data_cache(table)
    try:
        from app.api.overview import invalidate_overview_cache

        invalidate_overview_cache()
    except Exception:  # noqa: BLE001
        logger.debug("overview cache invalidate failed", exc_info=True)


def _resolve_universe(capset: CapabilitySet) -> list[str]:
    """解析标的池 — 以 CN_Equity_A (沪深京A股 ~5522只) 为主。

    有 batch 能力 → 直接拉 CN_Equity_A universe
    其他用户 → 用 instruments parquet + watchlist 兜底
    """
    if capset.has(Cap.KLINE_DAILY_BATCH):
        try:
            provider = kline_sync._get_data_provider()
            if provider.capabilities.instruments:
                inst = provider.get_instruments("stock")
                if not inst.is_empty() and "symbol" in inst.columns:
                    return sorted(inst["symbol"].cast(pl.Utf8).to_list())
        except Exception as e:  # noqa: BLE001
            logger.warning("provider instruments pool unavailable, fallback: %s", e)

    # Free 用户兜底: instruments parquet + watchlist + demo
    base: set[str] = set(_DEMO_SYMBOLS)
    base.update(_load_watchlist_symbols())
    d = Path(settings.data_dir)
    inst_path = d / "instruments" / "instruments.parquet"
    if inst_path.exists():
        try:
            inst = pl.read_parquet(inst_path, columns=["symbol"])
            base.update(inst["symbol"].to_list())
        except Exception as e:  # noqa: BLE001
            logger.warning("instruments supplement failed: %s", e)
    return sorted(base)


def _load_watchlist_symbols() -> list[str]:
    path = Path(settings.data_dir) / "user_data" / "watchlist.parquet"
    if not path.exists():
        return []
    try:
        df = pl.read_parquet(path, columns=["symbol"])
    except Exception as e:  # noqa: BLE001
        logger.warning("watchlist supplement failed: %s", e)
        return []
    if df.is_empty() or "symbol" not in df.columns:
        return []
    return df["symbol"].cast(pl.Utf8).to_list()


def run_instruments_sync(repo: KlineRepository) -> dict:
    """盘前同步个股维表。

    sync_instruments 直接写 instruments.parquet, 不经过 repo.save_*; 写完后
    必须刷新 repo 内存缓存, 否则 get_instruments() 在下次进程级 refresh_cache 前
    持续返回旧数据。复用 repo.refresh_instruments_cache() 公开方法。
    """
    rows = instrument_sync.sync_instruments(repo.store.data_dir)
    _refresh_instruments_view(repo)
    refresh_inst = getattr(repo, "refresh_instruments_cache", None)
    if callable(refresh_inst):
        refresh_inst()
    _invalidate("instruments")
    return {"instruments_rows": rows}


def run_now(
    repo: KlineRepository,
    capset: CapabilitySet,
    on_progress: ProgressCb | None = None,
) -> dict:
    """立即执行一次盘后管道,支持进度回调。

    跳过的 stage **不 emit**,避免前端把"无 capability"的卡片错误标记为 active/done。
    result 里带 skipped_stages 列表供前端展示。
    """
    emit = on_progress or _noop
    skipped: list[str] = []
    failed_stages: list[dict[str, str]] = []

    # Step -1: 刷新 fstore provider 连接，确保读到最新 generation 快照
    # （FStoreDuckDBClient 长连接不跟随 generation 切换，进程跑久后会读到旧快照；
    #  同时刷新 markets 客户端，保证 realtime 读新快照）
    try:
        provider = kline_sync._get_data_provider()
        refresh = getattr(provider, "refresh_fstore_clients", None)
        if callable(refresh):
            refresh()
    except Exception:  # noqa: BLE001
        logger.debug("fstore refresh before pipeline failed", exc_info=True)

    # Step 0: 先同步个股维表, 再解析标的池 — 确保标的池基于最新 instruments
    inst_rows = instrument_sync.sync_instruments(repo.store.data_dir)
    if inst_rows > 0:
        _refresh_instruments_view(repo)
    emit("sync_instruments", 8, f"个股维表同步完成,{inst_rows} 只标的")
    _invalidate("instruments")

    emit("resolve_universe", 9, "解析标的池…")
    universe = _resolve_universe(capset)
    emit("resolve_universe", 10, f"标的池规模:{len(universe)} 只")

    # Step 1: 日 K 同步
    #   付费档 + 今天有数据 → 实时行情接口拉一次覆写（1请求全市场）
    #   有历史数据 → batch K-line API 补齐缺口
    #   无任何数据 → batch K-line API 拉首次 1 年
    from datetime import date as _date, timedelta as _td, datetime as _dt

    latest_daily = repo.latest_daily_date()
    today = _date.today()
    today_exists = latest_daily and latest_daily >= today
    new_daily_days = 0
    # 日K范围拉取的起点(分支3补缺口/分支4首次); 实时增量/跳过时为 None。
    # 供 Step 1.5 除权因子回溯范围对齐: 范围拉取→用日K范围, 非范围→最近N天兜底。
    daily_range_start: _date | None = None
    local_daily_mode = is_local_daily_mode()

    # A 股日K拉取开关(默认开);关闭时跳过日K同步,保留已有数据
    pull_a_share = _prefs.get_pipeline_pull_a_share()
    if not pull_a_share:
        emit("sync_daily", 45, "已跳过 A 股日K同步(拉取内容未勾选)")
        logger.info("sync_daily: skipped (pipeline_pull_a_share=False)")
    elif local_daily_mode:
        # fquant_local 直接从 TDX/fstore 读日线并计算 enriched，不建立 raw kline_daily 镜像。
        start_date = _latest_enriched_date(repo) or latest_daily or (today - _td(days=365))
        daily_range_start = start_date
        new_daily_days = max(1, (today - start_date).days)
        emit("sync_daily", 45, f"本地磁盘日K模式,跳过 raw 写入 [{start_date} ~ {today}]")
        logger.info(
            "sync_daily: skipped raw mirror in local disk mode [%s ~ %s]", start_date, today
        )
    elif today_exists and capset.has(Cap.QUOTE_POOL):
        # 付费档:今天有数据(QuoteService 已落盘)→ 实时行情覆写,确保最新。
        # free/none 档无 quote.pool 能力,即便今天已有数据(如从 expert 降级),
        # 也降级到下方 batch 路径刷新,避免调用无权限的实时行情接口。
        emit("sync_daily", 12, f"获取日K [{today} ~ {today}] 实时行情…")
        written_daily = kline_sync.sync_daily_by_quotes(repo)
        new_daily_days = 1
        emit("sync_daily", 45, f"日K 完成,{written_daily} 只标的")
        logger.info("sync_daily: [%s ~ %s] live quotes, %d symbols", today, today, written_daily)
    elif latest_daily:
        # 有历史 → batch 补齐缺口。
        # 也覆盖"今天已有数据但无实时行情权限(free/none)"的降级场景:
        #   此时 start_date = latest_daily = today,batch 刷新当天日K。
        start_date = latest_daily
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info(
            "sync_daily: [%s ~ %s] %s",
            start_date,
            today,
            "refresh today" if today_exists else "gap fill",
        )

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit(
                "sync_daily",
                12 + int(33 * cur / tot),
                f"日K 批次 {cur}/{tot}",
                stage_pct=int(100 * cur / tot),
                skip_log=True,
            )

        written_daily = kline_sync.sync_and_persist_daily_batch(
            universe,
            repo,
            capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.combine(today, _dt.min.time()),
            on_chunk_done=_daily_chunk_progress,
        )
        gap_days = (today - start_date).days
        new_daily_days = gap_days
        emit("sync_daily", 45, f"日K 完成,覆盖 {gap_days} 天")
        logger.info("sync_daily: [%s ~ %s] done, %d days", start_date, today, gap_days)
    else:
        # 首次：无任何数据 → batch 拉 1 年
        start_date = today - _td(days=365)
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] initial fetch", start_date, today)

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit(
                "sync_daily",
                12 + int(33 * cur / tot),
                f"日K 批次 {cur}/{tot}",
                stage_pct=int(100 * cur / tot),
                skip_log=True,
            )

        written_daily = kline_sync.sync_and_persist_daily_batch(
            universe,
            repo,
            capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.combine(today, _dt.min.time()),
            on_chunk_done=_daily_chunk_progress,
        )
        new_daily_days = 365
        emit("sync_daily", 45, "日K 完成")
        logger.info("sync_daily: [%s ~ %s] done", start_date, today)
    _invalidate("daily")

    # Step 1.5: 同步除权因子 — 范围与日K拉取方式对齐
    #   日K范围拉取(补缺口/首次) → 除权用日K范围 [daily_range_start, now]
    #     首次会覆盖整个日K区间内的历史除权事件; 补缺口天然只增量(起点=latest_daily≈昨天)
    #   日K实时增量/跳过(分支2/分支1) → 除权兜底拉最近 30 天, 补可能遗漏的新除权
    #     (这两类分支不拉历史日K, 除权不能用日K范围, 只能兜底最近几日)
    written_adj = 0
    affected_symbols: list[str] = []
    if capset.has(Cap.ADJ_FACTOR) and not local_daily_mode:
        from datetime import datetime, timedelta

        adj_end = datetime.now()
        if daily_range_start is not None:
            adj_start = datetime.combine(daily_range_start, datetime.min.time())
        else:
            # 日K实时增量/跳过时, 除权兜底拉最近 N 天, 覆盖周末/长假/停机期间的新除权事件。
            # 15 天: 覆盖春节/国庆最长约10天长假 + 故障恢复缓冲; sync_adj_factor 内部 merge+unique 幂等, 多拉无副作用。
            adj_start = adj_end - timedelta(days=15)
        adj_start_str = adj_start.strftime("%Y-%m-%d")
        adj_end_str = adj_end.strftime("%Y-%m-%d")
        emit("sync_adj", 50, f"获取除权因子 [{adj_start_str} ~ {adj_end_str}]…")
        logger.info("sync_adj: [%s ~ %s] start", adj_start_str, adj_end_str)

        def _adj_chunk_progress(cur: int, tot: int) -> None:
            emit(
                "sync_adj",
                50 + int(10 * cur / tot),
                f"除权因子批次 {cur}/{tot}",
                stage_pct=int(100 * cur / tot),
                skip_log=True,
            )

        written_adj, affected_symbols = kline_sync.sync_adj_factor(
            universe,
            repo,
            capset,
            start_time=adj_start,
            end_time=adj_end,
            on_chunk_done=_adj_chunk_progress,
        )
        if affected_symbols:
            _refresh_single_view(repo, "adj_factor")
            emit("sync_adj", 60, f"除权因子完成,新增 {len(affected_symbols)} 只个股")
            logger.info(
                "sync_adj: [%s ~ %s] done, %d symbols",
                adj_start_str,
                adj_end_str,
                len(affected_symbols),
            )
        else:
            emit("sync_adj", 60, "除权因子完成,无新增")
            logger.info("sync_adj: [%s ~ %s] no new factors", adj_start_str, adj_end_str)
        _invalidate("adj_factor")
    elif local_daily_mode:
        skipped.append("sync_adj")
        logger.info(
            "sync_adj skipped: local disk mode uses provider factors during enriched compute"
        )
    else:
        skipped.append("sync_adj")
        logger.info("sync_adj skipped: no ADJ_FACTOR capability")

    # Step 2: 计算 enriched
    #   判断策略:
    #     - 首次 (enriched 目录不存在) → 全量
    #     - 往前扩展历史 (新日期 < enriched 已有最早日期) → 全量
    #       前面的除权因子会改变累积因子链,影响后面所有日期的复权价格
    #     - 往后新增日期 (新日期 > enriched 已有最晚日期)
    #       → 增量补新区块(所有标的) + 受除权影响个股全日期重算
    #     - 无新日期 + 有新除权因子 → 增量: 只重算受影响个股的全部日期
    #     - 无新日期 + 无变化 → 跳过
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    enriched_exists = enriched_dir.exists() and any(enriched_dir.glob("date=*"))
    daily_dir = repo.store.data_dir / "kline_daily"
    daily_days = len(list(daily_dir.glob("date=*"))) if daily_dir.exists() else 0
    prev_enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_exists else 0

    # 判断新日期方向: 找 daily 和 enriched 的日期集合做比较
    forward_incremental = False
    backward_extension = False

    if daily_days > prev_enriched_days and enriched_exists:
        daily_dates = sorted(d.stem.split("=")[1] for d in daily_dir.glob("date=*"))
        enriched_dates = sorted(d.stem.split("=")[1] for d in enriched_dir.glob("date=*"))
        earliest_enriched = enriched_dates[0]
        latest_enriched = enriched_dates[-1]
        new_dates = set(daily_dates) - set(enriched_dates)
        if new_dates:
            # 有新日期早于 enriched 最早日期 → 往前扩展
            if any(d < earliest_enriched for d in new_dates):
                backward_extension = True
            # 有新日期晚于 enriched 最晚日期 → 往后新增
            if any(d > latest_enriched for d in new_dates):
                forward_incremental = True

    def _enriched_batch_progress(cur: int, tot: int) -> None:
        emit(
            "compute_enriched",
            65 + int(23 * cur / tot),
            f"计算指标 批次 {cur}/{tot}",
            stage_pct=int(100 * cur / tot),
            skip_log=True,
        )

    if local_daily_mode and pull_a_share:
        emit("compute_enriched", 65, "本地磁盘计算 enriched…")
        try:
            from app.data_providers import get_provider
            from app.data_providers.registry import get_active_provider_name

            provider = get_provider(get_active_provider_name("daily"))
            local_start = _dt.combine(daily_range_start or today - _td(days=365), _dt.min.time())
            local_end = _dt.combine(today, _dt.min.time())
            written_enriched = run_pipeline_local(
                provider,
                data_dir=repo.store.data_dir,
                symbols=universe,
                start_time=local_start,
                end_time=local_end,
                on_batch_done=_enriched_batch_progress,
            )
        except Exception:
            logger.exception("compute_enriched local disk failed")
            raise
        new_enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_dir.exists() else 0
        emit(
            "compute_enriched",
            88,
            f"enriched 完成,写入 {written_enriched} 行/覆盖 {new_enriched_days} 天",
        )
        logger.info(
            "compute_enriched: local disk done, rows=%d days=%d",
            written_enriched,
            new_enriched_days,
        )
    elif not enriched_exists or backward_extension:
        # 首次 或 往前扩展 → 全量
        emit("compute_enriched", 65, "全量计算 enriched…")
        logger.info(
            "compute_enriched: full rebuild (first=%s, backward=%s, daily=%d, enriched=%d)",
            not enriched_exists,
            backward_extension,
            daily_days,
            prev_enriched_days,
        )
        written_enriched = run_pipeline(on_batch_done=_enriched_batch_progress)
        new_enriched_days = len(list(enriched_dir.glob("date=*")))
        emit("compute_enriched", 88, f"enriched 完成,覆盖 {new_enriched_days} 天")
        logger.info("compute_enriched: full rebuild done, %d days", new_enriched_days)

        # 生产接线：历史股本 point-in-time 换手率重算 (覆盖 enriched.turnover_rate 小数)
        # 仅 full rebuild/repair 路径调用；保留 staging rebuild 和 price limit 日期规则
        from app.share_capital import recompute_historical_turnover

        try:
            recompute_historical_turnover(repo)
            emit("compute_enriched", 90, "历史换手率 point-in-time 更新完成")
        except Exception as e:  # noqa: BLE001
            logger.warning("historical turnover recompute failed (non-fatal): %s", e)

    elif forward_incremental:
        # 往后新增日期: 增量补新区块 + 受影响个股全日期重算
        symbols_to_recompute = list(set(affected_symbols)) if affected_symbols else []
        emit(
            "compute_enriched",
            65,
            f"增量计算 enriched (新日期 + {len(symbols_to_recompute)} 只个股重算)…"
            if symbols_to_recompute
            else "增量计算 enriched (新日期)…",
        )
        logger.info(
            "compute_enriched: forward incremental, %d symbols to recompute",
            len(symbols_to_recompute),
        )
        written_enriched = run_pipeline(
            new_dates_only=True,
            symbols=symbols_to_recompute or None,
            on_batch_done=_enriched_batch_progress,
        )
        new_enriched_days = len(list(enriched_dir.glob("date=*")))
        emit("compute_enriched", 88, f"enriched 完成,覆盖 {new_enriched_days} 天")
        logger.info("compute_enriched: forward incremental done, %d days", new_enriched_days)
    elif affected_symbols:
        # 无新日期,仅除权因子变更 → 只重算受影响个股的全部日期
        emit("compute_enriched", 65, f"增量计算 enriched ({len(affected_symbols)} 只个股)…")
        logger.info("compute_enriched: adj_factor incremental, %d symbols", len(affected_symbols))
        written_enriched = run_pipeline(
            symbols=affected_symbols, on_batch_done=_enriched_batch_progress
        )
        emit("compute_enriched", 88, f"enriched 完成,{len(affected_symbols)} 只个股")
    else:
        written_enriched = 0
        logger.info("compute_enriched: skip (no new daily, no adj_factor changes)")
    _refresh_single_view(repo, "kline_enriched")
    _invalidate("enriched")

    if written_enriched > 0:
        # repair/incremental enriched 重算路径也必须使用 point-in-time 股本 (contract)
        # 仅在有实际 enriched 工作时调用，并传 symbols 过滤 (避免全库扫描退化)
        try:
            from app.share_capital import recompute_historical_turnover

            recompute_symbols = (
                symbols_to_recompute
                if "symbols_to_recompute" in locals()
                else (affected_symbols if "affected_symbols" in locals() else None)
            )
            recompute_historical_turnover(repo, symbols=recompute_symbols)
        except Exception as e:  # noqa: BLE001
            logger.warning("historical turnover recompute non-fatal for incremental/repair: %s", e)

    # 本地盘后分区只有通过 provider freshness + 相邻分区覆盖率校验后才可见。
    # 发布后立即刷新 repo，使同一轮 regime / strategy cache 也读取新 canonical 日。
    if local_daily_mode and pull_a_share:
        canonical_date = initialize_local_enriched_ceiling(repo)
        if canonical_date is not None:
            repo.refresh_cache()
            try:
                from app.services.canonical_history import canonical_history_manager

                publish_result = (
                    canonical_history_manager().publish_incremental_from_local(
                        repo,
                        canonical_date,
                    )
                )
                logger.info(
                    "canonical incremental publish: %s",
                    publish_result,
                )
            except Exception:
                logger.warning(
                    "canonical incremental publish failed; local canonical remains valid",
                    exc_info=True,
                )

    # Step 2.2: regime 增量计算 — enriched 完成后补算环境时序。
    # 非致命失败: 记录警告但不中断主 pipeline。
    try:
        from app.services import regime_builder

        regime_builder.compute_regime_incremental(repo, repo.store.data_dir)
        from app.api.regime import invalidate_regime_cache

        invalidate_regime_cache()
        emit("compute_enriched", 90, "环境时序(regime)增量计算完成")
    except Exception as e:  # noqa: BLE001
        logger.warning("regime incremental compute non-fatal: %s", e)

    # Step 2.15: 信号记分卡 — post-enriched 实例化 + pending 评估 (opt-in, 非致命)。
    # tracked_signals 默认空 (用户在设置页显式开启); 失败不得阻断主管道。
    try:
        from app.jobs import signal_scorecard_job

        _tracked = _prefs.get_tracked_signals()
        if _tracked:
            signal_scorecard_job.generate_instances(repo, repo.store.data_dir, _tracked, today)
            signal_scorecard_job.evaluate_pending(repo, repo.store.data_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("signal scorecard hook non-fatal: %s", e)

    # Step 2.3: 指数 / ETF / 港股同步 — 物理分开存储。
    # ETF 会尝试同步复权因子，但本地事件仅覆盖极少数标的，不能视作完整复权；
    # 港股则无本地除权数据源，保持未复权（见 sync_and_persist_hk_daily 注释）。
    written_index_daily = 0
    written_etf_daily = 0
    written_hk_daily = 0
    index_count = 0
    etf_count = 0
    etf_adj_symbols = 0
    hk_count = 0
    pull_index = _prefs.get_pipeline_pull_index()
    pull_etf = _prefs.get_pipeline_pull_etf()
    pull_hk = _prefs.get_pipeline_pull_hk()

    if capset.has(Cap.KLINE_DAILY_BATCH) and (pull_index or pull_etf or pull_hk):
        _types = []
        if pull_index:
            _types.append("指数")
        if pull_etf:
            _types.append("ETF")
        if pull_hk:
            _types.append("港股")
        emit("sync_index", 88, f"同步{'+'.join(_types)}日K…")
        # 子阶段进度分配: 88.0(开始) → 89.0(完成), 指数占前半, ETF 占后半
        try:
            if pull_index:
                emit("sync_index", 88, "同步指数维表…")
                index_count = index_sync.sync_index_instruments(
                    repo, pull_index=True, pull_etf=False
                )
                emit("sync_index", 88, f"指数维表完成,{index_count} 只")
                backfilled_index_daily = index_sync.ensure_required_index_history(
                    repo,
                    capset,
                    end_date=_dt.combine(today, _dt.min.time()),
                )
                written_index_daily += backfilled_index_daily
                if backfilled_index_daily:
                    emit(
                        "sync_index",
                        88,
                        f"关键指数长历史补齐,{backfilled_index_daily} 行",
                    )
                index_dir = repo.store.data_dir / "kline_index_enriched"
                index_dates = (
                    sorted(
                        d.name[5:]
                        for d in index_dir.glob("date=*")
                        if d.is_dir() and d.name.startswith("date=")
                    )
                    if index_dir.exists()
                    else []
                )
                index_start = (
                    _date.fromisoformat(index_dates[-1]) if index_dates else today - _td(days=365)
                )

                def _index_chunk(cur: int, tot: int) -> None:
                    emit(
                        "sync_index",
                        88,
                        f"指数日K批次 {cur}/{tot}",
                        stage_pct=int(100 * cur / tot) if tot else 100,
                        skip_log=cur < tot,
                    )

                written_index_daily += index_sync.sync_and_persist_index_daily(
                    repo,
                    capset,
                    start_date=_dt.combine(index_start, _dt.min.time()),
                    end_date=_dt.combine(today, _dt.min.time()),
                    on_chunk_done=_index_chunk,
                )
                emit("sync_index", 88, f"指数日K完成,{written_index_daily} 行")
                _invalidate("index_instruments")
                _invalidate("index_daily")
                _invalidate("index_enriched")

            if pull_etf:
                emit("sync_index", 88, "同步 ETF 维表…")
                etf_count = index_sync.sync_etf_instruments(repo)
                emit("sync_index", 88, f"ETF 维表完成,{etf_count} 只")
                etf_symbols: list[str] = []
                etf_inst = repo.get_etf_instruments()
                if not etf_inst.is_empty() and "symbol" in etf_inst.columns:
                    etf_symbols = sorted(set(etf_inst["symbol"].to_list()))
                if etf_symbols and capset.has(Cap.ADJ_FACTOR):
                    try:
                        emit("sync_index", 88, "同步 ETF 除权因子…")
                        from datetime import datetime, timedelta

                        adj_end = datetime.now()
                        adj_path = repo.store.data_dir / "adj_factor_etf" / "all.parquet"
                        fallback_start = adj_end - timedelta(days=30)
                        adj_start = fallback_start
                        if adj_path.exists():
                            max_date = (
                                pl.scan_parquet(adj_path)
                                .select(pl.col("trade_date").max())
                                .collect()
                                .item()
                            )
                            if max_date is not None:
                                if isinstance(max_date, str):
                                    adj_start = datetime.combine(
                                        _date.fromisoformat(max_date), datetime.min.time()
                                    )
                                elif isinstance(max_date, datetime):
                                    adj_start = datetime.combine(
                                        max_date.date(), datetime.min.time()
                                    )
                                else:
                                    adj_start = datetime.combine(max_date, datetime.min.time())
                        _, affected_etfs = index_sync.sync_etf_adj_factor(
                            etf_symbols,
                            repo,
                            capset,
                            start_time=adj_start,
                            end_time=adj_end,
                        )
                        etf_adj_symbols = len(affected_etfs)
                        emit("sync_index", 88, f"ETF 除权因子完成,{etf_adj_symbols} 只")
                    except Exception as e:  # noqa: BLE001
                        logger.warning("ETF adj_factor skipped: %s", e)
                etf_dir = repo.store.data_dir / "kline_etf_enriched"
                etf_dates = (
                    sorted(
                        d.name[5:]
                        for d in etf_dir.glob("date=*")
                        if d.is_dir() and d.name.startswith("date=")
                    )
                    if etf_dir.exists()
                    else []
                )
                etf_start = (
                    _date.fromisoformat(etf_dates[-1]) if etf_dates else today - _td(days=365)
                )

                def _etf_chunk(cur: int, tot: int) -> None:
                    emit(
                        "sync_index",
                        88,
                        f"ETF 日K批次 {cur}/{tot}",
                        stage_pct=int(100 * cur / tot) if tot else 100,
                        skip_log=cur < tot,
                    )

                written_etf_daily = index_sync.sync_and_persist_etf_daily(
                    repo,
                    capset,
                    start_date=_dt.combine(etf_start, _dt.min.time()),
                    end_date=_dt.combine(today, _dt.min.time()),
                    on_chunk_done=_etf_chunk,
                )
                emit("sync_index", 88, f"ETF 日K完成,{written_etf_daily} 行")
                _invalidate("etf_instruments")
                _invalidate("etf_daily")

            if pull_hk:
                emit("sync_index", 88, "同步港股维表…")
                hk_count = index_sync.sync_hk_instruments(repo)
                # DEBUG: 如果港股维表返回 0，打印 provider 诊断信息
                if hk_count == 0:
                    try:
                        _dbg_provider = kline_sync._get_data_provider()
                        _dbg_df = _dbg_provider.get_instruments("hk")
                        logger.warning(
                            "HK instruments 0! provider.get_instruments('hk')=%d rows, fstore.available=%s",
                            _dbg_df.height,
                            _dbg_provider._fstore.available,
                        )
                    except Exception as _e:  # noqa: BLE001
                        logger.warning("HK instruments debug failed: %s", _e)
                emit("sync_index", 88, f"港股维表完成,{hk_count} 只")
                hk_dir = repo.store.data_dir / "kline_hk_enriched"
                hk_dates = (
                    sorted(
                        d.name[5:]
                        for d in hk_dir.glob("date=*")
                        if d.is_dir() and d.name.startswith("date=")
                    )
                    if hk_dir.exists()
                    else []
                )
                # 首次回补对齐 A 股 enriched 面板起点(2024-10-09),而非任意 365 天。
                hk_start = _date.fromisoformat(hk_dates[-1]) if hk_dates else _date(2024, 10, 9)

                def _hk_chunk(cur: int, tot: int) -> None:
                    emit(
                        "sync_index",
                        88,
                        f"港股日K批次 {cur}/{tot}",
                        stage_pct=int(100 * cur / tot) if tot else 100,
                        skip_log=cur < tot,
                    )

                written_hk_daily = index_sync.sync_and_persist_hk_daily(
                    repo,
                    capset,
                    start_date=_dt.combine(hk_start, _dt.min.time()),
                    end_date=_dt.combine(today, _dt.min.time()),
                    on_chunk_done=_hk_chunk,
                )
                emit("sync_index", 88, f"港股日K完成,{written_hk_daily} 行(不复权)")
                _invalidate("hk_daily")
                _invalidate("hk_enriched")
                _invalidate("hk_instruments")

            repo.refresh_index_views()
            emit(
                "sync_index",
                89,
                f"同步完成,指数 {index_count} 只/{written_index_daily} 行, ETF {etf_count} 只/{written_etf_daily} 行"
                + (f", ETF复权 {etf_adj_symbols} 只" if etf_adj_symbols else "")
                + (f", 港股 {hk_count} 只/{written_hk_daily} 行" if pull_hk else ""),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("sync_index/etf/hk failed: %s", e)
            emit("sync_index", 89, f"指数/ETF/港股同步失败:{e}")
    else:
        skipped.append("sync_index")

    # Step 2.5: 分钟 K 同步(可选) — 未启用或无 capability 时静默跳过(不 emit)
    from app.services import preferences

    minute_on = preferences.get_minute_sync_enabled()
    minute_days = preferences.get_minute_sync_days()
    written_minute = 0
    if minute_on and capset.has(Cap.KLINE_MINUTE_BATCH):
        minute_start = today - _td(days=minute_days)
        emit("sync_minute", 90, f"获取分钟K [{minute_start} ~ {today}]…")
        logger.info("sync_minute: [%s ~ %s] start", minute_start, today)
        minute_symbols = _resolve_minute_symbols(capset)

        def _minute_chunk_progress(cur: int, tot: int) -> None:
            emit(
                "sync_minute",
                90 + int(3 * cur / tot),
                f"分钟K 批次 {cur}/{tot}",
                stage_pct=int(100 * cur / tot),
                skip_log=True,
            )

        try:
            written_minute = kline_sync.sync_and_persist_minute(
                minute_symbols,
                repo,
                capset,
                days=minute_days,
                on_chunk_done=_minute_chunk_progress,
            )
            minute_dir = repo.store.data_dir / "kline_minute"
            minute_cover_days = len(list(minute_dir.glob("date=*"))) if minute_dir.exists() else 0
            emit("sync_minute", 93, f"分钟K完成,覆盖 {minute_cover_days} 天")
            logger.info(
                "sync_minute: [%s ~ %s] done, %d days", minute_start, today, minute_cover_days
            )
            _invalidate("minute")
        except CatalogError as exc:
            failed_stages.append({"stage": "sync_minute", "error": str(exc)})
            emit("sync_minute", 93, f"分钟K降级:{exc}")
            logger.error("sync_minute degraded: %s", exc)
    else:
        skipped.append("sync_minute")
        if minute_on:
            logger.info("sync_minute skipped: no KLINE_MINUTE_BATCH capability")
        else:
            logger.info("sync_minute skipped: user disabled")

    # Step 3.5: 刷新策略结果缓存（让 /api/screener/cached 跟上 enriched 最新日，
    # 不再依赖用户手动点 run_all）
    _refresh_strategy_cache(repo, emit)

    # Step 4: 刷新视图
    emit("refresh_views", 95, "刷新 DuckDB 视图…")
    _refresh_views(repo)

    emit("done", 100, "完成")
    _invalidate(None)  # 兜底:全清

    return {
        "universe_size": len(universe),
        "daily_days": new_daily_days,
        "adj_factor_symbols": len(affected_symbols),
        "enriched_rows": written_enriched,
        "index_count": index_count,
        "index_daily_rows": written_index_daily,
        "etf_count": etf_count,
        "etf_daily_rows": written_etf_daily,
        "etf_adj_factor_symbols": etf_adj_symbols,
        "hk_count": hk_count,
        "hk_daily_rows": written_hk_daily,
        "minute_rows": written_minute,
        "skipped_stages": skipped,
        "failed_stages": failed_stages,
    }


def _refresh_strategy_cache(repo: KlineRepository, emit: ProgressCb) -> None:
    """盘后重算所有策略命中并写入 strategy_cache.json。

    复用 app/api/screener.run_all 的核心逻辑（一次读 enriched + engine.run_all），
    让 /api/screener/cached 跟上 enriched 最新日，不再依赖用户手动点 run_all。
    失败不阻断管道（best-effort）。
    """
    from dataclasses import asdict

    emit("refresh_strategy_cache", 94, "刷新策略结果缓存…")
    try:
        from app.services.screener import ScreenerService
        from app.services import strategy_cache
        from app.strategy import config as strategy_config
        from app.strategy.engine import StrategyEngine

        svc = ScreenerService(repo)
        as_of = svc.latest_date()
        if not as_of:
            logger.info("refresh_strategy_cache: no enriched date, skip")
            emit("refresh_strategy_cache", 94, "策略缓存跳过（无 enriched 数据）")
            return

        data_dir = repo.store.data_dir
        strategy_dirs = [
            Path(__file__).resolve().parent.parent / "strategy" / "builtin",
            data_dir / "strategies" / "custom",
            data_dir / "strategies" / "ai",
        ]
        engine = StrategyEngine(
            enriched_loader=svc._load_enriched_for_date,
            enriched_history_loader=svc._load_enriched_history,
            strategy_dirs=strategy_dirs,
        )
        # F16: 方案注册为 screen:<hex> 策略 (本进程不 reload, 构造后同步一次)。
        from app.strategy.screen_bridge import sync_screen_strategies

        sync_screen_strategies(engine, data_dir)
        all_overrides = strategy_config.list_overrides(data_dir)
        results: dict[str, dict] = {}
        for sid, r in engine.run_all(as_of, overrides_map=all_overrides).items():
            results[sid] = {
                "total": r.total,
                "as_of": str(as_of),
                "rows": asdict(r).get("rows", []),
            }
        if results:
            strategy_cache.write_cache(data_dir, str(as_of), results)
        _invalidate("screener")
        emit("refresh_strategy_cache", 94, f"策略缓存刷新完成,{len(results)} 策略")
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh_strategy_cache failed: %s", e)
        emit("refresh_strategy_cache", 94, f"策略缓存刷新失败:{e}")


def _refresh_views(repo: KlineRepository) -> None:
    """刷新所有 DuckDB 视图。"""
    d = repo.store.data_dir.as_posix()
    views = {
        "kline_daily": f"{d}/kline_daily/**/*.parquet",
        "kline_enriched": f"{d}/kline_daily_enriched/**/*.parquet",
        "kline_index_daily": f"{d}/kline_index_daily/**/*.parquet",
        "kline_index_enriched": f"{d}/kline_index_enriched/**/*.parquet",
        "kline_etf_daily": f"{d}/kline_etf_daily/**/*.parquet",
        "kline_etf_enriched": f"{d}/kline_etf_enriched/**/*.parquet",
        "kline_etf_minute": f"{d}/kline_etf_minute/**/*.parquet",
        "kline_minute": f"{d}/kline_minute/**/*.parquet",
        "adj_factor": f"{d}/adj_factor/**/*.parquet",
        "adj_factor_etf": f"{d}/adj_factor_etf/**/*.parquet",
        "instruments": f"{d}/instruments/**/*.parquet",
        "instruments_index": f"{d}/instruments_index/**/*.parquet",
        "instruments_etf": f"{d}/instruments_etf/**/*.parquet",
    }
    for name, path in views.items():
        try:
            repo.db.execute(
                f"CREATE OR REPLACE VIEW {name} AS "
                f"SELECT * FROM read_parquet('{path}', union_by_name=true)"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("refresh view %s failed: %s", name, e)
    repo.store._register_unified_views()


def _refresh_single_view(repo: KlineRepository, name: str) -> None:
    """刷新单个 DuckDB 视图。"""
    d = repo.store.data_dir.as_posix()
    paths = {
        "kline_daily": f"{d}/kline_daily/**/*.parquet",
        "kline_enriched": f"{d}/kline_daily_enriched/**/*.parquet",
        "kline_index_daily": f"{d}/kline_index_daily/**/*.parquet",
        "kline_index_enriched": f"{d}/kline_index_enriched/**/*.parquet",
        "kline_etf_daily": f"{d}/kline_etf_daily/**/*.parquet",
        "kline_etf_enriched": f"{d}/kline_etf_enriched/**/*.parquet",
        "kline_etf_minute": f"{d}/kline_etf_minute/**/*.parquet",
        "kline_minute": f"{d}/kline_minute/**/*.parquet",
        "adj_factor": f"{d}/adj_factor/**/*.parquet",
        "adj_factor_etf": f"{d}/adj_factor_etf/**/*.parquet",
        "instruments": f"{d}/instruments/**/*.parquet",
        "instruments_index": f"{d}/instruments_index/**/*.parquet",
        "instruments_etf": f"{d}/instruments_etf/**/*.parquet",
    }
    path = paths.get(name)
    if not path:
        return
    try:
        repo.db.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_parquet('{path}', union_by_name=true)"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh view %s failed: %s", name, e)


def _resolve_minute_symbols(capset: CapabilitySet) -> list[str]:
    """分钟 K 同步标的 — 与日K共用同一标的池。"""
    return _resolve_universe(capset)


def _latest_enriched_date(repo: KlineRepository):
    """本地模式不写 stock raw，用 enriched 分区作为增量起点。"""
    dates = _enriched_partition_dates(repo)
    return max(dates) if dates else None


def _enriched_partition_dates(repo: KlineRepository) -> list[date_type]:
    """List valid enriched partition dates."""
    from datetime import date as _date

    base = repo.store.data_dir / "kline_daily_enriched"
    if not base.exists():
        return []
    dates = []
    for path in base.glob("date=*"):
        try:
            dates.append(_date.fromisoformat(path.name[5:]))
        except ValueError:
            continue
    return sorted(dates)


def _previous_enriched_date(repo: KlineRepository, target: date_type) -> date_type | None:
    dates = [d for d in _enriched_partition_dates(repo) if d < target]
    return dates[-1] if dates else None


def _remove_enriched_partition(repo: KlineRepository, target: date_type) -> None:
    path = repo.store.data_dir / "kline_daily_enriched" / f"date={target.isoformat()}"
    if path.exists():
        shutil.rmtree(path)
        logger.warning("removed incomplete local enriched partition: %s", path)


def _provider_freshness_date() -> date_type | None:
    try:
        from app.data_providers import get_provider
        from app.data_providers.registry import get_active_provider_name

        provider = get_provider(get_active_provider_name("daily"))
        provider_freshness = getattr(provider, "get_daily_freshness", None)
        if callable(provider_freshness):
            return provider_freshness()
        client = getattr(provider, "_engine", None)
        freshness = getattr(client, "freshness", None)
        return freshness() if callable(freshness) else None
    except Exception as e:  # noqa: BLE001
        logger.debug("local freshness check failed: %s", e)
        return None


def _latest_verified_enriched_date(
    repo: KlineRepository,
    ceiling: date_type | None = None,
) -> date_type | None:
    """返回最近一个通过完整性校验的 enriched 分区日期（≤ ceiling）。

    只读目标与前一分区的 symbol 列（各一次 parquet 列裁剪读取），不做全量扫描。
    用于 ``initialize_local_enriched_ceiling`` 发布 canonical 水位前回退到安全分区。
    """
    dates = [d for d in _enriched_partition_dates(repo) if ceiling is None or d <= ceiling]
    if not dates:
        return None
    latest = dates[-1]
    previous = dates[-2] if len(dates) >= 2 else None
    if _local_partition_coverage_ok(repo, previous, latest):
        return latest
    # 最近分区不完整 → 退回前一个（前一个在上一轮管道已验证完整）
    return previous


def initialize_local_enriched_ceiling(repo: KlineRepository) -> date_type | None:
    """在首次缓存预热前发布已验证的安全 enriched 水位。

    **不得**直接把 provider freshness 发布成 canonical —— freshness 只表示 provider
    有该日期数据，不代表本地 enriched 分区已写完。仅当目标 enriched 分区通过既有
    ``_local_partition_coverage_ok`` 完整性校验才发布该日期；否则退回到最近已验证
    完整分区。``pipeline_pull_a_share`` 关闭时也不越过校验发布未验证日期。
    """
    if not is_local_daily_mode():
        return None
    fresh = _provider_freshness_date()
    if fresh is None:
        return None
    publish = getattr(repo, "set_enriched_canonical_date", None)
    verified = _latest_verified_enriched_date(repo, ceiling=fresh)
    if verified is not None and callable(publish):
        publish(verified)
    return verified


def _local_daily_coverage_ok(
    target: date_type,
    sample_symbols: tuple[str, ...] = _BOOTSTRAP_SAMPLE_SYMBOLS,
) -> bool:
    """Check a new local daily date is probably complete before publishing it."""
    try:
        from datetime import datetime as _dt
        from app.data_providers import get_provider
        from app.data_providers.registry import get_active_provider_name

        provider = get_provider(get_active_provider_name("daily"))
        start = _dt.combine(target, _dt.min.time())
        end = _dt.combine(target, _dt.min.time())
        df = provider.get_daily(list(sample_symbols), start, end, "stock")
        if df.is_empty() or "symbol" not in df.columns:
            return False
        if "date" in df.columns:
            df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
            df = df.filter(pl.col("date") == target)
        covered = df["symbol"].cast(pl.Utf8).unique().len()
        coverage = covered / max(1, len(sample_symbols))
        if coverage < _BOOTSTRAP_MIN_SAMPLE_COVERAGE:
            logger.warning(
                "local daily completeness low for %s: %.0f%% (%d/%d)",
                target,
                coverage * 100,
                covered,
                len(sample_symbols),
            )
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("local daily completeness check failed for %s: %s", target, e)
        return False


def _partition_symbol_count(repo: KlineRepository, target: date_type | None) -> int:
    if target is None:
        return 0
    path = (
        repo.store.data_dir / "kline_daily_enriched" / f"date={target.isoformat()}" / "part.parquet"
    )
    if not path.exists():
        return 0
    try:
        df = pl.read_parquet(path, columns=["symbol"])
    except Exception as e:  # noqa: BLE001
        logger.warning("read enriched partition failed %s: %s", path, e)
        return 0
    return df["symbol"].cast(pl.Utf8).unique().len() if "symbol" in df.columns else 0


def _local_partition_coverage_ok(
    repo: KlineRepository,
    previous: date_type | None,
    target: date_type,
) -> bool:
    """Verify the published target partition is not obviously partial."""
    target_count = _partition_symbol_count(repo, target)
    previous_count = _partition_symbol_count(repo, previous)
    if previous_count <= 0:
        return target_count > 0
    coverage = target_count / previous_count
    if coverage < _BOOTSTRAP_MIN_PARTITION_COVERAGE:
        logger.warning(
            "local enriched partition coverage low for %s: %.0f%% (%d/%d)",
            target,
            coverage * 100,
            target_count,
            previous_count,
        )
        return False
    return True


def bootstrap_local_enriched_if_stale(repo: KlineRepository, capset: CapabilitySet) -> dict:
    """Catch up local enriched at startup when TDX disk is newer than parquet."""
    if not is_local_daily_mode() or not _prefs.get_pipeline_pull_a_share():
        return {"started": False, "reason": "not_local_or_disabled"}

    partition_dates = _enriched_partition_dates(repo)
    latest_on_disk = max(partition_dates) if partition_dates else None
    fresh = _provider_freshness_date()
    if fresh is None:
        return {"started": False, "reason": "no_freshness"}

    # 不在此处提前发布 fresh canonical —— 分区可能不完整。
    # initialize_local_enriched_ceiling 已在启动时发布安全水位；
    # 仅在下方分区通过完整性校验后才发布 fresh。
    publish_canonical = getattr(repo, "set_enriched_canonical_date", None)

    future_dates = [value for value in partition_dates if value > fresh]
    if future_dates:
        logger.warning(
            "忽略 provider 未确认的 enriched 分区: canonical=%s latest_on_disk=%s",
            fresh,
            latest_on_disk,
        )
    canonical_dates = [value for value in partition_dates if value <= fresh]
    latest_enriched = max(canonical_dates) if canonical_dates else None

    if latest_enriched is not None and fresh == latest_enriched:
        previous = _previous_enriched_date(repo, fresh)
        if not _local_partition_coverage_ok(repo, previous, fresh):
            _remove_enriched_partition(repo, fresh)
            latest_enriched = previous
            repo.refresh_cache()
        else:
            if callable(publish_canonical):
                publish_canonical(fresh)
            return {
                "started": False,
                "reason": "up_to_date",
                "freshness": str(fresh),
                "enriched": str(latest_enriched),
            }
    if not _local_daily_coverage_ok(fresh):
        return {
            "started": False,
            "reason": "incomplete",
            "freshness": str(fresh),
            "enriched": str(latest_enriched) if latest_enriched else None,
        }

    logger.info("local enriched bootstrap: freshness=%s enriched=%s", fresh, latest_enriched)
    from datetime import datetime as _dt, timedelta as _td
    from app.data_providers import get_provider
    from app.data_providers.registry import get_active_provider_name

    provider = get_provider(get_active_provider_name("daily"))
    start_date = (latest_enriched + _td(days=1)) if latest_enriched else (fresh - _td(days=365))
    universe = _resolve_universe(capset)
    written = run_pipeline_local_incremental(
        provider,
        data_dir=repo.store.data_dir,
        symbols=universe,
        start_time=_dt.combine(start_date, _dt.min.time()),
        end_time=_dt.combine(fresh, _dt.min.time()),
    )
    if not _local_partition_coverage_ok(repo, latest_enriched, fresh):
        _remove_enriched_partition(repo, fresh)
        repo.refresh_cache()
        return {
            "started": False,
            "reason": "partition_incomplete",
            "freshness": str(fresh),
            "enriched": str(latest_enriched) if latest_enriched else None,
            "written": written,
        }
    if callable(publish_canonical):
        publish_canonical(fresh)
    repo.refresh_cache()
    return {
        "started": True,
        "freshness": str(fresh),
        "enriched": str(latest_enriched) if latest_enriched else None,
        "written": written,
    }


def start_local_enriched_bootstrap(repo: KlineRepository, capset: CapabilitySet) -> None:
    """Run local enriched catch-up in background so startup remains responsive.

    补算范围:
    1. A 股 enriched (核心, 覆盖选股/看板/复盘)
    2. 指数/ETF/港股日 K 本地 parquet (接口直查走 provider 不受影响, 但
       本地 enriched parquet 落后会卡住 market_overview / watchlist 等聚合)
    """
    if not is_local_daily_mode():
        return

    def _run() -> None:
        try:
            result = bootstrap_local_enriched_if_stale(repo, capset)
            logger.info("local enriched bootstrap: %s", result)

            # A 股 enriched 补完后, 检查指数/ETF/港股是否也落后于 freshness
            _bootstrap_asset_daily_if_stale(repo, capset)
        except Exception:  # noqa: BLE001
            logger.exception("local enriched bootstrap failed")

    threading.Thread(target=_run, name="local-enriched-bootstrap", daemon=True).start()


def _latest_asset_partition_date(repo: KlineRepository, table: str) -> date_type | None:
    """读某资产 enriched parquet 分区的最新日期。"""
    from datetime import date as _date

    base = repo.store.data_dir / table
    if not base.exists():
        return None
    dates = []
    for p in base.glob("date=*"):
        if not p.is_dir():
            continue
        try:
            dates.append(_date.fromisoformat(p.name[5:]))
        except ValueError:
            continue
    return max(dates) if dates else None


def _bootstrap_asset_daily_if_stale(repo: KlineRepository, capset: CapabilitySet) -> None:
    """启动时补算指数/ETF/港股日 K 本地 parquet，使其追上 provider freshness。

    与 A 股 enriched bootstrap 同理：只在上游快照比本地新时才补。
    用增量方式只拉缺口日期，不重复已有数据。
    """
    fresh = _provider_freshness_date()
    if fresh is None:
        return
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return
    from datetime import datetime as _dt, timedelta as _td

    # 先刷新 fstore 连接，确保读到最新 generation 的 instruments/markets 快照
    try:
        provider = kline_sync._get_data_provider()
        refresh = getattr(provider, "refresh_fstore_clients", None)
        if callable(refresh):
            refresh()
    except Exception:  # noqa: BLE001
        logger.debug("fstore refresh in asset bootstrap failed", exc_info=True)

    assets = [
        ("index", "kline_index_enriched", "sync_index_instruments", "sync_and_persist_index_daily"),
        ("etf", "kline_etf_enriched", "sync_etf_instruments", "sync_and_persist_etf_daily"),
        ("hk", "kline_hk_enriched", "sync_hk_instruments", "sync_and_persist_hk_daily"),
    ]
    for label, table, inst_fn_name, daily_fn_name in assets:
        if (
            not _prefs.get_pipeline_pull_hk()
            if label == "hk"
            else (
                not _prefs.get_pipeline_pull_index()
                if label == "index"
                else not _prefs.get_pipeline_pull_etf()
            )
        ):
            continue
        try:
            latest = _latest_asset_partition_date(repo, table)
            if latest is not None and latest >= fresh:
                logger.info("asset bootstrap %s: up_to_date (%s >= %s)", label, latest, fresh)
                continue
            start_date = (latest + _td(days=1)) if latest else (fresh - _td(days=365))

            # 先确保 instruments 在
            inst_fn = getattr(index_sync, inst_fn_name)
            inst_fn(repo)

            daily_fn = getattr(index_sync, daily_fn_name)
            written = daily_fn(
                repo,
                capset,
                start_date=_dt.combine(start_date, _dt.min.time()),
                end_date=_dt.combine(fresh, _dt.min.time()),
            )
            repo.refresh_index_views()
            logger.info(
                "asset bootstrap %s: synced %d rows [%s ~ %s]", label, written, start_date, fresh
            )
        except Exception:  # noqa: BLE001
            logger.warning("asset bootstrap %s failed", label, exc_info=True)


def _refresh_instruments_view(repo: KlineRepository) -> None:
    """单独刷新 instruments 视图。"""
    d = repo.store.data_dir.as_posix()
    try:
        repo.db.execute(
            f"CREATE OR REPLACE VIEW instruments AS "
            f"SELECT * FROM read_parquet('{d}/instruments/**/*.parquet', union_by_name=true)"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh instruments view failed: %s", e)


def _run_tracked(fn, job_label: str) -> None:
    """调度触发时包装 JobStore 跟踪，确保同步历史有记录。"""
    from app.services.pipeline_jobs import JobCancelledError, job_store

    job_store.reap_stale()

    job_id, is_new = job_store.create(kind=job_label)
    if not is_new:
        # 已有任务在跑: 调度不并发开第二个,也不标失败
        logger.info("scheduled %s skipped: existing active job", job_label)
        return

    owner = job_store.start(job_id)
    if owner is None:
        return

    def progress(
        stage: str, pct: int, msg: str, stage_pct: int | None = None, skip_log: bool = False
    ) -> None:
        job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

    try:
        result = fn(on_progress=progress)
        job_store.succeed(job_id, result, owner=owner)
        logger.info("scheduled %s completed: job_id=%s", job_label, job_id)
    except JobCancelledError:
        logger.info("scheduled %s cancelled: job_id=%s", job_label, job_id)
    except Exception:
        logger.exception("scheduled %s failed: job_id=%s", job_label, job_id)
        job_store.fail(job_id, f"scheduled {job_label} failed", owner=owner)
    finally:
        job_store.release(job_id, owner)
        from app.api.data import invalidate_job_status_cache

        invalidate_job_status_cache()


# ================================================================
# 定时复盘 (AI 大盘复盘报告)
# ================================================================

REVIEW_JOB_ID = "scheduled_review"


async def _run_scheduled_review(repo) -> None:
    """定时复盘 job: 流式生成复盘 → 实时推 SSE(开着页面可见) → 落盘归档 → 推飞书。

    与手动「生成复盘」体验一致: 流式事件经 quote_service.push_review_event →
    /api/intraday/stream 的 review_progress 事件 → 前端 reviewStore, 用户开着复盘页
    即可看到报告边生成边显示, 切走再回来也能看到生成中/已生成。
    LLM 偶发断流(peer closed connection)时自动重试最多 2 次。
    任何异常都吞掉只记日志, 绝不影响调度器主循环。
    """
    import json

    try:
        from app.services import market_recap_reports
        from app import secrets_store as ss

        # AI Key 未配置时跳过(避免每日报错刷日志)
        if not ss.get_ai_key():
            logger.info("scheduled review skipped: AI key not configured")
            return

        app_state = _get_app_state()
        quote_service = getattr(app_state, "quote_service", None) if app_state else None
        depth_service = getattr(app_state, "depth_service", None) if app_state else None

        content, meta = await _stream_review_with_retry(repo, quote_service, depth_service)
        if not content:
            logger.warning("scheduled review produced no content (meta=%s)", meta)
            # 通知前端进入 error 态(若有页面在听)
            if quote_service:
                quote_service.push_review_event(
                    json.dumps(
                        {"type": "error", "message": "复盘生成失败,请稍后手动重试"},
                        ensure_ascii=False,
                    )
                )
            return

        # 落盘: 与手动生成完全相同的归档格式
        market_recap_reports.save_report(
            {
                "as_of": meta.get("as_of"),
                "focus": "",
                "content": content,
                "summary": meta.get("summary", ""),
                "emotion_score": meta.get("emotion_score"),
                "emotion_label": meta.get("emotion_label", ""),
            }
        )
        logger.info("scheduled review saved: as_of=%s", meta.get("as_of"))

        # 通知前端: 生成完成且已归档(archived=true 让前端只刷新列表, 不重复归档)
        if quote_service:
            quote_service.push_review_event(
                json.dumps({"type": "done", "archived": True}, ensure_ascii=False)
            )

        # 推送到飞书(可选): 运行时读取配置, 用户改设置下次触发即生效。
        # 失败静默降级, 不影响已归档的报告。
        _maybe_push_review(content, meta)
    except Exception as e:  # noqa: BLE001
        logger.exception("scheduled review failed: %s", e)
        # 兜底: 异常时通知前端停止「生成中」状态, 避免页面卡在 streaming
        try:
            app_state = _get_app_state()
            qs = getattr(app_state, "quote_service", None) if app_state else None
            if qs:
                import json as _json

                qs.push_review_event(
                    _json.dumps(
                        {"type": "error", "message": "复盘生成异常,请稍后手动重试"},
                        ensure_ascii=False,
                    )
                )
        except Exception:  # noqa: BLE001
            pass


async def _stream_review_with_retry(repo, quote_service, depth_service) -> tuple[str, dict]:
    """流式生成复盘, 每个事件推 SSE + 累积内容。LLM 断流时最多重试 2 次。

    返回 (content, meta)。重试时推一个 retry 事件让前端清空已累积内容重新开始。
    成功(收到 done/无 error)或耗尽重试后返回。
    """
    import asyncio
    import json
    from app.services.market_recap import recap_market_stream

    max_attempts = 3  # 初次 + 2 次重试
    last_meta: dict = {}
    content_parts: list[str] = []

    for attempt in range(1, max_attempts + 1):
        content_parts = []  # 每次重试重新累积
        failed = False
        try:
            async for evt_json in recap_market_stream(repo, quote_service, depth_service):
                evt = json.loads(evt_json)
                t = evt.get("type")

                # 推给前端(让开着页面的用户实时看到, 与手动一致)
                if quote_service:
                    quote_service.push_review_event(evt_json)

                if t == "meta":
                    last_meta = evt
                elif t == "delta" and evt.get("content"):
                    content_parts.append(evt["content"])
                elif t == "error":
                    failed = True
                    logger.warning(
                        "scheduled review stream error (attempt %d/%d): %s",
                        attempt,
                        max_attempts,
                        evt.get("message"),
                    )
                    break  # 触发重试
                elif t == "done":
                    # 正常完成
                    return "".join(content_parts), last_meta
            # 流自然结束(无 done 事件)且有内容, 视为成功
            if content_parts and not failed:
                return "".join(content_parts), last_meta
        except Exception as e:  # noqa: BLE001
            # LLM 断流等异常(httpx.RemoteProtocolError)落到这里
            failed = True
            logger.warning(
                "scheduled review stream exception (attempt %d/%d): %s", attempt, max_attempts, e
            )

        # 失败: 决定是否重试
        if attempt < max_attempts:
            logger.info("scheduled review retrying in 3s (attempt %d → %d)", attempt, attempt + 1)
            # 通知前端: 即将重试, 清空已累积内容重新开始
            if quote_service:
                quote_service.push_review_event(
                    json.dumps({"type": "retry", "attempt": attempt + 1}, ensure_ascii=False)
                )
            await asyncio.sleep(3)

    # 耗尽重试, 返回已累积内容(可能为空)和最后 meta
    return "".join(content_parts), last_meta


def _maybe_push_review(content: str, meta: dict) -> None:
    """复盘报告归档后, 按 review_push_channels 选定的外部工具逐个推送完整报告。

    定时生成与手动生成共用本函数 (手动归档端点 POST /api/market-recap/reports 也会调用)。
    channels 为空则不推送; 'feishu' 复用监控中心的全局飞书 Webhook 通道。
    推送失败静默降级 (Webhook 是辅助通道), 不影响已归档的报告。
    """
    try:
        from app.services import preferences, webhook_adapter

        channels = preferences.get_review_push_channels()
        if not channels:
            return

        emotion = f"{meta.get('emotion_label') or ''}".strip()
        as_of = meta.get("as_of") or ""
        subtitle = as_of + (f" · 情绪 {emotion}" if emotion else "")

        configs = preferences.get_configured_webhook_channels()
        for ch in channels:
            cfg = configs.get(ch)
            if not cfg:
                logger.info("review push(%s) skipped: webhook not configured", ch)
                continue
            if ch == "feishu":
                ok = webhook_adapter.send_feishu_card(
                    cfg.get("url", ""),
                    "TickFlow · 每日复盘",
                    subtitle,
                    content,
                    cfg.get("secret", ""),
                )
            else:
                ok = webhook_adapter.send_channel(
                    ch, cfg, "TickFlow · 每日复盘", f"{subtitle}\n\n{content}".strip()
                )
            logger.info("review push(%s) %s", ch, "sent" if ok else "failed")
    except Exception as e:  # noqa: BLE001
        logger.warning("review push error: %s", e)


def _register_review_job(scheduler, repo, hour: int, minute: int) -> None:
    """注册/更新定时复盘 job(工作日 mon-fri, Asia/Shanghai)。

    供 start_scheduler(启动时) 和 settings API(改时间时) 共用。
    用 replace_existing=True, 重复注册只更新 trigger。

    注意: _run_scheduled_review 是协程函数, 必须把函数对象本身(配合 args)传给
    add_job, 而非用 lambda 包裹 —— 否则 APScheduler 会把 lambda 当同步函数在线程池
    执行, 仅得到一个未 await 的协程对象, 复盘实际不会运行。
    """
    scheduler.add_job(
        _run_scheduled_review,
        args=[repo],
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=hour, minute=minute, timezone="Asia/Shanghai"
        ),
        id=REVIEW_JOB_ID,
        misfire_grace_time=7200,  # 复盘非关键, 允许 2 小时内补跑
        replace_existing=True,
    )


def start_scheduler(repo: KlineRepository, capset: CapabilitySet) -> AsyncIOScheduler:
    """启动调度器。

    工作日 09:10 — 同步个股维表
    工作日 HH:MM — 盘后管道（时间由用户偏好决定，默认 15:30）
    """
    from app.services import preferences

    sched = preferences.get_pipeline_schedule()
    inst_sched = preferences.get_instruments_schedule()

    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    # 盘前: 同步 instruments（时间由偏好决定）
    def _instruments_task(on_progress=None):
        emit = on_progress or _noop
        emit("sync_instruments", 0, "同步个股维表…")
        result = run_instruments_sync(repo)
        emit("done", 100, f"个股维表同步完成,{result.get('instruments_rows', 0)} 只标的")
        return result

    scheduler.add_job(
        lambda: _run_tracked(_instruments_task, "instruments_sync"),
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=inst_sched["hour"],
            minute=inst_sched["minute"],
            timezone="Asia/Shanghai",
        ),
        id="pre_market_instruments",
        misfire_grace_time=1800,
        replace_existing=True,
    )

    # 盘后: 日 K + enriched（时间由偏好决定）
    def _pipeline_then_refresh(on_progress=None):
        # 与手动触发 (/api/pipeline/run) 对齐: 管道落盘后重建 Polars 内存缓存,
        # 否则 live_agg 的昨日连板数等基准列会停留在旧交易日, 次日开盘连板梯队
        # 整体少算一档 (仅手动触发或重启才会刷缓存, cron 调度路径此前漏了这步)。
        result = run_now(repo, capset, on_progress=on_progress)
        repo.refresh_cache()
        return result

    scheduler.add_job(
        lambda: _run_tracked(_pipeline_then_refresh, "daily_pipeline"),
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=sched["hour"],
            minute=sched["minute"],
            timezone="Asia/Shanghai",
        ),
        id="daily_pipeline",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 盘后: 五档盘口 sealed 定版(时间由偏好决定, 默认15:02, 范围15:01~18:00)
    depth_sched = preferences.get_depth_finalize_time()

    def _depth_finalize():
        depth_svc = getattr(_get_app_state(), "depth_service", None) if _get_app_state() else None
        if depth_svc:
            depth_svc.finalize()

    scheduler.add_job(
        _depth_finalize,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=depth_sched["hour"],
            minute=depth_sched["minute"],
            timezone="Asia/Shanghai",
        ),
        id="depth_finalize",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 定时复盘 (AI 大盘复盘报告): 工作日到点自动生成并归档。
    # 默认关闭 —— 仅当用户在复盘页开启时才注册 job。
    # 复用 recap_market_once(非流式) + market_recap_reports.save_report(落盘)。
    # quote_service / depth_service 通过 _get_app_state() 延迟取用。
    review_sched = preferences.get_review_schedule()
    if review_sched["enabled"]:
        _register_review_job(scheduler, repo, review_sched["hour"], review_sched["minute"])
        logger.info(
            "scheduled_review enabled @%02d:%02d mon-fri",
            review_sched["hour"],
            review_sched["minute"],
        )

    # 交易自动复盘 (P6.4): 工作日 16:45 盘后状态驱动 AI 归因。
    # job 固定注册, 运行时读 tradingAutoReview 开关 (默认 false=零开销直接返回)。
    _register_trading_auto_review_job(scheduler, repo)

    # F11 回测定时复跑: 工作日到点用滚动窗口复跑收藏的策略 Run (默认关闭,
    # 仅当用户开启时注册 job; job 运行时还会再读开关, 关=零开销直接返回)。
    rerun_pref = get_backtest_auto_rerun()
    if rerun_pref["enabled"]:
        _register_backtest_favorite_rerun_job(
            scheduler, repo, rerun_pref["hour"], rerun_pref["minute"]
        )
        logger.info(
            "backtest_favorite_rerun enabled @%02d:%02d mon-fri",
            rerun_pref["hour"],
            rerun_pref["minute"],
        )

    scheduler.start()
    logger.info(
        "scheduler started; instruments@%02d:%02d, pipeline@%02d:%02d, depth@%02d:%02d mon-fri",
        inst_sched["hour"],
        inst_sched["minute"],
        sched["hour"],
        sched["minute"],
        depth_sched["hour"],
        depth_sched["minute"],
    )
    return scheduler


TRADING_AUTO_REVIEW_JOB_ID = "trading_auto_review"


async def _run_trading_auto_review(repo) -> None:
    """盘后交易自动复盘 job (P6.4 L0/L1/L2 状态驱动 AI 归因)。

    每日 16:45 触发 (盘后, 晚于 pipeline 15:30)。读 settings 的 tradingAutoReview 开关:
    false(默认) → 直接返回, 零开销; true → 跑 run_state_driven_autopsy 并 logger.info 结果。
    """
    from app.services import preferences

    if not preferences.get_trading_auto_review():
        return  # 开关关闭, 零开销
    from app.services.trading.review_job import run_state_driven_autopsy

    result = await run_state_driven_autopsy(repo.store.data_dir)
    logger.info("trading_auto_review: %s", result)


def _register_trading_auto_review_job(scheduler, repo) -> None:
    """注册/更新交易自动复盘 job (工作日 16:45, Asia/Shanghai)。

    供 start_scheduler(启动时) 和 settings API(开关变更时) 共用。
    job 固定注册, 开关在运行时判断 (false=直接返回), 避免改开关还要动态增删 job。
    """
    scheduler.add_job(
        _run_trading_auto_review,
        args=[repo],
        trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=45, timezone="Asia/Shanghai"),
        id=TRADING_AUTO_REVIEW_JOB_ID,
        misfire_grace_time=7200,
        replace_existing=True,
    )


def _register_backtest_favorite_rerun_job(scheduler, repo, hour: int, minute: int) -> None:
    """注册/更新回测定时复跑 job (F11, 工作日 mon-fri, Asia/Shanghai)。

    供 start_scheduler(启动时) 和 settings API(改偏好时) 共用,
    replace_existing=True, 重复注册只更新 trigger。
    任务体为同步函数 — AsyncIOScheduler 把同步 job 放线程池执行,
    回测计算不阻塞事件循环; 运行时还会再读一次偏好开关 (关=零开销)。
    """
    from app.jobs.backtest_favorite_rerun import run_backtest_favorite_rerun_job

    def _rerun_task(on_progress=None):
        return run_backtest_favorite_rerun_job(repo, on_progress=on_progress)

    scheduler.add_job(
        lambda: _run_tracked(_rerun_task, BACKTEST_RERUN_JOB_ID),
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=hour, minute=minute, timezone="Asia/Shanghai"
        ),
        id=BACKTEST_RERUN_JOB_ID,
        misfire_grace_time=7200,  # 盘后复跑非关键, 允许 2 小时内补跑
        replace_existing=True,
    )


# app_state 延迟引用(start_scheduler 在 lifespan 早期调用, app.state 可能还没就绪)
_app_state_ref = None


def set_app_state(app_state) -> None:
    """lifespan 注册 app.state 引用, 供 scheduled job 访问 depth_service 等单例。"""
    global _app_state_ref
    _app_state_ref = app_state


def _get_app_state():
    return _app_state_ref
