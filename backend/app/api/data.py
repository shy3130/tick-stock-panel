"""数据画像 API —— 让前端知道"我们本地有什么数据"。"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.enriched_generation import EnrichedPublication
from app.indicators.pipeline import ENRICHED_COLUMNS
from app.services.data_status_cache import DataStatusCache
from app.services.pipeline_jobs import job_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data", tags=["data"])


@router.get("/workload")
def get_workload(request: Request) -> dict:
    """进程级请求/实际计算计数, 不含用户参数; 沿用面板认证。"""
    from app.services import minute_shared_pull, screener_work, strategy_run_queue
    from app.services.heavy_job_limiter import shared_heavy_job_limiter
    from app.services.workload import workload

    result = workload.snapshot()
    result["resources"] = {
        "heavy_jobs": shared_heavy_job_limiter.snapshot(),
        "strategy_queue": strategy_run_queue.MANAGER.snapshot(),
        "strategy_results": screener_work.batch_results.cache_info(),
        "minute_results": minute_shared_pull.cache_info(),
    }
    qs = getattr(request.app.state, "quote_service", None)
    if qs is not None:
        result["resources"]["sse"] = qs.stream_versions()
    return result


class IntegrityScanIn(BaseModel):
    lookback_days: int = Field(default=30, ge=1, le=366)
    asset_types: list[Literal["stock", "etf", "index"]] | None = Field(default=None, min_length=1, max_length=3)


class IntegrityRepairIn(BaseModel):
    report_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def _start_integrity_job(request: Request, operation: Callable, *, repair: bool) -> dict:
    from app.services.pipeline_jobs import (
        JobCancelledError,
        release_run_slot,
        run_with_capacity,
        try_acquire_run_slot,
    )

    job_id, is_new = job_store.create()
    if not is_new:
        raise HTTPException(status_code=409, detail="已有数据任务正在运行, 请等待完成后再自检或修补")

    def progress(stage, pct, message, **kwargs):
        job_store.progress(job_id, stage, pct, message, **kwargs)

    def run():
        qs = getattr(request.app.state, "quote_service", None)
        with qs.paused() if repair and qs else contextlib.nullcontext():
            return operation(progress)

    def execute():
        try:
            if not try_acquire_run_slot(job_id):
                job_store.fail(job_id, "已有数据任务尚未结束, 请稍后重试")
                return
            report = run_with_capacity(job_id, run)
            job_store.succeed(job_id, {"integrity_report": report})
        except JobCancelledError:
            pass
        except Exception as exc:
            logger.exception("日K自检/修补失败: %s", job_id)
            job_store.fail(job_id, str(exc))
        finally:
            release_run_slot(job_id)
            invalidate_data_cache()
            qs = getattr(request.app.state, "quote_service", None)
            notify = getattr(qs, "notify_daily_integrity_updated", None)
            if callable(notify):
                from app.services.integrity_check import latest_report
                report = latest_report(request.app.state.repo.store.data_dir)
                notify(report["asset_types"] if report else [], data_changed=repair)

    try:
        threading.Thread(target=execute, daemon=True, name=f"integrity-{job_id}").start()
    except RuntimeError:
        job_store.fail(job_id, "无法启动数据自检任务")
        raise
    return {"job_id": job_id}


@router.get("/integrity")
def get_integrity_report(request: Request) -> dict:
    from app.services import daily_health
    from app.services.integrity_check import latest_report

    root = request.app.state.repo.store.data_dir
    return {"report": latest_report(root), "blocking_assets": sorted({
        e["asset_type"] for e in daily_health.snapshot(root)["entries"]
    }), "data_revisions": {
        asset: daily_health.revision(root, asset) for asset in daily_health.ASSET_LABELS
    }}


@router.post("/integrity/scan")
def scan_integrity(body: IntegrityScanIn, request: Request) -> dict:
    from app.services.integrity_check import run_check

    root = request.app.state.repo.store.data_dir
    return _start_integrity_job(request, lambda progress: run_check(
        root, lookback_days=body.lookback_days, asset_types=body.asset_types, on_progress=progress,
    ), repair=False)


@router.post("/integrity/repair")
def repair_integrity(body: IntegrityRepairIn, request: Request) -> dict:
    from app.data_providers import custom
    from app.services import preferences
    from app.services.integrity_check import latest_report, run_repair
    from app.tickflow.capabilities import Cap

    state = request.app.state
    report = latest_report(state.repo.store.data_dir)
    if report is None or report.get("id") != body.report_id:
        raise HTTPException(status_code=409, detail="自检报告已变化, 请重新自检后再修补")
    if not report.get("repairable_count"):
        raise HTTPException(status_code=400, detail="当前没有可自动修补的问题")
    provider = preferences.get_daily_data_provider()
    custom_daily = provider != "tickflow" and custom.provider_has_dataset(provider, "daily")
    if not custom_daily and not state.capabilities.has(Cap.KLINE_DAILY_BATCH):
        raise HTTPException(status_code=403, detail="当前日K数据源不可用, 请先检查数据源配置")
    return _start_integrity_job(request, lambda progress: run_repair(
        state.repo, state.capabilities, body.report_id, on_progress=progress,
    ), repair=True)

# 表统计仍沿用 TTL + 写入事件失效; 请求只返回快照, 重统计由单个后台线程执行。
_TABLE_TTLS = {
    name: 120.0 if name == "minute" else 30.0
    for name in (
        "daily", "enriched", "index_daily", "index_enriched", "index_instruments",
        "etf_daily", "etf_enriched", "etf_instruments", "minute", "adj_factor",
        "instruments", "financials",
    )
}
_status_cache = DataStatusCache({**_TABLE_TTLS, "storage": 60.0, "last_runs": float("inf")})


def invalidate_data_cache(table: str | None = None, *, discard_stale: bool = False) -> None:
    """写入后标记统计过期; 普通刷新保留旧快照, 清空数据时丢弃旧值。"""
    _status_cache.invalidate(table, discard_stale=discard_stale)


def invalidate_storage_cache() -> None:
    """向后兼容入口 — 清全部缓存。新代码请用 invalidate_data_cache(table)。"""
    invalidate_data_cache(None)


def _safe_aggregate(repo, view: str) -> dict | None:
    """聚合视图基础统计;视图不存在或为空时返 None。"""
    try:
        row = repo.execute_one(
            f"""SELECT count(*) AS rows,
                       min(date) AS earliest,
                       max(date) AS latest,
                       count(DISTINCT symbol) AS symbols,
                       count(DISTINCT date) AS trading_days
                FROM {view}"""
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("aggregate %s failed: %s", view, e)
        return None
    if not row or not row[0]:
        return None
    return {
        "rows": int(row[0]),
        "earliest_date": str(row[1]) if row[1] else None,
        "latest_date": str(row[2]) if row[2] else None,
        "symbols_covered": int(row[3] or 0),
        "trading_days": int(row[4] or 0),
    }


def _safe_aggregate_daily(repo, view: str = "kline_daily") -> dict | None:
    """日K轻量统计 — 零数据扫描。

    从分区目录名获取日期范围和交易日数，不读任何 parquet。
    标的数从 instruments 小表获取（~5000行，毫秒级）。
    """
    daily_dir = repo.store.data_dir / "kline_daily"
    if not daily_dir.exists():
        return None
    dates: list[str] = []
    for d in daily_dir.iterdir():
        if d.is_dir() and d.name.startswith("date="):
            dates.append(d.name[5:])
    if not dates:
        return None
    dates.sort()

    symbols = _count_instruments_symbols(repo)

    return {
        "rows": 0,
        "earliest_date": dates[0],
        "latest_date": dates[-1],
        "symbols_covered": symbols,
        "trading_days": len(dates),
    }


def _safe_aggregate_enriched(repo) -> dict | None:
    """Enriched 轻量统计 — 零数据扫描。

    字段数从 DESCRIBE 读 schema（不碰数据），毫秒级。
    日期范围从分区目录名获取（同 minute 策略），不读任何 parquet。
    标的数从 instruments 小表取。
    """
    # 字段数：读 schema，不碰数据
    fields = 0
    try:
        cols = repo.execute_all("DESCRIBE kline_enriched")
        fields = len(cols)
    except Exception:  # noqa: BLE001
        pass

    # 日期范围：从分区目录名获取，不扫数据
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    if not enriched_dir.exists():
        return None
    dates: list[str] = []
    for d in enriched_dir.iterdir():
        if d.is_dir() and d.name.startswith("date="):
            dates.append(d.name[5:])
    if not dates:
        return None
    dates.sort()

    symbols = _count_instruments_symbols(repo)

    return {
        "rows": 0,
        "fields": fields,
        "earliest_date": dates[0],
        "latest_date": dates[-1],
        "symbols_covered": symbols,
        "trading_days": len(dates),
    }


def _count_instruments_symbols(repo) -> int:
    """从 instruments 小表取标的数（~5000行，毫秒级）。"""
    try:
        sym_row = repo.execute_one(
            "SELECT count(DISTINCT symbol) FROM instruments"
        )
        if sym_row and sym_row[0]:
            return int(sym_row[0])
    except Exception:  # noqa: BLE001
        pass
    return 0


def _safe_aggregate_instruments(repo) -> dict | None:
    """instruments 视图统计(无 date 列,用 as_of)。"""
    try:
        row = repo.execute_one(
            """SELECT count(*) AS rows,
                      count(DISTINCT symbol) AS symbols,
                      max(as_of) AS latest_as_of,
                      count_if(name IS NOT NULL AND name != '') AS named
               FROM instruments"""
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("aggregate instruments failed: %s", e)
        return None
    if not row or not row[0]:
        return None
    return {
        "rows": int(row[0]),
        "symbols_covered": int(row[1] or 0),
        "latest_as_of": str(row[2]) if row[2] else None,
        "named": int(row[3] or 0),
    }


def _safe_aggregate_index_daily(repo) -> dict | None:
    """指数日K统计。指数数据量较小，直接读取 parquet 元数据统计真实行数。"""
    return _safe_aggregate(repo, "kline_index_daily")


def _safe_aggregate_index_enriched(repo) -> dict | None:
    """指数 enriched 统计。指数数据量较小，直接读取 parquet 元数据统计真实行数。"""
    fields = 0
    try:
        cols = repo.execute_all("DESCRIBE kline_index_enriched")
        fields = len(cols)
    except Exception:  # noqa: BLE001
        pass
    stats = _safe_aggregate(repo, "kline_index_enriched")
    if not stats:
        return None
    return {**stats, "fields": fields}


def _safe_aggregate_index_instruments(repo) -> dict | None:
    """指数 instruments 视图统计。"""
    try:
        row = repo.execute_one(
            """SELECT count(*) AS rows,
                      count(DISTINCT symbol) AS symbols,
                      count_if(name IS NOT NULL AND name != '') AS named
               FROM instruments_index"""
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("aggregate instruments_index failed: %s", e)
        return None
    if not row or not row[0]:
        return None
    return {
        "rows": int(row[0]),
        "symbols_covered": int(row[1] or 0),
        "latest_as_of": None,
        "named": int(row[2] or 0),
    }


def _safe_aggregate_etf_instruments(repo) -> dict | None:
    """ETF instruments 统计 — 优先独立 instruments_etf，兼容旧 instruments_index。"""
    queries = [
        """SELECT count(*) AS rows,
                  count(DISTINCT symbol) AS symbols,
                  count_if(name IS NOT NULL AND name != '') AS named
           FROM instruments_etf""",
        """SELECT count(*) AS rows,
                  count(DISTINCT symbol) AS symbols,
                  count_if(name IS NOT NULL AND name != '') AS named
           FROM instruments_index
           WHERE asset_type = 'etf'""",
    ]
    for sql in queries:
        try:
            row = repo.execute_one(sql)
        except Exception as e:  # noqa: BLE001
            logger.debug("aggregate etf instruments fallback failed: %s", e)
            continue
        if row and row[0]:
            return {
                "rows": int(row[0]),
                "symbols_covered": int(row[1] or 0),
                "latest_as_of": None,
                "named": int(row[2] or 0),
            }
    return None


def _safe_aggregate_etf_enriched(repo) -> dict | None:
    """ETF enriched 统计 — 独立 kline_etf_enriched。"""
    fields = 0
    try:
        cols = repo.execute_all("DESCRIBE kline_etf_enriched")
        fields = len(cols)
    except Exception:  # noqa: BLE001
        pass
    stats = _safe_aggregate(repo, "kline_etf_enriched")
    if not stats:
        return None
    return {**stats, "fields": fields}


def _safe_aggregate_etf_daily(repo) -> dict | None:
    """ETF 日K统计 — 优先独立 kline_etf_daily，兼容旧 index 存储。"""
    queries = [
        """SELECT count(*) AS rows,
                  min(date) AS earliest,
                  max(date) AS latest,
                  count(DISTINCT symbol) AS symbols,
                  count(DISTINCT date) AS trading_days
           FROM kline_etf_daily""",
        """SELECT count(*) AS rows,
                  min(date) AS earliest,
                  max(date) AS latest,
                  count(DISTINCT symbol) AS symbols,
                  count(DISTINCT date) AS trading_days
           FROM kline_index_daily
           WHERE symbol IN (
               SELECT DISTINCT symbol FROM instruments_index WHERE asset_type = 'etf'
           )""",
    ]
    for sql in queries:
        try:
            row = repo.execute_one(sql)
        except Exception as e:  # noqa: BLE001
            logger.debug("aggregate etf daily fallback failed: %s", e)
            continue
        if row and row[0]:
            return {
                "rows": int(row[0]),
                "earliest_date": str(row[1]) if row[1] else None,
                "latest_date": str(row[2]) if row[2] else None,
                "symbols_covered": int(row[3] or 0),
                "trading_days": int(row[4] or 0),
            }
    return None


def _safe_aggregate_adj_factor(repo) -> dict | None:
    """adj_factor 视图统计,日期范围对齐日 K 覆盖区间。"""
    try:
        # 取日 K 的日期范围作为过滤条件
        dr = repo.execute_one(
            "SELECT min(date), max(date) FROM kline_daily"
        )
        if not dr or not dr[0]:
            return None
        d_min, d_max = dr[0], dr[1]
        row = repo.execute_one(
            """SELECT count(*) AS rows,
                      count(DISTINCT symbol) AS symbols,
                      count(DISTINCT trade_date) AS trading_days
               FROM adj_factor
               WHERE trade_date BETWEEN ? AND ?""",
            [str(d_min), str(d_max)],
        )
        if not row or not row[0]:
            return None
        return {
            "rows": int(row[0]),
            "symbols_covered": int(row[1]) if isinstance(row[1], (int, float)) else 0,
            "earliest_date": str(d_min),
            "latest_date": str(d_max),
            "trading_days": int(row[2] or 0),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("aggregate adj_factor failed: %s", e)
        return None


def _safe_aggregate_minute(repo) -> dict | None:
    """kline_minute 统计 — 从分区目录名获取交易日数，跳过全表扫描。

    分钟 K 按 date=YYYY-MM-DD 分区存储，直接数目录即可，
    无需 count(*) / count(DISTINCT ...) 等昂贵查询。
    """
    minute_dir = repo.store.data_dir / "kline_minute"
    if not minute_dir.exists():
        return None

    # 从 date=YYYY-MM-DD 目录名提取交易日
    dates: list[str] = []
    for d in minute_dir.iterdir():
        if d.is_dir() and d.name.startswith("date="):
            dates.append(d.name[5:])

    if not dates:
        return None

    dates.sort()
    return {
        "rows": 0,  # 不再查询行数
        "earliest_date": dates[0],
        "latest_date": dates[-1],
        "symbols_covered": 0,  # 不再查询标的数
        "trading_days": len(dates),
    }


def _safe_aggregate_financials(repo) -> dict | None:
    """财务数据统计 — 检查各表文件是否存在及行数。"""
    data_dir = repo.store.data_dir
    tables_info: dict[str, dict] = {}
    total_rows = 0

    for table in ("metrics", "income", "balance_sheet", "cash_flow", "shares"):
        path = data_dir / "financials" / table / "part.parquet"
        if path.exists():
            try:
                import polars as pl
                df = pl.read_parquet(path, columns=["symbol"])
                rows = len(df)
                symbols = df["symbol"].n_unique() if not df.is_empty() else 0
                tables_info[table] = {"rows": rows, "symbols": symbols}
                total_rows += rows
            except Exception:
                tables_info[table] = {"rows": 0, "symbols": 0}
        else:
            tables_info[table] = {"rows": 0, "symbols": 0}

    if total_rows == 0:
        return None

    return {
        "rows": total_rows,
        "tables": tables_info,
    }


def _scan_dir_stats(dirpath: Path) -> tuple[int, float]:
    """单次遍历统计目录下文件数和总大小(MB)。比 rglob+stat 快很多。"""
    if not dirpath.exists():
        return 0, 0.0
    count = 0
    total = 0
    for entry in os.scandir(dirpath):
        if entry.is_dir(follow_symlinks=False):
            c, s = _scan_dir_recursive(entry)
            count += c
            total += s
        elif entry.is_file(follow_symlinks=False):
            try:
                total += entry.stat().st_size
            except OSError:
                pass
            count += 1
    return count, round(total / 1048576, 2)


def _scan_dir_recursive(entry: os.DirEntry) -> tuple[int, int]:
    """递归统计一个 DirEntry 下的文件数和总字节数。"""
    count = 0
    total = 0
    try:
        for sub in os.scandir(entry.path):
            if sub.is_dir(follow_symlinks=False):
                c, s = _scan_dir_recursive(sub)
                count += c
                total += s
            elif sub.is_file(follow_symlinks=False):
                try:
                    total += sub.stat().st_size
                except OSError:
                    pass
                count += 1
    except PermissionError:
        pass
    return count, total


def _compute_storage(data_dir: Path) -> dict:
    """单次遍历计算 storage 统计，避免多次 rglob。"""
    import os

    # 只统计关心的子目录
    subdirs = {
        "daily": data_dir / "kline_daily",
        "enriched": data_dir / "kline_daily_enriched",
        "index_daily": data_dir / "kline_index_daily",
        "index_enriched": data_dir / "kline_index_enriched",
        "index_instruments": data_dir / "instruments_index",
        "etf_daily": data_dir / "kline_etf_daily",
        "etf_enriched": data_dir / "kline_etf_enriched",
        "etf_instruments": data_dir / "instruments_etf",
        "etf_adj_factor": data_dir / "adj_factor_etf",
        "minute": data_dir / "kline_minute",
        "adj_factor": data_dir / "adj_factor",
        "instruments": data_dir / "instruments",
        "ext_data": data_dir / "ext_data",
    }
    stats = {}
    total_size = 0
    for key, d in subdirs.items():
        fc, sz = _scan_dir_stats(d)
        total_size += sz
        stats[f"{key}_files"] = fc
        stats[f"{key}_size_mb"] = sz

    # total: 再加上其他零散目录 (financials 有下方专属明细统计, 不在此列)
    other_dirs = ["pools", "backtest_results", "screener_results", "ai_cache"]
    for name in other_dirs:
        d = data_dir / name
        if d.exists():
            _, s = _scan_dir_stats(d)
            total_size += s

    # financials 单独统计 (明细与 total 各计入一次, 不得与其他目录循环重复累加)
    fin_dir = data_dir / "financials"
    if fin_dir.exists():
        fc, sz = _scan_dir_stats(fin_dir)
        stats["financials_files"] = fc
        stats["financials_size_mb"] = sz
        total_size += sz
    # 根目录散文件
    for entry in os.scandir(data_dir):
        if entry.is_file(follow_symlinks=False):
            try:
                total_size += entry.stat().st_size / 1048576
            except OSError:
                pass
    stats["total_size_mb"] = round(total_size, 2)
    return stats


def _next_cron_run(scheduler, job_id: str) -> str | None:
    """读 APScheduler 下次执行时间。"""
    if not scheduler:
        return None
    try:
        job = scheduler.get_job(job_id)
        if job and job.next_run_time:
            return job.next_run_time.isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        pass
    return None


def _load_last_runs() -> dict[str, str | None]:
    """只在后台扫描任务记录, 与表统计一起发布。"""
    cache: dict[str, str | None] = {"last_instruments_run": None, "last_pipeline_run": None}
    for job in job_store.list_recent(limit=50):
        if job["status"] not in ("succeeded", "failed"):
            continue
        result = job.get("result") or {}
        if "instruments_rows" in result and cache["last_instruments_run"] is None:
            cache["last_instruments_run"] = job["finished_at"]
        if "daily_days" in result and cache["last_pipeline_run"] is None:
            cache["last_pipeline_run"] = job["finished_at"]
    return cache


def _data_pipeline_busy(request: Request) -> bool:
    """同步任务运行中或指标预热未完成 — /status 的重聚合应退化为纯缓存读。"""
    if not getattr(request.app.state, "indicators_ready", True):
        return True
    return job_store.active_status() in ("pending", "running")


@router.get("/status")
async def status(request: Request) -> dict:
    repo = request.app.state.repo
    scheduler = getattr(request.app.state, "scheduler", None)
    aggregates = {
        "daily": _safe_aggregate_daily,
        "enriched": _safe_aggregate_enriched,
        "index_daily": _safe_aggregate_index_daily,
        "index_enriched": _safe_aggregate_index_enriched,
        "index_instruments": _safe_aggregate_index_instruments,
        "etf_daily": _safe_aggregate_etf_daily,
        "etf_enriched": _safe_aggregate_etf_enriched,
        "etf_instruments": _safe_aggregate_etf_instruments,
        "minute": _safe_aggregate_minute,
        "adj_factor": _safe_aggregate_adj_factor,
        "instruments": _safe_aggregate_instruments,
        "financials": _safe_aggregate_financials,
    }
    loaders = {name: lambda fetch=fetch: fetch(repo) for name, fetch in aggregates.items()}
    loaders["storage"] = lambda: _compute_storage(repo.store.data_dir)
    loaders["last_runs"] = _load_last_runs
    snapshot = _status_cache.snapshot(loaders, is_busy=lambda: _data_pipeline_busy(request))
    last_runs = snapshot.pop("last_runs") or {
        "last_instruments_run": None, "last_pipeline_run": None,
    }
    return {
        **snapshot,
        **last_runs,
        "next_instruments_run": _next_cron_run(scheduler, "pre_market_instruments"),
        "next_pipeline_run": _next_cron_run(scheduler, "daily_pipeline"),
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "indicators_ready": getattr(request.app.state, "indicators_ready", True),
    }


@router.post("/clear")
def clear_data(request: Request):
    """清除所有本地 Parquet 数据（保留 capabilities.json 和目录结构）。

    互斥保护 (与其他破坏性路径 pipeline/repair/rebuild 一致):
    - 重任务执行槽: 有数据任务在运行 (或上次任务卡死未结束) 时 fail-closed
      拒绝清空 — 一边删除一边写入会让终态既非"已清空"也非"完整同步";
    - 清空期间暂停实时行情取数, 防止轮询线程覆写同一批 parquet。
    """
    from app.services.pipeline_jobs import release_run_slot, try_acquire_run_slot

    if not try_acquire_run_slot("data_clear"):
        raise HTTPException(
            status_code=409,
            detail="有数据任务在运行（或上一次任务卡死未结束），请先停止任务或稍后再试",
        )
    qs = getattr(request.app.state, "quote_service", None)
    # 全量分钟增量线程独立写 kline_minute/adj_factor 分区, 不占 run slot —
    # 清空期间必须一并暂停, 否则"清完又被写回部分今日数据"(终态不完整)。
    mrs = getattr(request.app.state, "minute_refresh", None)
    mrs_was_running = False
    try:
        if mrs is not None:
            with contextlib.suppress(Exception):
                mrs_was_running = bool(mrs.status().get("running"))
                mrs.stop()
        if qs is not None:
            with qs.paused():
                return _clear_data_impl(request)
        return _clear_data_impl(request)
    finally:
        if mrs is not None and mrs_was_running:
            with contextlib.suppress(Exception):
                mrs.start()
        release_run_slot("data_clear")


def _clear_data_impl(request: Request):
    import shutil

    repo = request.app.state.repo
    data_dir = repo.store.data_dir
    from app.services.daily_health import clear_assets
    deleted = 0
    # recover=True: 清空语义就是接管一切 — 外部进程(崩掉的脚本/中断的管道)
    # 残留的 publishing 标记(owner pid 已死)不应永久阻塞清空; 活进程的发布
    # 仍会被拦(another publication is active), 写盘竞态保护不受影响。
    publications = {
        "kline_daily_enriched": EnrichedPublication(data_dir, "stock", recover=True),
        "kline_etf_enriched": EnrichedPublication(data_dir, "etf", recover=True),
    }

    for sub in (
        "kline_daily", "kline_daily_enriched", "kline_index_daily", "kline_index_enriched",
        "kline_etf_daily", "kline_etf_enriched", "kline_etf_minute", "kline_minute",
        "adj_factor", "adj_factor_etf", "instruments", "instruments_index", "instruments_etf", "pools", "financials",
        "backtest_results", "screener_results", "ai_cache",
    ):
        d = data_dir / sub
        if not d.exists():
            continue
        publication = publications.get(sub)
        parquet_files = list(d.rglob("*.parquet"))
        if publication is not None and parquet_files:
            publication.begin()
        try:
            for f in parquet_files:
                f.unlink()
                deleted += 1
                if publication is not None:
                    publication.mark_changed()
            for child in list(d.iterdir()):
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
            if publication is not None:
                publication.commit()
        except BaseException:
            if publication is not None:
                publication.abandon()
            raise

    clear_assets(data_dir, {"stock", "etf", "index"})
    (data_dir / "user_data" / "integrity_report.json").unlink(missing_ok=True)
    # 清除同步历史（内存 + 磁盘 job_store/ 文件夹）
    from app.services.pipeline_jobs import job_store
    job_store.clear()

    # 清除财务数据
    fin_dir = data_dir / "financials"
    for sub in ("metrics", "income", "balance_sheet", "cash_flow"):
        fp = fin_dir / sub / "part.parquet"
        if fp.exists():
            fp.unlink()
            deleted += 1

    # 清除监控运行数据 (user_data 下仅清运行产物, 不动 monitor_rules/preferences/secrets 等用户配置)
    # - 触发记录 alerts.jsonl
    from app.services import alert_store
    alert_store.clear(data_dir)
    # - 待推送的实时通知队列 (进程内存)
    qs = getattr(request.app.state, "quote_service", None)
    if qs is not None:
        qs.clear_pending_alerts()

    # 清除 Polars 缓存
    # 先 clear_cache 无条件清空内存 (refresh_cache 在磁盘无数据时会提前 return,
    # 导致 _enriched_cache 等旧数据残留 —— 清数据后看板仍显示旧数据的根因),
    # 再 refresh_cache 尝试重载 (磁盘有数据则重建缓存)。
    repo.clear_cache()
    repo.refresh_cache()

    # 清除 Screener 进程级 _history_cache (TTL 缓存)
    from app.services.screener import ScreenerService
    ScreenerService.clear_history_cache()

    # 清除 Overview 总览聚合结果缓存 (5s TTL)
    from app.api.overview import invalidate_overview_cache
    invalidate_overview_cache()

    # 刷新 DuckDB 视图（空 parquet 目录也需要重新挂载）——
    # 委托给 repository 的唯一权威实现, 覆盖全部视图 (此前这里内联的副本漏了几张)。
    repo.rebuild_views()

    logger.info("数据已清除: 删除 %d 个 parquet 文件", deleted)
    invalidate_data_cache(None, discard_stale=True)
    return {"deleted_files": deleted}


# 各表字段说明
_TABLE_FIELD_DESC: dict[str, dict[str, str]] = {
    "kline_daily": {
        "symbol": "股票代码",
        "date": "交易日期",
        "open": "开盘价",
        "high": "最高价",
        "low": "最低价",
        "close": "收盘价",
        "volume": "成交量",
        "amount": "成交额",
    },
    "kline_enriched": ENRICHED_COLUMNS,
    "kline_index_daily": {
        "symbol": "指数代码",
        "date": "交易日期",
        "open": "开盘点位",
        "high": "最高点位",
        "low": "最低点位",
        "close": "收盘点位",
        "volume": "成交量",
        "amount": "成交额",
    },
    "kline_index_enriched": ENRICHED_COLUMNS,
    "kline_etf_daily": {
        "symbol": "ETF代码",
        "date": "交易日期",
        "open": "开盘价",
        "high": "最高价",
        "low": "最低价",
        "close": "收盘价",
        "volume": "成交量",
        "amount": "成交额",
    },
    "kline_etf_enriched": ENRICHED_COLUMNS,
    "kline_minute": {
        "symbol": "股票代码",
        "datetime": "分钟时间戳",
        "open": "开盘价",
        "high": "最高价",
        "low": "最低价",
        "close": "收盘价",
        "volume": "成交量",
        "amount": "成交额",
    },
    "adj_factor": {
        "symbol": "股票代码",
        "trade_date": "除权除息日",
        "ex_factor": "单事件等比因子 = 除权前收盘价 / 除权参考价, 按日期连乘得累计复权因子",
        # 事件明细列: 表结构固定 8 列, 明细由提供事件的数据源(扶摇)回填, TickFlow 源为 null
        "dividend": "每股现金分红(税前, 元)。可空, 仅事件明细数据源回填",
        "bonus": "每股送转比例(10送3 = 0.3)。可空, 仅事件明细数据源回填",
        "allot": "每股配股比例。可空, 仅事件明细数据源回填",
        "allot_price": "配股价(元/股)。可空, 仅事件明细数据源回填",
        "prev_close": "除权前一日收盘价(元), 因子推导基准。可空, 仅事件明细数据源回填",
    },
    "instruments": {
        "symbol": "股票代码",
        "name": "股票名称",
        "code": "股票编码(纯数字)",
        "exchange": "交易所(SH/SZ/BJ)",
        "region": "地区",
        "type": "证券类型",
        "listing_date": "上市日期",
        "total_shares": "总股本",
        "float_shares": "流通股本",
        "tick_size": "最小价格变动单位",
        "limit_up": "涨停限制(%)",
        "limit_down": "跌停限制(%)",
        "as_of": "快照日期",
    },
    "instruments_index": {
        "symbol": "指数代码",
        "name": "指数名称",
        "code": "指数编码(纯数字)",
        "asset_type": "资产类型(index)",
    },
    "instruments_etf": {
        "symbol": "ETF代码",
        "name": "ETF名称",
        "code": "ETF编码(纯数字)",
        "asset_type": "资产类型(etf)",
        "source": "数据源",
    },
}

# view 名 → DuckDB 视图名
_SCHEMA_VIEWS: dict[str, str] = {
    "daily": "kline_daily",
    "enriched": "kline_enriched",
    "index_daily": "kline_index_daily",
    "index_enriched": "kline_index_enriched",
    "index_instruments": "instruments_index",
    "etf_daily": "kline_etf_daily",
    "etf_enriched": "kline_etf_enriched",
    "etf_instruments": "instruments_etf",
    "minute": "kline_minute",
    "adj_factor": "adj_factor",
    "instruments": "instruments",
}


@router.get("/schema/{table}")
def table_schema(request: Request, table: str) -> list[dict]:
    """返回指定表的字段名、类型和中文说明。

    优先从 DuckDB DESCRIBE 读取(有数据时含精确类型)；
    视图不存在(无数据)时回退到 _TABLE_FIELD_DESC 静态定义。
    """
    view = _SCHEMA_VIEWS.get(table)
    if not view:
        return []
    desc_map = _TABLE_FIELD_DESC.get(view, {})
    repo = request.app.state.repo
    fields: list[dict] = []
    try:
        cols = repo.execute_all(f"DESCRIBE {view}")
        for col in cols:
            name = col[0]
            dtype = col[1]
            fields.append({
                "name": name,
                "type": dtype,
                "desc": desc_map.get(name, ""),
            })
    except Exception:  # noqa: BLE001
        # 视图不存在(本地无数据)，用静态字段定义兜底
        if desc_map:
            for name, desc in desc_map.items():
                fields.append({"name": name, "type": "—", "desc": desc})
    return fields


@router.get("/version")
def get_version(request: Request) -> dict:
    """返回当前项目版本号。

    优先读 app.__version__ (与 /health 接口同源, 唯一权威版本),
    回退到项目根 VERSION 文件, 最后兜底 v0.0.0。
    """
    from app import __version__

    # 1. 优先用 app.__version__ (唯一权威版本, 运行时从 frontend/package.json 加载)
    if __version__:
        v = __version__.strip()
        return {"version": v if v.startswith("v") else f"v{v}"}

    # 2. 回退到项目根 VERSION 文件
    from app.config import settings
    project_root = Path(settings.data_dir).parent
    version_file = project_root / "VERSION"
    if version_file.exists():
        v = version_file.read_text(encoding="utf-8").strip()
        if v:
            return {"version": v}

    return {"version": "v0.0.0"}


@router.post("/refresh-cache")
def refresh_cache(request: Request) -> dict:
    """重建 Polars 内存缓存 (clear + refresh), 不清数据、不清 alerts。

    用于跨天后缓存残留、或维表更新后手动刷新场景。
    与设置页「清理并刷新」的区别: 那个清前端 react-query, 这个重建后端 enriched 缓存。
    """
    repo = request.app.state.repo
    repo.clear_cache()
    repo.refresh_cache()
    # 清除 Overview 总览聚合结果缓存 + Screener 历史 TTL 缓存
    from app.api.overview import invalidate_overview_cache
    invalidate_overview_cache()
    from app.services.screener import ScreenerService
    ScreenerService.clear_history_cache()
    logger.info("refresh-cache: Polars 缓存已重建")
    return {"ok": True}
