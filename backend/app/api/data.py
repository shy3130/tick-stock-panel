"""数据画像 API —— 让前端知道"我们本地有什么数据"。"""

from __future__ import annotations

from collections.abc import Callable
import logging
import os
import re
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.indicators.pipeline import ENRICHED_COLUMNS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data", tags=["data"])

# ===== 缓存：storage 文件统计 + 各数据域轻量元数据 =====
# 首个请求在锁内完成一次采样，后续请求复用 TTL 缓存，避免并发轮询形成查询风暴。

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
    "hk_daily": None,
    "hk_enriched": None,
    "hk_instruments": None,
    "minute": None,
    "adj_factor": None,
    "instruments": None,
    "financials": None,
}
_table_cache_ts: dict[str, float] = {k: 0.0 for k in _table_cache}
_table_cache_lock = threading.Lock()

_last_finished_cache: dict[str, str | None] | None = None
_last_pipeline_cache: dict[str, Any] | None = None  # {"job": ...}; 空 dict = 已计算但无终态管道
_last_finished_lock = threading.Lock()


def invalidate_job_status_cache() -> None:
    """终态 job 写入后失效最近完成时间与管道状态缓存。"""
    global _last_finished_cache, _last_pipeline_cache
    with _last_finished_lock:
        _last_finished_cache = None
        _last_pipeline_cache = None


def invalidate_data_cache(table: str | None = None) -> None:
    """数据写入/清除后调用。

    table=None 时清所有表 cache + storage(粗粒度,用于 pipeline 完成/clear);
    指定 table 时只清那张表,不影响 storage(细粒度,用于单 stage 写完)。
    """
    with _table_cache_lock:
        if table is None:
            global _storage_cache, _storage_cache_ts
            _storage_cache = None
            _storage_cache_ts = 0.0
            for k in _table_cache:
                _table_cache[k] = None
                _table_cache_ts[k] = 0.0
        elif table in _table_cache:
            _table_cache[table] = None
            _table_cache_ts[table] = 0.0
    if table is None:
        invalidate_job_status_cache()


def invalidate_storage_cache() -> None:
    """向后兼容入口 — 清全部缓存。新代码请用 invalidate_data_cache(table)。"""
    invalidate_data_cache(None)


def _get_table_stats(name: str, fetch: Callable[[], dict | None]) -> dict | None:
    """读取带 TTL 的单飞缓存；无数据的 None 结果同样缓存。"""
    ttl = _TABLE_TTL_LARGE if name in _LARGE_TABLES else _TABLE_TTL
    with _table_cache_lock:
        cached_ts = _table_cache_ts.get(name, 0.0)
        if cached_ts > 0 and (time.time() - cached_ts) < ttl:
            return _table_cache.get(name)

        fresh = fetch()
        _table_cache[name] = fresh
        _table_cache_ts[name] = time.time()
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


_PARTITION_DATE_RE = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")


def _iso_partition_dates(data_dir: Path, max_date: date | None = None) -> list[str]:
    """列出严格 ISO 日期分区名（排序后）；只读目录名，不碰任何数据行。"""
    if not data_dir.exists():
        return []
    dates: list[str] = []
    for entry in data_dir.iterdir():
        if not entry.is_dir():
            continue
        match = _PARTITION_DATE_RE.fullmatch(entry.name)
        if not match:
            continue
        try:
            parsed = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if max_date is not None and parsed > max_date:
            continue
        dates.append(parsed.isoformat())
    dates.sort()
    return dates


def _partition_date_stats(
    repo,
    directory: str,
    instruments_table: str | None,
    *,
    schema_view: str | None = None,
    max_date: date | None = None,
) -> dict | None:
    """从严格 ISO 日期分区、标的小表和 schema 获取轻量统计。"""
    data_dir = repo.store.data_dir / directory
    dates = _iso_partition_dates(data_dir, max_date)
    if not dates:
        return None
    result = {
        "rows": 0,
        "row_count_exact": False,
        "earliest_date": dates[0],
        "latest_date": dates[-1],
        "symbols_covered": _count_instruments_symbols(repo, instruments_table)
        if instruments_table
        else 0,
        "trading_days": len(dates),
    }
    if schema_view is not None:
        latest_dir = data_dir / f"date={dates[-1]}"
        parquet = next(latest_dir.glob("*.parquet"), None)
        if parquet is not None:
            try:
                result["fields"] = len(pl.read_parquet_schema(parquet))
            except Exception:
                result["fields"] = 0
        else:
            result["fields"] = 0
    return result


def _parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _canonical_history_manifest() -> dict[str, Any] | None:
    """只读已发布 canonical 历史 manifest（current.json），不扫描任何数据行。

    返回键与前端契约对齐：generation/earliest_date/latest_date/rows/symbols/
    trading_days。manifest 缺失或日期不可解析时返回 None（保持纯本地统计）。
    """
    try:
        from app.services.canonical_history import resolve_published_history

        published = resolve_published_history()
    except Exception:  # noqa: BLE001
        return None
    if not published:
        return None
    manifest, _ = published
    earliest = _parse_iso_date(manifest.get("start_date"))
    latest = _parse_iso_date(manifest.get("end_date"))
    if earliest is None or latest is None:
        return None
    try:
        return {
            "generation": manifest.get("generation"),
            "earliest_date": earliest.isoformat(),
            "latest_date": latest.isoformat(),
            "rows": int(manifest.get("rows") or 0),
            "symbols": int(manifest.get("symbols") or 0),
            "trading_days": int(manifest.get("trading_days") or 0),
        }
    except (TypeError, ValueError):
        return None


def _latest_partition_symbols(repo, directory: str, latest: str | None) -> int | None:
    """只读最新本地分区的 symbol 列，精确计算该分区标的数（不做全历史扫描）。"""
    if latest is None:
        return None
    partition = repo.store.data_dir / directory / f"date={latest}"
    try:
        parquet_files = list(partition.glob("*.parquet"))
    except OSError:
        return None
    if not parquet_files:
        return None
    try:
        return int(
            pl.scan_parquet(parquet_files)
            .select(pl.col("symbol").cast(pl.Utf8).n_unique())
            .collect()
            .item()
        )
    except Exception:  # noqa: BLE001
        logger.debug("latest partition symbol count failed: %s", partition, exc_info=True)
        return None


_SHANGHAI_TZ = timezone(timedelta(hours=8))
_FRESHNESS_PUBLISH_TIME = dtime(15, 30)  # 盘后管道默认 15:30 后发布当日数据


def _expected_reference_date(now: datetime) -> date:
    """返回最近一个应已收盘的交易日（周一~周五；无节假日日历，保守推断）。

    周末一律回退到周五，不得把周六/周日误报为缺数据；未到当日发布时间
    （15:30）时以前一交易日为期望基准。
    """
    local = now.astimezone(_SHANGHAI_TZ)
    day = local.date()
    if local.time() < _FRESHNESS_PUBLISH_TIME:
        day -= timedelta(days=1)
    while day.weekday() >= 5:  # 5=周六, 6=周日
        day -= timedelta(days=1)
    return day


def _daily_watermark(repo) -> date | None:
    """返回 provider 已确认的最新可读交易日水位（本地元数据读取，不联网）。

    fquant_local 模式读取 provider freshness（TDX/fstore 本地快照水位，
    与管道 bootstrap 同源）。不可用时返回 None，由 freshness 逻辑按交易日历
    保守推断；canonical ceiling 是本地发布水位，不能冒充 provider 水位。
    """
    try:
        from app.services.data_mode import is_local_daily_mode

        if is_local_daily_mode():
            from app.jobs.daily_pipeline import _provider_freshness_date

            value = _provider_freshness_date()
            if isinstance(value, date):
                return value
    except Exception:  # noqa: BLE001
        logger.debug("provider watermark lookup failed", exc_info=True)
    return None


def _daily_freshness(
    latest: date | None,
    watermark: date | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """基于当前时间与 latest 的保守、可解释新鲜度判定；不做任何网络请求。

    provider 水位可得时优先对齐 provider（区分“本地与 provider 对齐”与
    “等待上游发布”）；否则按周一~周五日历保守推断（节假日无日历，可能偏严）。
    """
    current = now or datetime.now(timezone.utc)
    reference = _expected_reference_date(current)
    if latest is None:
        return {
            "status": "unknown",
            "age_days": None,
            "reference_date": reference.isoformat(),
            "reason": "本地无已发布数据，无法判断新鲜度",
        }
    if watermark is not None:
        if latest < watermark:
            age = (watermark - latest).days
            return {
                "status": "awaiting_publish",
                "age_days": age,
                "reference_date": watermark.isoformat(),
                "reason": (
                    f"本地落后 provider 水位 {age} 天"
                    f"（provider {watermark.isoformat()} > 本地 {latest.isoformat()}），等待盘后管道发布"
                ),
            }
        if latest < reference and watermark < reference:
            age = (reference - latest).days
            return {
                "status": "awaiting_publish",
                "age_days": age,
                "reference_date": reference.isoformat(),
                "reason": (
                    f"本地已与 provider 水位 {watermark.isoformat()} 对齐，"
                    f"但上游快照尚未发布最近收盘日 {reference.isoformat()}"
                ),
            }
        return {
            "status": "current",
            "age_days": 0,
            "reference_date": max(watermark, reference).isoformat(),
            "reason": f"本地与 provider 水位对齐（provider 最新 {watermark.isoformat()}）",
        }
    if latest >= reference:
        return {
            "status": "current",
            "age_days": 0,
            "reference_date": reference.isoformat(),
            "reason": f"已覆盖最近收盘交易日 {latest.isoformat()}（按周一~周五日历推断）",
        }
    age = (reference - latest).days
    return {
        "status": "awaiting_publish",
        "age_days": age,
        "reference_date": reference.isoformat(),
        "reason": (
            f"落后最近收盘交易日 {age} 天（provider 水位不可读，按日历推断，法定节假日可能偏严）"
        ),
    }


def _merge_canonical_daily_stats(repo, local: dict | None) -> dict | None:
    """把已发布 canonical 全历史与本地 enriched overlay 合并为可查询范围统计。

    只读 canonical manifest（current.json）、本地分区目录名与最新分区
    symbol 列，绝不扫描全历史行（canonical 可达千万行）。earliest/latest/
    trading_days/symbols_covered 表示可查询范围（canonical ∪ 本地 overlay，
    与 repository._scan_merged_enriched 读取口径一致：受 read ceiling 夹逼）；
    rows 使用 canonical manifest 行数作为已知下界，row_count_exact=False。
    manifest 缺失时保持纯本地行为，仅补充轻量附加字段。
    """
    canonical = _canonical_history_manifest()
    universe = _count_instruments_symbols(repo, "instruments")

    if canonical is None:
        if local is None:
            return None
        stats = dict(local)
        stats["universe_symbols"] = universe
        stats["latest_partition_symbols"] = _latest_partition_symbols(
            repo, "kline_daily_enriched", local.get("latest_date")
        )
        stats["freshness"] = _daily_freshness(
            _parse_iso_date(local.get("latest_date")), _daily_watermark(repo)
        )
        stats["storage_mode"] = "persisted"
        stats["status_message"] = (
            "未发布 canonical 历史；统计仅基于本地 enriched 分区，"
            "rows 未精确统计（0 表示未统计，非无数据）"
        )
        return stats

    ceiling = getattr(repo, "enriched_read_ceiling", None)
    canonical_earliest = _parse_iso_date(canonical["earliest_date"])
    canonical_latest = _parse_iso_date(canonical["latest_date"])
    if ceiling is not None:
        # manifest end_date 可领先 read ceiling（盘中截断的残缺最新日），
        # 可查询范围与读取层一致地按水位夹逼。
        canonical_latest = min(canonical_latest, ceiling)

    local_dates = _iso_partition_dates(repo.store.data_dir / "kline_daily_enriched", ceiling)
    local_earliest = local_dates[0] if local_dates else None
    local_latest = local_dates[-1] if local_dates else None

    earliest = canonical_earliest.isoformat()
    if local_earliest and local_earliest < earliest:
        earliest = local_earliest
    latest_iso = canonical_latest.isoformat()
    if local_latest and local_latest > latest_iso:
        latest_iso = local_latest

    # canonical 覆盖窗口内的本地分区已在 manifest trading_days 中计数，
    # 只把窗口外的本地日期计入增量交易日。
    extra_days = sum(
        1
        for value in local_dates
        if value < canonical["earliest_date"] or value > canonical["latest_date"]
    )

    stats = dict(local) if local is not None else {}
    stats.update(
        {
            "rows": canonical["rows"],  # 已知下界，未含本地 overlay 增量
            "row_count_exact": False,
            "earliest_date": earliest,
            "latest_date": latest_iso,
            "symbols_covered": canonical["symbols"],  # manifest 为权威历史统计
            "trading_days": canonical["trading_days"] + extra_days,
            "universe_symbols": universe,
            "canonical_history": dict(canonical),
            "freshness": _daily_freshness(_parse_iso_date(latest_iso), _daily_watermark(repo)),
            "storage_mode": "persisted",
        }
    )
    if local_dates:
        stats["local_overlay"] = {
            "earliest_date": local_earliest,
            "latest_date": local_latest,
            "trading_days": len(local_dates),
        }
        stats["latest_partition_symbols"] = _latest_partition_symbols(
            repo, "kline_daily_enriched", local_latest
        )
        overlay_span = f"本地 overlay {local_earliest}~{local_latest}（{len(local_dates)} 天）"
    else:
        overlay_span = "本地无 overlay 分区"
    stats["status_message"] = (
        f"可查询范围 = canonical 全历史 {canonical['earliest_date']}~{canonical['latest_date']}"
        f" ∪ {overlay_span}；rows 为 canonical 已发布 {canonical['rows']} 行下界"
        "（未含本地增量，非精确值）"
    )
    return stats


def _safe_aggregate_daily(repo) -> dict | None:
    """日 K 轻量统计；本地模式始终以 canonical enriched 为准。"""
    try:
        from app.services.data_mode import is_local_daily_mode

        if is_local_daily_mode():
            return _safe_aggregate_local_daily(repo)
    except Exception:  # noqa: BLE001
        pass
    return _partition_date_stats(repo, "kline_daily", "instruments")


def _safe_aggregate_local_daily(repo) -> dict | None:
    """fquant_local 禁写 raw mirror 时，用 enriched 分区表示日线可用性。"""
    stats = _partition_date_stats(
        repo,
        "kline_daily_enriched",
        "instruments",
        max_date=getattr(repo, "enriched_read_ceiling", None),
    )
    if stats is not None:
        stats["source"] = "fquant_local_enriched"
        stats["raw_mirror_disabled"] = True
    return _merge_canonical_daily_stats(repo, stats)


def _safe_aggregate_enriched(repo) -> dict | None:
    """Enriched 轻量统计；字段从最新分区的 Parquet schema 获取。

    与读取层 (_scan_merged_enriched) 口径一致：canonical 全历史 + 本地
    overlay 合并表示可查询范围，不因本地 overlay 起点 (如 2024-10-09)
    掩盖已发布的全历史。
    """
    stats = _partition_date_stats(
        repo,
        "kline_daily_enriched",
        "instruments",
        schema_view="kline_enriched",
        max_date=getattr(repo, "enriched_read_ceiling", None),
    )
    return _merge_canonical_daily_stats(repo, stats)


def _instruments_frame(repo, table: str):
    getters = {
        "instruments": "get_instruments",
        "instruments_index": "get_index_instruments",
        "instruments_etf": "get_etf_instruments",
        "instruments_hk": "get_hk_instruments",
    }
    getter = getattr(repo, getters.get(table, ""), None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def _count_instruments_symbols(repo, table: str = "instruments") -> int:
    """优先读已预热的 instruments 内存表，避免与后台 DuckDB 写任务争锁。"""
    frame = _instruments_frame(repo, table)
    if frame is not None and not frame.is_empty() and "symbol" in frame.columns:
        return frame.get_column("symbol").n_unique()
    try:
        sym_row = repo.execute_one(f"SELECT count(DISTINCT symbol) FROM {table}")
        if sym_row and sym_row[0]:
            return int(sym_row[0])
    except Exception:
        pass
    return 0


def _instrument_stats(repo, table: str) -> dict | None:
    frame = _instruments_frame(repo, table)
    if frame is None or frame.is_empty() or "symbol" not in frame.columns:
        return None
    names = frame.get_column("name").to_list() if "name" in frame.columns else []
    latest_as_of = frame.get_column("as_of").max() if "as_of" in frame.columns else None
    return {
        "rows": frame.height,
        "symbols_covered": frame.get_column("symbol").n_unique(),
        "latest_as_of": str(latest_as_of) if latest_as_of else None,
        "named": sum(1 for value in names if value is not None and str(value)),
    }


def _safe_aggregate_instruments(repo) -> dict | None:
    """instruments 统计；优先使用启动时已预热的 Polars 缓存。"""
    return _instrument_stats(repo, "instruments")


def _safe_aggregate_index_daily(repo) -> dict | None:
    return _partition_date_stats(repo, "kline_index_daily", "instruments_index")


def _safe_aggregate_index_enriched(repo) -> dict | None:
    return _partition_date_stats(
        repo,
        "kline_index_enriched",
        "instruments_index",
        schema_view="kline_index_enriched",
    )


def _safe_aggregate_index_instruments(repo) -> dict | None:
    """指数 instruments 统计；不占用共享 DuckDB 查询锁。"""
    return _instrument_stats(repo, "instruments_index")


def _safe_aggregate_etf_instruments(repo) -> dict | None:
    """ETF instruments 统计；getter 已兼容旧 instruments_index 数据。"""
    return _instrument_stats(repo, "instruments_etf")


def _safe_aggregate_etf_enriched(repo) -> dict | None:
    """ETF enriched 统计；新独立目录优先，为空时只读回退旧 kline_index_enriched。

    历史契约（见 repository.get_etf_daily / _refresh_etf_instruments）：旧版 ETF 曾与
    指数混存于 kline_index_enriched。未迁移老用户在新独立目录为空时走此只读回退，
    不做任何写迁移。兼容统计仍受 provider-confirmed read ceiling 约束，不计入未来/
    未确认分区；新独立 kline_etf_enriched 存在且有数据时永远优先。
    """
    stats = _partition_date_stats(
        repo,
        "kline_etf_enriched",
        "instruments_etf",
        schema_view="kline_etf_enriched",
    )
    if stats is not None:
        return stats
    return _partition_date_stats(
        repo,
        "kline_index_enriched",
        "instruments_etf",
        schema_view="kline_index_enriched",
        max_date=getattr(repo, "enriched_read_ceiling", None),
    )


def _safe_aggregate_etf_daily(repo) -> dict | None:
    """ETF 日K统计；新独立目录优先，为空时只读回退旧 kline_index_daily。

    旧版 ETF 日K曾与指数混存于 kline_index_daily。未迁移老用户在新独立目录为空时
    走此只读回退，不做写迁移。兼容统计仍受 provider-confirmed read ceiling 约束，
    不计入未来/未确认分区；新独立 kline_etf_daily 存在且有数据时永远优先。
    """
    stats = _partition_date_stats(repo, "kline_etf_daily", "instruments_etf")
    if stats is not None:
        return stats
    return _partition_date_stats(
        repo,
        "kline_index_daily",
        "instruments_etf",
        max_date=getattr(repo, "enriched_read_ceiling", None),
    )


def _safe_aggregate_hk_instruments(repo) -> dict | None:
    """港股 instruments 统计；不占用共享 DuckDB 查询锁。"""
    return _instrument_stats(repo, "instruments_hk")


def _safe_aggregate_hk_enriched(repo) -> dict | None:
    return _partition_date_stats(
        repo,
        "kline_hk_enriched",
        "instruments_hk",
        schema_view="kline_hk_enriched",
    )


def _safe_aggregate_hk_daily(repo) -> dict | None:
    return _partition_date_stats(repo, "kline_hk_daily", "instruments_hk")


def _single_parquet_stats(
    repo,
    directory: str,
    *,
    date_column: str,
) -> dict | None:
    """Return exact lightweight coverage for a single-file Parquet dataset."""
    path = repo.store.data_dir / directory / "all.parquet"
    if not path.is_file():
        return None
    try:
        row = (
            pl.scan_parquet(path)
            .select(
                pl.len().alias("rows"),
                pl.col(date_column).min().alias("earliest_date"),
                pl.col(date_column).max().alias("latest_date"),
                pl.col("symbol").n_unique().alias("symbols_covered"),
                pl.col(date_column).n_unique().alias("trading_days"),
            )
            .collect()
            .row(0, named=True)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("single parquet stats failed for %s: %s", directory, exc)
        return None
    if not row["rows"]:
        return None
    return {
        "rows": int(row["rows"]),
        "row_count_exact": True,
        "earliest_date": str(row["earliest_date"]) if row["earliest_date"] is not None else None,
        "latest_date": str(row["latest_date"]) if row["latest_date"] is not None else None,
        "symbols_covered": int(row["symbols_covered"] or 0),
        "trading_days": int(row["trading_days"] or 0),
    }


def _adj_factor_on_demand_stats() -> dict | None:
    """本地无 adj_factor 镜像文件时，检查 provider 是否按需供给复权因子。

    fquant_local 模式不落复权因子本地镜像，enriched 计算时由 provider
    按需提供 —— 无文件不等于无数据，不得误报为零数据。capability 声明
    读取失败时 fail-soft 返回 None（沿用本地无数据语义）。
    """
    try:
        from app.data_providers.registry import get_active_provider_name, get_provider
        from app.services.data_mode import is_local_daily_mode

        if not is_local_daily_mode():
            return None
        provider = get_provider(get_active_provider_name("adj_factor"))
        caps = getattr(provider, "capabilities", None)
        if not getattr(caps, "adj_factor", False):
            return None
    except Exception:  # noqa: BLE001
        logger.debug("adj_factor on-demand capability check failed", exc_info=True)
        return None
    return {
        "rows": 0,
        "row_count_exact": False,
        "earliest_date": None,
        "latest_date": None,
        "symbols_covered": 0,
        "trading_days": 0,
        "available": True,
        "storage_mode": "provider_on_demand",
        "status_message": (
            "本地无复权因子镜像；fquant_local 模式由 active provider 在 enriched "
            "计算时按需提供，rows=0 仅表示未本地物化，并非无数据"
        ),
    }


def _safe_aggregate_adj_factor(repo) -> dict | None:
    """复权因子使用单文件存储，按实际 ``all.parquet`` 精确统计。

    文件不存在时不把"无镜像"误报为零数据：active provider 声明 adj_factor
    能力且处于本地 fquant 模式时，标记为 provider_on_demand 供给。
    """
    stats = _single_parquet_stats(repo, "adj_factor", date_column="trade_date")
    if stats is not None:
        stats["available"] = True
        stats["storage_mode"] = "persisted"
        return stats
    return _adj_factor_on_demand_stats()


def _safe_aggregate_minute(repo) -> dict | None:
    """分钟 K 状态：本地缓存优先，否则展示 active provider 的发布水位。"""
    local = _partition_date_stats(repo, "kline_minute", None)
    if local is not None:
        local.update({"available": True, "source": "local_cache"})
        return local

    try:
        from app.data_providers.registry import get_active_provider_name, get_provider

        provider_name = get_active_provider_name("minute")
        provider = get_provider(provider_name)
        get_coverage = getattr(provider, "get_minute_coverage", None)
        if not callable(get_coverage):
            return None
        coverage = get_coverage()
    except Exception as exc:  # noqa: BLE001
        logger.debug("minute provider coverage unavailable: %s", exc)
        return None
    if not coverage:
        return None
    return {
        "rows": 0,
        "row_count_exact": False,
        "earliest_date": None,
        "latest_date": coverage["latest_date"],
        "symbols_covered": 0,
        "trading_days": 0,
        "available": True,
        "source": "catalog_tdx_minutes",
        "stage": coverage.get("stage"),
        "generation": coverage.get("generation"),
        "logical": coverage.get("logical"),
    }


# 财务表日期列候选：provider 归一化输出首列 t_date，旧快照可能只有
# report_date/notice_date/update_date —— 按 schema 自适应选第一个存在的。
_FINANCIAL_DATE_COLUMNS = ("t_date", "report_date", "notice_date", "update_date")


def _date_value_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return text[:10]


def _financial_table_stats(path: Path) -> dict:
    """单表轻量统计：行数/标的数 + 实际日期列的 earliest/latest。

    惰性扫描 + 列裁剪，只读 symbol 与命中的日期列，不加载其余列。
    """
    try:
        frame = pl.scan_parquet(path)
        names = frame.collect_schema().names()
        date_column = next((column for column in _FINANCIAL_DATE_COLUMNS if column in names), None)
        expressions = [
            pl.len().alias("rows"),
            pl.col("symbol").n_unique().alias("symbols"),
        ]
        if date_column is not None:
            expressions.append(pl.col(date_column).min().alias("earliest_date"))
            expressions.append(pl.col(date_column).max().alias("latest_date"))
        row = frame.select(expressions).collect().row(0, named=True)
        stats = {"rows": int(row["rows"]), "symbols": int(row["symbols"] or 0)}
        if date_column is not None:
            stats["earliest_date"] = _date_value_str(row.get("earliest_date"))
            stats["latest_date"] = _date_value_str(row.get("latest_date"))
        return stats
    except Exception:  # noqa: BLE001
        logger.debug("financial table stats failed: %s", path, exc_info=True)
        return {"rows": 0, "symbols": 0}


def _safe_aggregate_financials(repo) -> dict | None:
    """财务数据统计 — 各表行数/标的数/日期范围（含 quick/forecast）。"""
    from app.services.financial_sync import FINANCIAL_TABLES

    data_dir = repo.store.data_dir
    tables_info: dict[str, dict] = {}
    total_rows = 0

    for table in FINANCIAL_TABLES:
        path = data_dir / "financials" / table / "part.parquet"
        if path.exists():
            stats = _financial_table_stats(path)
        else:
            stats = {"rows": 0, "symbols": 0}
        tables_info[table] = stats
        total_rows += stats["rows"]

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
    """计算各数据域与整个 data 根目录的文件体积。"""
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
        "hk_daily": data_dir / "kline_hk_daily",
        "hk_enriched": data_dir / "kline_hk_enriched",
        "hk_instruments": data_dir / "instruments_hk",
        "minute": data_dir / "kline_minute",
        "adj_factor": data_dir / "adj_factor",
        "instruments": data_dir / "instruments",
        "financials": data_dir / "financials",
        "ext_data": data_dir / "ext_data",
    }
    stats: dict[str, int | float] = {}
    for key, directory in subdirs.items():
        file_count, size_mb = _scan_dir_stats(directory)
        stats[f"{key}_files"] = file_count
        stats[f"{key}_size_mb"] = size_mb

    _, total_size_mb = _scan_dir_stats(data_dir)
    stats["total_size_mb"] = total_size_mb
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
    """返回 storage 单飞缓存，避免页面轮询并发遍历目录。"""
    global _storage_cache, _storage_cache_ts
    with _storage_lock:
        if _storage_cache is not None and (time.time() - _storage_cache_ts) < _STORAGE_TTL:
            return _storage_cache
        fresh = _compute_storage(data_dir)
        _storage_cache = fresh
        _storage_cache_ts = time.time()
        return fresh


# 新任务以 kind 显式标识；旧成功任务仍用 result.daily_days，
# 旧失败任务则用调度失败标记或 pipeline 独有 stage 兼容识别。
_SCHEDULED_PIPELINE_ERROR = "scheduled daily_pipeline failed"
_PIPELINE_ONLY_STAGES = frozenset(
    {
        "resolve_universe",
        "sync_daily",
        "sync_adj",
        "compute_enriched",
        "sync_index",
        "refresh_strategy_cache",
        "refresh_views",
    }
)


def _is_pipeline_terminal_job(job: dict) -> bool:
    """判断一条终态 job 是否为盘后管道（succeeded/degraded/failed）。"""
    if job.get("kind") == "daily_pipeline":
        return True
    result = job.get("result")
    if isinstance(result, dict) and "daily_days" in result:
        return True
    if job.get("status") == "failed":
        if job.get("error") == _SCHEDULED_PIPELINE_ERROR:
            return True
        # 手动触发的失败 job 无 result 标记，按 pipeline 独有 stage 兜底识别
        # （sync_instruments/sync_minute/done 与其他 job 共用，不参与判定）。
        return job.get("stage") in _PIPELINE_ONLY_STAGES
    return False


def _scan_recent_terminal_jobs() -> None:
    """扫描 JobStore 最近任务，刷新 last_finished 标签缓存与最近管道终态。"""
    global _last_finished_cache, _last_pipeline_cache
    from app.services.pipeline_jobs import job_store

    labels: dict[str, str | None] = {}
    pipeline: dict | None = None
    for j in job_store.list_recent(limit=50):
        if j["status"] not in ("succeeded", "degraded", "failed", "cancelled"):
            continue
        if "instruments_rows" in (j.get("result") or {}) and "instruments" not in labels:
            labels["instruments"] = j["finished_at"]
        if "daily_days" in (j.get("result") or {}) and "pipeline" not in labels:
            labels["pipeline"] = j["finished_at"]
        if pipeline is None and _is_pipeline_terminal_job(j):
            pipeline = j
    with _last_finished_lock:
        _last_finished_cache = labels
        _last_pipeline_cache = {"job": pipeline}


def _last_finished(job_label: str) -> str | None:
    """从 JobStore 读最近一次该类型任务的完成时间（缓存到 pipeline 终态失效）。"""
    with _last_finished_lock:
        if _last_finished_cache is not None:
            return _last_finished_cache.get(job_label)
    _scan_recent_terminal_jobs()
    with _last_finished_lock:
        return _last_finished_cache.get(job_label) if _last_finished_cache else None


def _last_pipeline() -> dict[str, Any] | None:
    """最近一次盘后管道终态，携带 degraded/failed 的 failed_stages 与 error。"""
    with _last_finished_lock:
        cached = _last_pipeline_cache
    if cached is None:
        _scan_recent_terminal_jobs()
        with _last_finished_lock:
            cached = _last_pipeline_cache or {}
    job = cached.get("job")
    if not isinstance(job, dict):
        return None
    result = job.get("result")
    failed_stages = result.get("failed_stages") if isinstance(result, dict) else None
    return {
        "status": job.get("status"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "failed_stages": failed_stages or [],
    }


class CanonicalHistoryBackfillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date | None = None
    end_date: date | None = None
    batch_size: int = Field(default=100, ge=1, le=1_000)
    workers: int = Field(default=8, ge=1, le=8)


def _canonical_history_view() -> dict[str, Any]:
    from app.services.canonical_history import canonical_history_manager

    raw = canonical_history_manager().status()
    available = bool(raw.get("available"))
    manifest = raw.get("manifest") if available else None
    job = None
    if raw.get("job_id"):
        job = {
            "id": raw.get("job_id"),
            "status": raw.get("status"),
            "progress_pct": round(float(raw.get("progress", 0)) * 100, 2),
            "processed_symbols": raw.get("symbols_done", 0),
            "total_symbols": raw.get("symbols_total", 0),
            "written_rows": raw.get("rows", 0),
            "started_at": raw.get("started_at"),
            "finished_at": raw.get("finished_at"),
            "error": raw.get("error"),
        }
    published = None
    if isinstance(manifest, dict):
        published = {
            "generation": manifest.get("generation"),
            "created_at": manifest.get("published_at"),
            "earliest_date": manifest.get("start_date"),
            "latest_date": manifest.get("end_date"),
            "row_count": manifest.get("rows", 0),
            "symbols": manifest.get("symbols", 0),
            "trading_days": manifest.get("trading_days", 0),
        }
    return {
        "available": available,
        "reason": None if available else raw.get("reason") or "not_published",
        "published": published,
        "job": job,
    }


@router.get("/canonical-history/status")
def canonical_history_status() -> dict[str, Any]:
    return _canonical_history_view()


@router.post("/canonical-history/backfill", status_code=202)
def canonical_history_backfill(
    body: CanonicalHistoryBackfillRequest | None = None,
) -> dict[str, str]:
    from app.services.canonical_history import canonical_history_manager

    payload = body or CanonicalHistoryBackfillRequest()
    if (
        payload.start_date is not None
        and payload.end_date is not None
        and payload.start_date > payload.end_date
    ):
        raise HTTPException(status_code=422, detail="start_date must not be after end_date")
    try:
        result = canonical_history_manager().start(
            start_date=payload.start_date,
            end_date=payload.end_date,
            batch_size=payload.batch_size,
            workers=payload.workers,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"job_id": str(result["job_id"]), "status": str(result["status"])}


@router.get("/status")
def status(request: Request) -> dict:
    repo = request.app.state.repo
    scheduler = getattr(request.app.state, "scheduler", None)
    data_dir = repo.store.data_dir

    return {
        "daily": _get_table_stats("daily", lambda: _safe_aggregate_daily(repo)),
        "enriched": _get_table_stats("enriched", lambda: _safe_aggregate_enriched(repo)),
        "index_daily": _get_table_stats("index_daily", lambda: _safe_aggregate_index_daily(repo)),
        "index_enriched": _get_table_stats(
            "index_enriched", lambda: _safe_aggregate_index_enriched(repo)
        ),
        "index_instruments": _get_table_stats(
            "index_instruments", lambda: _safe_aggregate_index_instruments(repo)
        ),
        "etf_daily": _get_table_stats("etf_daily", lambda: _safe_aggregate_etf_daily(repo)),
        "etf_enriched": _get_table_stats(
            "etf_enriched", lambda: _safe_aggregate_etf_enriched(repo)
        ),
        "etf_instruments": _get_table_stats(
            "etf_instruments", lambda: _safe_aggregate_etf_instruments(repo)
        ),
        "hk_daily": _get_table_stats("hk_daily", lambda: _safe_aggregate_hk_daily(repo)),
        "hk_enriched": _get_table_stats("hk_enriched", lambda: _safe_aggregate_hk_enriched(repo)),
        "hk_instruments": _get_table_stats(
            "hk_instruments", lambda: _safe_aggregate_hk_instruments(repo)
        ),
        "minute": _get_table_stats("minute", lambda: _safe_aggregate_minute(repo)),
        "adj_factor": _get_table_stats("adj_factor", lambda: _safe_aggregate_adj_factor(repo)),
        "instruments": _get_table_stats("instruments", lambda: _safe_aggregate_instruments(repo)),
        "financials": _get_table_stats("financials", lambda: _safe_aggregate_financials(repo)),
        # 文件层面信息(缓存)
        "storage": _get_storage(data_dir),
        # 调度
        "next_instruments_run": _next_cron_run(scheduler, "pre_market_instruments"),
        "next_pipeline_run": _next_cron_run(scheduler, "daily_pipeline"),
        "last_instruments_run": _last_finished("instruments"),
        "last_pipeline_run": _last_finished("pipeline"),
        "last_pipeline": _last_pipeline(),
        "checked_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }


@router.post("/clear")
def clear_data(request: Request):
    """清除所有本地 Parquet 数据（保留 capabilities.json 和目录结构）。"""
    import shutil

    repo = request.app.state.repo
    data_dir = repo.store.data_dir
    deleted = 0

    for sub in (
        "kline_daily",
        "kline_daily_enriched",
        "kline_index_daily",
        "kline_index_enriched",
        "kline_etf_daily",
        "kline_etf_enriched",
        "kline_etf_minute",
        "kline_minute",
        "adj_factor",
        "adj_factor_etf",
        "instruments",
        "instruments_index",
        "instruments_etf",
        "pools",
        "financials",
        "backtest_results",
        "screener_results",
        "ai_cache",
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
        with qs._lock:
            qs._pending_alerts.clear()

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

    # 清除复盘分区聚合缓存 (5min TTL) —— 否则清数据后复盘页仍显示旧的情绪/天梯序列
    from app.services.review_series import invalidate_review_cache

    invalidate_review_cache()

    # 刷新 DuckDB 视图（空 parquet 目录也需要重新挂载）
    d = data_dir.as_posix()
    for name, path in {
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
    }.items():
        try:
            repo.db.execute(
                f"CREATE OR REPLACE VIEW {name} AS "
                f"SELECT * FROM read_parquet('{path}', union_by_name=true)"
            )
        except Exception:
            pass

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
            fields.append(
                {
                    "name": name,
                    "type": dtype,
                    "desc": desc_map.get(name, ""),
                }
            )
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
