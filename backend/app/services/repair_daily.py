"""日K修补入口。

自检异常按资产、日期和股票精确修补, 复用数据源取数、仓库与正式指标计算。
旧的手动日期范围回补保持调用盘后管道, 不改变其同步范围和行为。
"""
from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from app.market_time import cn_today
from app.tickflow.capabilities import CapabilitySet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailyRepairTarget:
    asset_type: str
    day: date
    # None 表示无法定位股票, 仅回补这一日的整类日K。
    symbols: tuple[str, ...] | None


def plan_daily_repair(issues) -> list[DailyRepairTarget]:
    from app.services.data_integrity import TABLE_FAMILY

    grouped = {}
    for issue in issues:
        if issue.kind in {"unconfirmed", "recompute"}:
            continue
        key = TABLE_FAMILY[issue.table], issue.day
        symbols = set(issue.symbols) if issue.symbols else None
        previous = grouped.get(key, set())
        grouped[key] = previous | symbols if previous is not None and symbols is not None else None
    return [DailyRepairTarget(asset, day, tuple(sorted(symbols)) if symbols is not None else None)
            for (asset, day), symbols in sorted(grouped.items())]


def repair_integrity_issues(repo, capset, issues, *, backup_dir: Path, on_progress=None) -> dict:
    """精准修补编排: 所有源数据先暂存校验, 再写原始日K, 最后重算并发布依赖。

    普通同步/旧的手动范围回补仍调用原有管道。本入口只处理已经确认的异常,
    直接组合正式取数、仓库和指标函数, 不启动无关维表/分钟/除权/模拟盘流程。
    """
    import polars as pl

    from app.indicators.pipeline import recompute_daily_symbols
    from app.services import daily_health, data_integrity, kline_sync
    from app.tickflow.repository import daily_dirname

    root = Path(repo.store.data_dir)
    targets = plan_daily_repair(issues)
    staging = root / ".daily_repair_staging" / backup_dir.name

    def emit(stage, pct, message, *, stage_pct=0):
        if on_progress:
            on_progress(stage, pct, message, stage_pct=stage_pct)

    try:
        # 未完成全部取数和校验前, 正式日K/enriched 均保持原样。
        for i, target in enumerate(targets):
            label = daily_health.ASSET_LABELS[target.asset_type]
            repo.validate_daily_repair_target(target.asset_type, target.day)
            if target.symbols is None:
                symbols, required = repo.daily_repair_universe(target.asset_type, target.day)
            else:
                symbols, required = list(target.symbols), set(target.symbols)
            emit("sync_daily", int(45 * i / max(len(targets), 1)),
                 f"获取 {target.day} {label}日K ({len(symbols)} 只)")

            def progress(cur, total, *args, target_day=target.day, target_index=i):
                emit("sync_daily", int(45 * target_index / max(len(targets), 1)),
                     f"获取 {target_day} 日K批次 {cur}/{total}", stage_pct=int(100 * cur / total) if total else 0)

            frames = []
            for incoming in kline_sync.iter_daily_frames(
                symbols, capset, start_time=datetime.combine(target.day, datetime.min.time()),
                end_time=datetime.combine(target.day, datetime.min.time()), asset_type=target.asset_type,
                on_chunk_done=progress,
            ):
                if incoming.is_empty():
                    continue
                if not {"symbol", "date"}.issubset(incoming.columns):
                    raise ValueError("数据源返回的日K缺少股票或日期字段")
                incoming = incoming.with_columns(pl.col("date").cast(pl.Date, strict=False), pl.col("symbol").cast(pl.String))
                incoming = incoming.filter((pl.col("date") == target.day) & pl.col("symbol").is_in(symbols))
                if not incoming.is_empty():
                    if not data_integrity.invalid_daily_rows(incoming, target.day).is_empty():
                        raise ValueError(f"数据源返回的 {target.day} 日K仍异常, 未写入正式数据")
                    frames.append(incoming)
            if not frames:
                raise ValueError(f"数据源未返回 {target.day} 的修补日K, 原数据保持不变")
            frame = pl.concat(frames, how="diagonal_relaxed").unique(subset=["symbol", "date"], keep="last")
            missing = required - set(frame["symbol"].to_list())
            if missing:
                raise ValueError(f"{target.day} 缺少修补股票: {', '.join(sorted(missing)[:10])}")
            part = staging / daily_dirname(target.asset_type) / f"date={target.day}"
            part.mkdir(parents=True, exist_ok=True)
            frame.write_parquet(part / "part.parquet")
            if data_integrity.inspect_daily_partition(staging, daily_dirname(target.asset_type), target.day):
                raise ValueError(f"数据源返回的 {target.day} 日K未通过复检, 未写入正式数据")

        for i, target in enumerate(targets):
            emit("sync_daily", 45 + int(10 * i / max(len(targets), 1)), f"写入已校验的 {target.day} 修补日K")
            table = daily_dirname(target.asset_type)
            frame = pl.read_parquet(staging / table / f"date={target.day}/part.parquet")
            repo.replace_daily_rows(target.asset_type, target.day, frame,
                                    symbols=list(target.symbols) if target.symbols is not None else None,
                                    backup_dir=backup_dir / table / f"date={target.day}")
            remaining = data_integrity.inspect_daily_partition(root, table, target.day)
            if remaining is not None:
                raise ValueError(f"{target.day} 日K仍未通过校验, 未进入指标计算")
            daily_health.record_scan(root, [], assets=[target.asset_type], start=target.day, end=target.day)
            repo.refresh_view(table, str(root / table / "**/*.parquet"))

        # 每只股票各自的最早异常日起点; 只有无法定位的整日异常才扩大到整类资产。
        handled = {(data_integrity.TABLE_FAMILY[i.table], i.day.isoformat()) for i in issues}
        entries = [e for e in daily_health.snapshot(root)["entries"]
                   if (e["asset_type"], e["day"]) in handled]
        assets = {asset for asset, _ in handled}
        handled_days = {asset: {date.fromisoformat(day) for a, day in handled if a == asset}
                        for asset in assets}
        scopes = {}
        for entry in entries:
            if entry["asset_type"] not in assets or not entry.get("needs_enriched", True):
                continue
            key = entry["asset_type"], entry["symbol"]
            day = date.fromisoformat(entry["day"])
            scopes[key] = min(scopes.get(key, day), day)
        grouped = {}
        for (asset, symbol), start in scopes.items():
            full_start = scopes.get((asset, None))
            if symbol is not None and full_start is not None and start >= full_start:
                continue
            grouped.setdefault((asset, start), []).append(symbol)
        for (asset, start), symbols in sorted(grouped.items()):
            selected = None if None in symbols else sorted(symbols)
            emit("compute_enriched", 60, f"重算{daily_health.ASSET_LABELS[asset]} {start} 起的受影响指标")
            recompute_daily_symbols(repo, asset, start=start, symbols=selected,
                                    on_batch_done=lambda cur, total: emit("compute_enriched", 60 + int(30 * cur / total),
                                        f"受影响股票指标 {cur}/{total}", stage_pct=int(100 * cur / total)))
            daily_health.mark_enriched_ready(root, asset, start=start, symbols=selected,
                                             days=handled_days[asset])

        _refresh_repair_dependents(repo, entries, assets, emit)
        for asset in assets:
            day = min(date.fromisoformat(e["day"]) for e in entries if e["asset_type"] == asset)
            daily_health.complete(root, asset, start=day, symbols=None, days=handled_days[asset])
        return {"repaired_partitions": len(targets)}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _refresh_repair_dependents(repo, entries: list[dict], assets: set[str], emit) -> None:
    from app.services import strategy_cache
    from app.services.screener import ScreenerService

    root = Path(repo.store.data_dir)
    for asset in assets:
        repo.invalidate_enriched_asset(asset)
        ScreenerService.clear_history_cache(asset_type=asset)
    if "stock" in assets or "index" in assets:
        strategy_cache.clear_cache(root)
    if "index" in assets:
        repo.invalidate_enriched_asset("stock")
        repo.invalidate_enriched_asset("etf")
        ScreenerService.clear_history_cache()
    if "stock" in assets:
        _refresh_market_statistics(repo, min(date.fromisoformat(e["day"]) for e in entries if e["asset_type"] == "stock"), emit)


def _refresh_market_statistics(repo, start: date, emit) -> None:
    """已经存在的市场统计须跟随历史修正, 不受自动拉取开关影响。"""
    from app.services import market_mainline, regime_builder

    root = Path(repo.store.data_dir)
    end = repo.latest_enriched_date()
    if end is None:
        return
    if not regime_builder.load_regime_history(root).is_empty():
        emit("compute_regime", 92, "重算受影响日期的市场环境")
        rows = regime_builder.run_regime_batch(repo, start=start, end=end)
        regime_builder.replace_regime_range(root, start, end, rows)
        regime_builder.refresh_phase_labels(root, start=start)
    for kind in ("concept", "industry"):
        if not market_mainline.load_mainline_history(root, kind).is_empty():
            rows = market_mainline.compute_mainline_range(repo, root, start, end, kind=kind)
            market_mainline.replace_mainline_range(root, start, end, kind, rows)


def run_repair_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    start_date: date,
    end_date: date | None = None,
    on_progress: Callable | None = None,
    symbols: list[str] | None = None,
) -> dict:
    """修正 / 补全数据 — 复用盘后管道,日期范围由用户指定。

    通过 run_now(override_start_date=start_date) 把日K/除权/指数的拉取起点
    统一设为 start_date (到今天),其余流程与盘后管道完全一致。

    Args:
        repo:        数据仓库
        capset:      权限集
        start_date:  用户选定的起始日期
        end_date:    保留参数(目前盘后管道固定拉到今天, 此值未使用, 为接口兼容保留)
        on_progress: 进度回调
        symbols:     完整性自动修复传入的坏标的清单 (issue #514: 定向重拉,
                     不再全市场); 手动修正不传 = 全标的池。

    Returns:
        run_now() 的完整结果 dict。
    """
    today = cn_today()
    if start_date > today:
        return {"error": "起始日期不能晚于今天"}

    logger.info(
        "repair_daily: run pipeline with override_start_date=%s symbols=%s",
        start_date, f"{len(symbols)} targeted" if symbols else "universe",
    )

    from app.jobs.daily_pipeline import run_now
    return run_now(
        repo, capset, on_progress=on_progress,
        override_start_date=start_date, override_symbols=symbols,
    )
