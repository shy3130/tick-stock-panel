"""历史交易日数据完整性检测 — 停机缺口 / 盘中快照判别。

场景: 用户盘中停机后, 次日再启动并开实时行情, 实时 flush 写出"今天"分区后,
盘后管道的「今天已有数据 → 只刷今天」分支会让停机日的盘中快照永久留存
(close=停机时刻价, volume=半日累计, 技术指标全错且污染后续 lookback 类指标)。

判据 (quote_ts 列, 毫秒 Unix 时间戳, 仅实时 flush 写入真实值):
- null              → batch 拉取 / 盘后计算写入的权威历史 → 完整
- d < 今天 且 时刻 < d 15:30 → 实时写入 (盘中快照或 close_final 定版) → 坏
- d < 今天 且 时刻 ≥ d 15:30 → 完整 (仅理论可能: 盘后管道 15:35 前的极晚写入)
- batch 权威行中仅夹杂少量零成交实时行 → 停牌残留 → 忽略
- d == 今天         → 实时更新中, 属正常, 不校验
- 分区缺失的候选日  → 缺口 (配置了 fuyao apikey → 按真实交易日检测;
  未配置 → 直接降级工作日近似; 节假日误报对修复只是空拉取, 但对
  realtime_gate 是 409 死锁 — 休市日永远无数据, 门禁循环放行不了)

检测成本: 每分区只读 parquet 元数据 statistics (不解压数据页), 实测 ~0.5ms/分区。
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ

logger = logging.getLogger(__name__)

# 尾盘定版线: 实时侧写入的分区一律不算定版 — 15:00 后的 close_final 定版也不
# 可信 (实测有源"时间戳翻新但价格停在竞价前旧价"), 且 15:30 前科创/创业板盘后
# 固定价交易仍在成交 (量未定版)。数据完整以盘后管道 (默认 15:35, 见
# daily_pipeline) 的官方 batch 行覆盖为准; 定版线取盘后固定价终止线 15:30,
# 与 quote_service._FINAL_DEADLINE 的 close_final 重试截止对齐 (15:30 后不再
# 有新实时写入, 判 15:30 与判 15:35 等价)。
CLOSE_CUTOFF = dt_time(15, 30)

# 扫描窗口: 最近 N 个自然日内、今天之前的交易日
SCAN_WINDOW_DAYS = 7

# 自动修复窗口: 最早坏日距今超过 N 个自然日 → 只报告不自动修 (更大缺口由用户手动 repair)
AUTO_REPAIR_MAX_LAG_DAYS = 5

# 参与检测的日K族表 (实时 flush 会写这三族的 daily/enriched)
_DAILY_TABLES = ("kline_daily", "kline_etf_daily", "kline_index_daily")

# 表 → 资产族 (用于管道/修复侧按族取起点)
TABLE_FAMILY = {
    "kline_daily": "stock",
    "kline_etf_daily": "etf",
    "kline_index_daily": "index",
}


@dataclass(frozen=True)
class IntegrityIssue:
    day: date
    table: str
    kind: str  # "snapshot"=盘中快照 | "missing"=分区缺失
    # snapshot 且能定位时: 缺权威行(batch)的 symbol 清单 (修复只拉这些);
    # 空 = 干净或无法定位 (missing / 列缺失 / 读失败), 修复回退全市场。
    symbols: tuple[str, ...] = ()


def _quote_ts_max_ms(part_dir: Path) -> int | None:
    """读单个日期分区的 max(quote_ts); 列不存在/全 null/无统计 → None。

    优先走 parquet 元数据 row-group statistics (不解压数据页),
    statistics 缺失时回退 polars 列扫描。
    """
    import pyarrow.parquet as pq

    candidates: list[int | None] = []
    files = sorted(part_dir.glob("*.parquet"))
    if not files:
        return None
    for path in files:
        try:
            meta = pq.read_metadata(path)
            names = [meta.schema.column(i).name for i in range(meta.num_columns)]
            if "quote_ts" not in names:
                continue
            idx = names.index("quote_ts")
            file_max: int | None = None
            for rg in range(meta.num_row_groups):
                stats = meta.row_group(rg).column(idx).statistics
                if stats is not None and stats.max is not None:
                    value = stats.max
                    file_max = int(value) if file_max is None else max(file_max, int(value))
            if file_max is None and meta.num_rows > 0:
                # statistics 未写入 → 回退列扫描
                file_max = (
                    pl.scan_parquet(path)
                    .select(pl.col("quote_ts").max())
                    .collect()
                    .item()
                )
            candidates.append(file_max)
        except Exception as e:  # noqa: BLE001
            logger.debug("quote_ts scan skipped %s: %s", path, e)
    values = [v for v in candidates if v is not None]
    return max(values) if values else None


def _is_snapshot(day: date, quote_ts_ms: int | None) -> bool:
    """非空 quote_ts 且对应北京时间时刻早于当日收盘线 → 盘中快照。"""
    if quote_ts_ms is None:
        return False
    try:
        ts = datetime.fromtimestamp(int(quote_ts_ms) / 1000, tz=CN_TZ)
    except (OverflowError, OSError, ValueError):
        return False
    return ts.date() == day and ts.time() < CLOSE_CUTOFF


def _partition_snapshot_defects(
    day: date, part_dir: Path, quote_ts_max_ms: int | None
) -> set[str] | None:
    """分区级快照判定 → 缺陷 symbol 集合 (issue #514: 判坏下沉到 symbol 级)。

    batch 行用 null quote_ts 标识权威历史。实时轮询曾把停牌股票的 09:15、
    零成交记录写入分区; 后续 batch 会过滤停牌日, merge-upsert 因而留下这些
    孤立行。旧逻辑「任一行有成交即判坏整分区」会让 4 行残留触发全市场重拉。

    返回语义:
    - None      : 无法判定但按坏处理 (volume/amount 列缺失 / 读失败 / 无 symbol
                  列却有成交残留) — 保守 fail-closed, 修复回退全市场;
    - 空集合    : 分区干净 (残留 symbol 均已有权威行, 或仅零成交残留且权威行占多数);
    - 非空集合  : 恰好这些 symbol 在窗口内写了有成交实时行且无权威行 → 只修它们。
    """
    if not _is_snapshot(day, quote_ts_max_ms):
        return set()

    start_ms = int(datetime.combine(day, dt_time.min, tzinfo=CN_TZ).timestamp() * 1000)
    cutoff_ms = int(datetime.combine(day, CLOSE_CUTOFF, tzinfo=CN_TZ).timestamp() * 1000)
    authoritative_rows = 0
    suspicious_rows = 0
    authoritative_symbols: set[str] = set()
    suspicious_symbols: set[str] = set()  # 窗口内实时行的 symbol (含零成交)
    active_symbols: set[str] = set()  # 窗口内且有成交的实时行 symbol

    for path in sorted(part_dir.glob("*.parquet")):
        try:
            schema = pl.read_parquet_schema(path)
            if "quote_ts" not in schema:
                continue
            columns = [
                name for name in ("symbol", "quote_ts", "volume", "amount")
                if name in schema
            ]
            frame = pl.read_parquet(path, columns=columns).with_columns(
                pl.col("quote_ts").cast(pl.Int64, strict=False),
            )
            has_symbol = "symbol" in frame.columns
            authoritative = frame.filter(pl.col("quote_ts").is_null())
            authoritative_rows += authoritative.height
            if has_symbol:
                authoritative_symbols.update(authoritative["symbol"].drop_nulls().to_list())
            suspicious = frame.filter(
                pl.col("quote_ts").is_between(start_ms, cutoff_ms, closed="left")
            )
            if suspicious.is_empty():
                continue
            suspicious_rows += suspicious.height

            activity_columns = [
                name for name in ("volume", "amount") if name in suspicious.columns
            ]
            if not activity_columns:
                return None
            active = suspicious.filter(
                pl.any_horizontal(
                    pl.col(name).cast(pl.Float64, strict=False).fill_null(0) > 0
                    for name in activity_columns
                )
            )
            if has_symbol:
                suspicious_symbols.update(suspicious["symbol"].drop_nulls().to_list())
                active_symbols.update(active["symbol"].drop_nulls().to_list())
            elif active.height > 0:
                return None  # 无 symbol 列却存在成交残留, 无法定位 → 整分区保守判坏
        except Exception as e:
            logger.debug("snapshot residue scan skipped %s: %s", path, e)
            return None

    if active_symbols:
        # 有成交残留: 只把「无权威行」的 symbol 记为缺陷; 两行兼有 = 无害滞后残留。
        return active_symbols - authoritative_symbols
    # 零成交残留: 沿用旧比例兜底 (权威行不占多数 → 整分区仍判坏, 现有行为锁定)。
    if suspicious_rows > 0 and authoritative_rows <= suspicious_rows:
        defects = suspicious_symbols - authoritative_symbols
        return defects or None  # 定位不到 symbol (如无 symbol 列) → 按未知处理
    return set()


def _partition_is_snapshot(day: date, part_dir: Path, quote_ts_max_ms: int | None) -> bool:
    """整个分区是否仍是盘中快照 (旧布尔口径, 供既有调用方; 内部走 symbol 级判据)。"""
    defects = _partition_snapshot_defects(day, part_dir, quote_ts_max_ms)
    return defects is None or bool(defects)


def _candidate_days(today: date, lookback_days: int) -> list[date]:
    """最近 lookback_days 自然日内、严格早于今天的交易日。

    fuyao 交易日历可用时按真实日历过滤 (休市日不进候选, 消除节假日误报 —
    2026 中秋 09-25 休市日曾被当缺失日, realtime_gate 409 死锁);
    从未取到日历时回退工作日近似 (历史行为)。
    """
    calendar = _trading_calendar()
    days: list[date] = []
    for offset in range(1, lookback_days + 1):
        d = today - timedelta(days=offset)
        if calendar is not None:
            if d in calendar:
                days.append(d)
        elif d.weekday() < 5:
            days.append(d)
    return sorted(days)


# ---------------------------------------------------------------------------
# 交易日历 (fuyao) — 「工作日近似」在节假日误报 missing; 修复任务对休市日
# 永远拉不到数据, realtime_gate 因此循环 409 (2026-09-25 中秋实证)。
# 取数失败时沿用上一次成功结果 (stale-while-error): 节假日表近乎不变,
# 陈旧日历仍远好于退回工作日近似 (长假期内误报会让门禁反复死锁)。
# ---------------------------------------------------------------------------
_CAL_TTL_S = 1800.0  # 成功/失败结论统一 30 分钟复取 (日历变化极稀疏)
_CAL_LOCK = threading.Lock()
_CAL: tuple[float, set[date] | None] = (0.0, None)  # (取数时刻, 交易日集合)


def _trading_calendar() -> set[date] | None:
    """A 股交易日集合 (fuyao 近一年); 从未取到过 → None (调用方回退周几近似)。

    与 trading_day.is_trading_day 探测链第 1 环同源; 这里需要**任意历史日**
    的判定, 故直接消费整年集合。线程安全 (gate / boot / 修复管道共用)。
    """
    now = time.monotonic()
    global _CAL
    with _CAL_LOCK:
        fetched_at, cached = _CAL
        if fetched_at and now - fetched_at < _CAL_TTL_S:
            return cached
    fetched: set[date] | None = None
    try:
        from app.data_providers import custom as custom_sources

        if custom_sources.is_custom_provider("fuyao"):
            raw = custom_sources.get_provider("fuyao").trading_days()
            if raw:
                fetched = set(raw)
    except Exception:  # noqa: BLE001 — 日历不可用不上抛, 回退 stale/近似
        fetched = None
    with _CAL_LOCK:
        _CAL = (now, fetched if fetched is not None else cached)
        return _CAL[1]


def scan_recent_integrity(
    data_dir: Path,
    *,
    today: date | None = None,
    lookback_days: int = SCAN_WINDOW_DAYS,
) -> list[IntegrityIssue]:
    """扫描最近交易日的数据完整性, 返回坏分区列表 (按日期升序)。

    每族表独立判定; 族内"最近无任何活动"(最新分区早于窗口)时整族跳过 —
    覆盖首次启动(无数据)与长期停用(用户自主)两类不应自动修复的场景。
    """
    data_dir = Path(data_dir)
    today = today or datetime.now(CN_TZ).date()
    window_start = today - timedelta(days=lookback_days)
    issues: list[IntegrityIssue] = []

    for table in _DAILY_TABLES:
        base = data_dir / table
        existing: set[date] = set()
        if base.exists():
            for part in base.glob("date=*"):
                try:
                    existing.add(date.fromisoformat(part.name[5:]))
                except ValueError:
                    continue
        latest = max(existing) if existing else None
        # 族内近期无活动 → 不判定 (首次启动 / 长期停用)
        if latest is None or latest < window_start:
            continue

        for day in _candidate_days(today, lookback_days):
            if day not in existing:
                # 只报"尾部缺口": 晚于本地最新分区的缺失日。
                # 历史内部空洞是另一类问题(laggards), 已有独立告警, 不在此扩面。
                if day > latest:
                    issues.append(IntegrityIssue(day=day, table=table, kind="missing"))
                continue
            part_dir = base / f"date={day.isoformat()}"
            quote_ts = _quote_ts_max_ms(part_dir)
            defects = _partition_snapshot_defects(day, part_dir, quote_ts)
            if defects is None or defects:
                issues.append(IntegrityIssue(
                    day=day, table=table, kind="snapshot",
                    symbols=tuple(sorted(defects)) if defects else (),
                ))

    issues.sort(key=lambda i: (i.day, i.table))
    return issues


def earliest_issue_day(
    issues: list[IntegrityIssue],
    tables: Iterable[str] | None = None,
) -> date | None:
    """坏分区中最早的一天; tables 限定参与的表族 (None=全部)。"""
    scoped = (
        [i for i in issues if i.table in tables] if tables is not None else issues
    )
    return min((i.day for i in scoped), default=None)


def within_auto_repair_window(day: date | None, *, today: date | None = None) -> bool:
    """最早坏日是否落在自动修复窗口内 (≤ AUTO_REPAIR_MAX_LAG_DAYS 自然日)。"""
    if day is None:
        return False
    today = today or datetime.now(CN_TZ).date()
    return (today - day).days <= AUTO_REPAIR_MAX_LAG_DAYS


# ── 自动修复退避 (issue #514) ────────────────────────────────────────
# 同一坏日的自动修复失败后 (多为数据源拉取失败), 重启/再开实时行情不应无条件
# 再发一次全量修复。按坏日在 data_dir 记 last_attempt, 冷却期内跳过自动修复;
# 数据页手动修正不受限制 (不走 launch_integrity_repair)。
INTEGRITY_REPAIR_BACKOFF_S = 6 * 3600.0


def _backoff_path(data_dir: Path) -> Path:
    return Path(data_dir) / ".integrity_repair_backoff.json"


def repair_backoff_remaining_s(data_dir: Path, start_date: date) -> float:
    """自动修复冷却剩余秒数 (0 = 不在退避期)。按修复起点日 (最早坏日) 记账。"""
    try:
        payload = json.loads(_backoff_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0
    last = (payload.get("last_attempt") or {}).get(start_date.isoformat())
    if not isinstance(last, (int, float)):
        return 0.0
    elapsed = time.time() - float(last)
    if elapsed >= INTEGRITY_REPAIR_BACKOFF_S:
        return 0.0
    return INTEGRITY_REPAIR_BACKOFF_S - elapsed


def _record_repair_attempt(data_dir: Path, start_date: date) -> None:
    payload = {"last_attempt": {start_date.isoformat(): time.time()}}
    path = _backoff_path(data_dir)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.debug("repair backoff marker write failed: %s", e)


def prune_enriched_partitions(
    data_dir: Path,
    start: date,
    table: str = "kline_daily_enriched",
) -> int:
    """删除 enriched 表 date ≥ start 的日期分区, 使修复重算把它们当"新日期"。

    股票 enriched 增量重算只算 enriched 里不存在的日期; 盘中快照日分区已存在
    (虽是错的), 不删则永远不会被重算。指数/ETF 的 enriched 走 merge-upsert
    全行覆盖, 无需删除。删除后 run_pipeline(new_dates_only=True) 用剩余分区
    最近 60 天做历史前缀重算 (修复窗口 ≤5 天, 回看充足)。
    """
    base = Path(data_dir) / table
    if not base.exists():
        return 0
    import shutil

    removed = 0
    for part in base.glob("date=*"):
        try:
            d = date.fromisoformat(part.name[5:])
        except ValueError:
            continue
        if d >= start:
            shutil.rmtree(part, ignore_errors=True)
            removed += 1
    return removed


def describe_issues(issues: list[IntegrityIssue]) -> str:
    """面向用户的一句话描述 (409 详情 / 日志用)。"""
    if not issues:
        return ""
    days = sorted({i.day for i in issues})
    day_text = "、".join(d.isoformat() for d in days)
    kinds = {i.kind for i in issues}
    reason = "停机前的盘中快照" if "snapshot" in kinds else "缺失"
    return f"{day_text} 的数据为{reason}"


def launch_integrity_repair(
    app_state,
    start_date: date,
    reason: str,
    symbols: list[str] | None = None,
) -> tuple[str | None, bool]:
    """自动创建数据修复任务 (复用 repair_daily 管道 + JobStore 任务体系)。

    返回 (job_id, is_new):
    - (None, False)  : 无法修复 (无 batch 能力 / 无 repo) 或自动修复退避中
    - (id, False)    : 已有 pending/running 任务复用 (singleflight)
    - (id, True)     : 新建并启动
    任务体与 /api/kline/repair_daily 完全一致: run slot + 实时 paused 互斥 +
    run_repair_daily(override_start_date, symbols)。

    symbols: 坏分区能定位到的缺陷标的清单 (issue #514 定向修复); None/空 = 全市场。

    调度自适应: 调用方在事件循环内 (API 端点) → executor 后台执行;
    无事件循环 (boot Timer 线程) → 独立 daemon 线程执行。
    """
    import asyncio
    import threading

    repo = getattr(app_state, "repo", None)
    capset = getattr(app_state, "capabilities", None)
    if repo is None or capset is None:
        return None, False
    try:
        from app.tickflow.capabilities import Cap

        if not capset.has(Cap.KLINE_DAILY_BATCH):
            logger.info("integrity repair skipped: no KLINE_DAILY_BATCH capability")
            return None, False
    except Exception:  # noqa: BLE001
        return None, False

    try:
        remaining = repair_backoff_remaining_s(repo.store.data_dir, start_date)
    except Exception:  # noqa: BLE001 — repo 形态异常时不因退避查询阻断修复
        remaining = 0.0
    if remaining > 0:
        logger.warning(
            "integrity: 自动修复退避中 (start=%s, 剩余 %.0f 分钟, reason=%s), 跳过。"
            "上次修复未成功多为数据源拉取失败, 可稍后自动重试或在数据页手动修正",
            start_date, remaining / 60, reason,
        )
        return None, False

    from app.services.pipeline_jobs import (
        JobCancelledError,
        job_store,
        release_run_slot,
        run_with_capacity,
        try_acquire_run_slot,
    )
    from app.services.repair_daily import run_repair_daily

    job_id, is_new = job_store.create()
    if not is_new:
        return job_id, False

    with contextlib.suppress(Exception):
        _record_repair_attempt(repo.store.data_dir, start_date)
    repair_symbols = list(symbols) if symbols else None
    qs = getattr(app_state, "quote_service", None)

    def progress(stage: str, pct: int, msg: str,
                 stage_pct: int | None = None, skip_log: bool = False) -> None:
        job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

    def _run() -> dict:
        # 修复期间暂停实时取数, 防止覆写同一批 parquet 竞态
        if qs:
            with qs.paused():
                return run_repair_daily(
                    repo, capset, start_date, on_progress=progress, symbols=repair_symbols,
                )
        return run_repair_daily(
            repo, capset, start_date, on_progress=progress, symbols=repair_symbols,
        )

    def _execute() -> None:
        try:
            if not try_acquire_run_slot(job_id):
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
            result = run_with_capacity(job_id, _run)
            if isinstance(result, dict) and "error" in result:
                job_store.fail(job_id, str(result["error"]))
            else:
                job_store.succeed(job_id, result)
        except JobCancelledError:
            pass  # 已由 terminate() 标记失败
        except Exception as e:  # noqa: BLE001
            logger.exception("integrity repair failed: job_id=%s", job_id)
            job_store.fail(job_id, str(e))
        finally:
            release_run_slot(job_id)
            with contextlib.suppress(Exception):
                from app.api.data import invalidate_storage_cache

                invalidate_storage_cache()

    try:
        loop = asyncio.get_running_loop()

        async def task() -> None:
            await loop.run_in_executor(None, _execute)

        asyncio.create_task(task())
    except RuntimeError:
        threading.Thread(
            target=_execute, daemon=True, name=f"integrity-repair-{job_id[:8]}"
        ).start()

    logger.warning("integrity: 自动修复任务启动 job=%s start=%s reason=%s", job_id, start_date, reason)
    return job_id, True


def boot_integrity_check(app_state) -> None:
    """启动自检 (后台线程调用): 发现窗口内的坏数据自动创建修复任务。

    分钟K缺口无需单独处理 — 修复管道 Step 2.5 在 minute_sync_enabled 时
    以 start=max(datetime) 增量补洞, 天然覆盖停机缺口。
    """
    repo = getattr(app_state, "repo", None)
    if repo is None:
        return
    try:
        issues = scan_recent_integrity(repo.store.data_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("boot integrity scan failed: %s", e)
        return
    if not issues:
        logger.info("boot integrity check: 近 %d 个交易日数据完整", SCAN_WINDOW_DAYS)
        return
    earliest = earliest_issue_day(issues)
    logger.warning("boot integrity check: %s (共 %d 个坏分区)", describe_issues(issues), len(issues))
    if not within_auto_repair_window(earliest):
        logger.warning(
            "integrity: 最早坏日 %s 超出自动修复窗口(%d 天), 请在数据页手动执行数据修正",
            earliest, AUTO_REPAIR_MAX_LAG_DAYS,
        )
        return
    with contextlib.suppress(Exception):
        launch_integrity_repair(
            app_state, earliest, "boot_integrity_check",
            symbols=_stock_snapshot_symbols(issues),
        )


def _stock_snapshot_symbols(issues: list[IntegrityIssue]) -> list[str] | None:
    """issues 里股票日K快照坏分区可定位的缺陷标的并集 (定向修复用; 空则全市场)。"""
    symbols = {
        s
        for i in issues
        if i.table == "kline_daily" and i.kind == "snapshot"
        for s in i.symbols
    }
    return sorted(symbols) if symbols else None
