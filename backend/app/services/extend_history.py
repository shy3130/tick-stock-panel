"""向前扩展历史数据 — 完全独立于 daily_pipeline 的盘后管道。

用户从日 K 卡片手动触发,指定往前补的时长 (x 天/月/年)。
流程:
  1. 获取当前最早日期
  2. 向前拉日 K batch (start = 最早日期 - offset, end = 最早日期)
  3. 向前拉除权因子 (同范围)
  4. 全量重算 enriched
  5. 刷新视图 + 缓存

⚠️ 本模块不导入 daily_pipeline 的任何函数,只复用基础设施:
  - kline_sync.sync_and_persist_daily_batch / sync_adj_factor
  - indicators.pipeline.run_pipeline
  - pipeline_jobs.JobStore
  - tickflow.repository.KlineRepository
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from app.config import settings
from app.services import kline_sync
from app.services.pipeline_jobs import job_store
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

_DEMO_SYMBOLS = (
    "600000.SH", "600036.SH", "600519.SH", "601318.SH", "601398.SH",
    "000001.SZ", "000333.SZ", "000651.SZ", "000858.SZ", "002594.SZ",
)


def _noop(stage: str, pct: int, msg: str, **kwargs) -> None:  # noqa: ARG001
    pass


def _invalidate(table: str | None = None) -> None:
    from app.api.data import invalidate_data_cache
    invalidate_data_cache(table)


def _resolve_universe(capset: CapabilitySet) -> list[str]:
    """解析标的池 — 与 daily_pipeline 独立的副本。"""
    if capset.has(Cap.KLINE_DAILY_BATCH):
        provider_name = "tickflow"
        try:
            provider = kline_sync._get_data_provider()
            provider_name = provider.name
            if provider.name != "tickflow" and provider.capabilities.instruments:
                inst = provider.get_instruments("stock")
                if not inst.is_empty() and "symbol" in inst.columns:
                    return sorted(inst["symbol"].cast(pl.Utf8).to_list())
        except Exception as e:
            logger.warning("provider instruments pool unavailable: %s", e)
        if provider_name == "tickflow":
            try:
                from app.tickflow.pools import get_pool
                all_a = get_pool("CN_Equity_A", refresh=True)
                if all_a:
                    return sorted(all_a)
            except Exception as e:
                logger.warning("CN_Equity_A pool unavailable: %s", e)

    base: set[str] = set(_DEMO_SYMBOLS)
    base.update(_load_watchlist_symbols())
    d = Path(settings.data_dir)
    inst_path = d / "instruments" / "instruments.parquet"
    if inst_path.exists():
        try:
            inst = pl.read_parquet(inst_path, columns=["symbol"])
            base.update(inst["symbol"].to_list())
        except Exception as e:
            logger.warning("instruments supplement failed: %s", e)
    return sorted(base)


def _load_watchlist_symbols() -> list[str]:
    path = Path(settings.data_dir) / "user_data" / "watchlist.parquet"
    if not path.exists():
        return []
    try:
        df = pl.read_parquet(path, columns=["symbol"])
    except Exception as e:
        logger.warning("watchlist supplement failed: %s", e)
        return []
    if df.is_empty() or "symbol" not in df.columns:
        return []
    return df["symbol"].cast(pl.Utf8).to_list()


def _refresh_single_view(repo: KlineRepository, name: str) -> None:
    """刷新单个 DuckDB 视图。"""
    d = repo.store.data_dir.as_posix()
    paths = {
        "kline_daily": f"{d}/kline_daily/**/*.parquet",
        "kline_enriched": f"{d}/kline_daily_enriched/**/*.parquet",
        "kline_minute": f"{d}/kline_minute/**/*.parquet",
        "adj_factor": f"{d}/adj_factor/**/*.parquet",
        "instruments": f"{d}/instruments/**/*.parquet",
    }
    path = paths.get(name)
    if not path:
        return
    try:
        repo.db.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_parquet('{path}', union_by_name=true)"
        )
    except Exception as e:
        logger.warning("refresh view %s failed: %s", name, e)


def compute_offset(value: int, unit: str) -> timedelta:
    """将用户输入的 value + unit 转成 timedelta。"""
    if unit == "day":
        return timedelta(days=value)
    elif unit == "month":
        return timedelta(days=value * 30)
    elif unit == "year":
        return timedelta(days=value * 365)
    else:
        raise ValueError(f"不支持的单位: {unit}")


def run_extend_history(
    repo: KlineRepository,
    capset: CapabilitySet,
    value: int,
    unit: str,
    on_progress: Callable | None = None,
) -> dict:
    """向前扩展历史数据的主函数。

    完全独立于 daily_pipeline.run_now(),不调用其任何逻辑。
    返回结果 dict 供 job_store 记录。
    """
    emit = on_progress or _noop

    # 0. 计算时间偏移
    offset = compute_offset(value, unit)
    today = date.today()

    # 1. 获取当前最早日期
    emit("extend_history", 2, "检查当前数据范围…")
    earliest = repo.earliest_daily_date()

    if not earliest:
        return {"error": "本地无日K数据,请先执行一次完整同步"}

    new_start = earliest - offset
    # 不能超过今天
    if new_start >= earliest:
        return {"error": "扩展范围无效,请增大时间跨度"}

    # 2. 解析标的池
    emit("extend_history", 5, "解析标的池…")
    universe = _resolve_universe(capset)
    if not universe:
        return {"error": "标的池为空"}
    emit("extend_history", 8, f"标的池: {len(universe)} 只")

    start_str = new_start.strftime("%Y-%m-%d")
    end_str = earliest.strftime("%Y-%m-%d")

    # 3. 拉日 K
    emit("extend_history", 10, f"获取日K [{start_str} ~ {end_str}]…")
    logger.info("extend_history: daily K [%s ~ %s], %d symbols", start_str, end_str, len(universe))

    def _daily_chunk(cur: int, tot: int) -> None:
        emit("extend_history", 10 + int(35 * cur / tot),
             f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

    written_daily = kline_sync.sync_and_persist_daily_batch(
        universe, repo, capset,
        start_date=datetime.combine(new_start, datetime.min.time()),
        end_date=datetime.combine(earliest, datetime.min.time()),
        on_chunk_done=_daily_chunk,
    )
    emit("extend_history", 45, f"日K 完成,写入 {written_daily} 行")
    logger.info("extend_history: daily K done, %d rows", written_daily)
    _refresh_single_view(repo, "kline_daily")
    _invalidate("daily")

    # 4. 拉除权因子 (新范围)
    written_adj = 0
    adj_start = datetime.combine(new_start, datetime.min.time())
    adj_end = datetime.combine(today, datetime.min.time())
    adj_start_str = new_start.strftime("%Y-%m-%d")
    adj_end_str = today.strftime("%Y-%m-%d")

    if capset.has(Cap.ADJ_FACTOR):
        emit("extend_history", 48, f"获取除权因子 [{adj_start_str} ~ {adj_end_str}]…")
        logger.info("extend_history: adj_factor [%s ~ %s]", adj_start_str, adj_end_str)

        def _adj_chunk(cur: int, tot: int) -> None:
            emit("extend_history", 48 + int(10 * cur / tot),
                 f"除权因子批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

        written_adj, _affected = kline_sync.sync_adj_factor(
            universe, repo, capset,
            start_time=adj_start, end_time=adj_end,
            on_chunk_done=_adj_chunk,
        )
        emit("extend_history", 60, f"除权因子完成,{written_adj} 行")
        logger.info("extend_history: adj_factor done, %d rows", written_adj)
        _refresh_single_view(repo, "adj_factor")
        _invalidate("adj_factor")
    else:
        emit("extend_history", 60, "除权因子跳过(无权限)")
        logger.info("extend_history: adj_factor skipped, no ADJ_FACTOR capability")

    # 5. 全量重算 enriched
    emit("extend_history", 65, "全量计算 enriched…")
    logger.info("extend_history: full enriched rebuild start")

    from app.indicators.pipeline import run_pipeline
    written_enriched = run_pipeline()

    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_dir.exists() else 0
    emit("extend_history", 92, f"enriched 完成,覆盖 {enriched_days} 天")
    logger.info("extend_history: enriched done, %d days", enriched_days)
    _refresh_single_view(repo, "kline_enriched")
    _invalidate("enriched")

    # 6. 刷新视图
    emit("extend_history", 95, "刷新视图…")
    _refresh_single_view(repo, "kline_daily")
    _refresh_single_view(repo, "kline_enriched")
    _refresh_single_view(repo, "adj_factor")
    _invalidate(None)

    # 7. 统计结果
    daily_dir = repo.store.data_dir / "kline_daily"
    daily_days = len(list(daily_dir.glob("date=*"))) if daily_dir.exists() else 0

    emit("extend_history", 100, f"完成,已扩展至 {new_start}")

    return {
        "earliest_before": earliest.isoformat(),
        "earliest_after": new_start.isoformat(),
        "daily_rows": written_daily,
        "daily_days": daily_days,
        "adj_factor_rows": written_adj,
        "enriched_days": enriched_days,
        "universe_size": len(universe),
    }
