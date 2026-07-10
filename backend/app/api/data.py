"""数据画像 API —— 让前端知道"我们本地有什么数据"。"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Request

from app.indicators.pipeline import ENRICHED_COLUMNS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data", tags=["data"])

# ===== 缓存:storage(文件扫描) + 每张表 aggregate 各自缓存 =====
# 同步期间前端 2s 轮一次 status,每张表 aggregate 全表 count + min/max + distinct
# 太重,加 TTL + 事件失效。stage 写完只清对应那张表的缓存。

_TABLE_TTL = 30.0  # 兜底 TTL,即使没人调 invalidate 也会过期
_TABLE_TTL_LARGE = 120.0  # 大表(分钟K等)单独 TTL，避免多分区聚合反复重算
_STORAGE_TTL = 60.0  # storage 文件扫描独立 TTL,stage 写完不触发重算

# 聚合慢的大表（分区数多、行数多），使用更长的 TTL
_LARGE_TABLES = {"minute"}

_storage_cache: dict[str, Any] | None = None
_storage_cache_ts: float = 0.0
_storage_lock = threading.Lock()

_table_cache: dict[str, dict | None] = {
    "daily": None,
    "enriched": None,
    "index_daily": None,
    "index_enriched": None,
    "index_instruments": None,
    "etf_daily": None,
    "etf_enriched": None,
    "etf_instruments": None,
    "minute": None,
    "adj_factor": None,
    "instruments": None,
    "financials": None,
}
_table_cache_ts: dict[str, float] = {k: 0.0 for k in _table_cache}
_table_cache_lock = threading.Lock()

_last_finished_cache: dict[str, str | None] | None = None
_last_finished_lock = threading.Lock()


def invalidate_data_cache(table: str | None = None) -> None:
    """数据写入/清除后调用。

    table=None 时清所有表 cache + storage(粗粒度,用于 pipeline 完成/clear);
    指定 table 时只清那张表,不影响 storage(细粒度,用于单 stage 写完)。
    """
    with _table_cache_lock:
        if table is None:
            global _storage_cache, _storage_cache_ts, _last_finished_cache
            _storage_cache = None
            _storage_cache_ts = 0.0
            _last_finished_cache = None
            for k in _table_cache:
                _table_cache[k] = None
                _table_cache_ts[k] = 0.0
        elif table in _table_cache:
            _table_cache[table] = None
            _table_cache_ts[table] = 0.0


def invalidate_storage_cache() -> None:
    """向后兼容入口 — 清全部缓存。新代码请用 invalidate_data_cache(table)。"""
    invalidate_data_cache(None)


def _get_table_stats(name: str, fetch: Callable[[], dict | None]) -> dict | None:
    """走 TTL+事件 双重缓存。fetch 在锁外执行避免阻塞别的请求。"""
    ttl = _TABLE_TTL_LARGE if name in _LARGE_TABLES else _TABLE_TTL
    now = time.time()
    with _table_cache_lock:
        cached = _table_cache.get(name)
        cached_ts = _table_cache_ts.get(name, 0.0)
        if cached is not None and (now - cached_ts) < ttl:
            return cached

    fresh = fetch()

    with _table_cache_lock:
        _table_cache[name] = fresh
        _table_cache_ts[name] = now
    return fresh


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

    for table in ("metrics", "income", "balance_sheet", "cash_flow"):
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

    # total: 再加上其他零散文件(pools, financials, capabilities.json 等)
    other_dirs = ["pools", "financials", "backtest_results", "screener_results", "ai_cache"]
    for name in other_dirs:
        d = data_dir / name
        if d.exists():
            _, s = _scan_dir_stats(d)
            total_size += s

    # financials 单独统计
    fin_dir = data_dir / "financials"
    if fin_dir.exists():
        fc, sz = _scan_dir_stats(fin_dir)
        stats["financials_files"] = fc
        stats["financials_size_mb"] = sz
        total_size += sz
    for name in other_dirs:
        d = data_dir / name
        if d.exists():
            _, s = _scan_dir_stats(d)
            total_size += s
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


def _get_storage(data_dir: Path) -> dict:
    """返回缓存的 storage 统计；走独立 TTL，stage 写完不触发重算。"""
    global _storage_cache, _storage_cache_ts
    now = time.time()
    with _storage_lock:
        if _storage_cache is not None and (now - _storage_cache_ts) < _STORAGE_TTL:
            return _storage_cache
    fresh = _compute_storage(data_dir)
    with _storage_lock:
        _storage_cache = fresh
        _storage_cache_ts = now
    return fresh


def _last_finished(job_label: str) -> str | None:
    """从 JobStore 读最近一次该类型任务的完成时间（缓存到 pipeline 终态失效）。"""
    global _last_finished_cache
    with _last_finished_lock:
        if _last_finished_cache is not None:
            return _last_finished_cache.get(job_label)

    from app.services.pipeline_jobs import job_store
    jobs = job_store.list_recent(limit=50)
    cache: dict[str, str | None] = {}
    for j in jobs:
        if j["status"] not in ("succeeded", "failed"):
            continue
        if "instruments_rows" in (j.get("result") or {}) and "instruments" not in cache:
            cache["instruments"] = j["finished_at"]
        if "daily_days" in (j.get("result") or {}) and "pipeline" not in cache:
            cache["pipeline"] = j["finished_at"]
    with _last_finished_lock:
        _last_finished_cache = cache
    return cache.get(job_label)


@router.get("/status")
def status(request: Request) -> dict:
    repo = request.app.state.repo
    scheduler = getattr(request.app.state, "scheduler", None)
    data_dir = repo.store.data_dir

    return {
        "daily":       _get_table_stats("daily",       lambda: _safe_aggregate_daily(repo)),
        "enriched":    _get_table_stats("enriched",    lambda: _safe_aggregate_enriched(repo)),
    "index_daily":       _get_table_stats("index_daily",       lambda: _safe_aggregate_index_daily(repo)),
    "index_enriched":    _get_table_stats("index_enriched",    lambda: _safe_aggregate_index_enriched(repo)),
    "index_instruments": _get_table_stats("index_instruments", lambda: _safe_aggregate_index_instruments(repo)),
    "etf_daily":         _get_table_stats("etf_daily",         lambda: _safe_aggregate_etf_daily(repo)),
    "etf_enriched":      _get_table_stats("etf_enriched",      lambda: _safe_aggregate_etf_enriched(repo)),
    "etf_instruments":   _get_table_stats("etf_instruments",   lambda: _safe_aggregate_etf_instruments(repo)),
    "minute":      _get_table_stats("minute",      lambda: _safe_aggregate_minute(repo)),
        "adj_factor":  _get_table_stats("adj_factor",  lambda: _safe_aggregate_adj_factor(repo)),
        "instruments": _get_table_stats("instruments", lambda: _safe_aggregate_instruments(repo)),
        "financials":  _get_table_stats("financials",  lambda: _safe_aggregate_financials(repo)),

        # 文件层面信息(缓存)
        "storage": _get_storage(data_dir),

        # 调度
        "next_instruments_run": _next_cron_run(scheduler, "pre_market_instruments"),
        "next_pipeline_run":    _next_cron_run(scheduler, "daily_pipeline"),
        "last_instruments_run": _last_finished("instruments"),
        "last_pipeline_run":    _last_finished("pipeline"),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        # 指标缓存就绪标志 (启动时 enriched 异步预热, 完成前为 false)
        "indicators_ready": getattr(request.app.state, "indicators_ready", True),
    }


@router.post("/clear")
def clear_data(request: Request):
    """清除所有本地 Parquet 数据（保留 capabilities.json 和目录结构）。"""
    import shutil

    repo = request.app.state.repo
    data_dir = repo.store.data_dir
    deleted = 0

    for sub in (
        "kline_daily", "kline_daily_enriched", "kline_index_daily", "kline_index_enriched",
        "kline_etf_daily", "kline_etf_enriched", "kline_etf_minute", "kline_minute",
        "adj_factor", "adj_factor_etf", "instruments", "instruments_index", "instruments_etf", "pools", "financials",
        "backtest_results", "screener_results", "ai_cache",
    ):
        d = data_dir / sub
        if d.exists():
            # 先删所有 parquet 文件
            for f in d.rglob("*.parquet"):
                f.unlink()
                deleted += 1
            # 再删除空的日期分区子目录（date=YYYY-MM-DD 等）
            for child in list(d.iterdir()):
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)

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
    invalidate_data_cache(None)
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
        "timestamp": "除权除息时间戳(ms)",
        "trade_date": "除权除息日",
        "ex_factor": "复权因子",
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

    # 1. 优先用 app.__version__ (唯一权威版本, 打包期由 PyInstaller 注入)
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
