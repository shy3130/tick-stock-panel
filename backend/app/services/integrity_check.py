"""显式日K自检报告与修补编排; 复用完整性检测与精准修补服务。"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from app.market_time import cn_now, cn_today
from app.services import daily_health, data_integrity
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)
ASSET_TABLES = {family: table for table, family in data_integrity.TABLE_FAMILY.items()}
ASSET_LABELS = {"stock": "股票", "etf": "ETF", "index": "指数"}
KIND_LABELS = {
    "missing": "日期缺失", "snapshot": "盘中快照未定版", "corrupt": "文件损坏或为空",
    "invalid": "日K数值异常", "unconfirmed": "缺失日期待核实",
    "recompute": "依赖指标待重算",
}


def _report_path(root: Path) -> Path:
    return root / "user_data" / "integrity_report.json"


def latest_report(root: Path) -> dict | None:
    try:
        value = json.loads(_report_path(root).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("version") == 1 else None
    except (OSError, ValueError):
        return None


def _save(root: Path, report: dict) -> None:
    path = _report_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(report, ensure_ascii=False))


def _repairable(issue: data_integrity.IntegrityIssue) -> bool:
    return issue.kind != "unconfirmed" and "目录以外" not in issue.reason


def _scan(root: Path, days: int, assets: list[str], end: date, progress=None):
    if not 1 <= days <= 366 or not assets or not set(assets).issubset(ASSET_TABLES):
        raise ValueError("请选择资产类型和 1～366 天的检查范围")
    if end >= cn_today():
        raise ValueError("自检只检查已经结束的历史日期")

    def emit(cur, total):
        if progress:
            progress("integrity_check", int(95 * cur / total), f"检查日K {cur}/{total}",
                     stage_pct=int(100 * cur / total), skip_log=cur < total)

    return data_integrity.scan_recent_integrity(
        root, today=end + timedelta(days=1), lookback_days=days,
        tables=[ASSET_TABLES[a] for a in assets], deep=True, on_progress=emit,
    )


def run_check(
    root: Path, *, lookback_days: int = 30, asset_types: list[str] | None = None,
    on_progress: Callable | None = None, end_date: date | None = None,
) -> dict:
    root = Path(root)
    assets = list(dict.fromkeys(asset_types if asset_types is not None else [
        data_integrity.TABLE_FAMILY[t] for t in data_integrity.active_daily_tables()
    ]))
    end = end_date or cn_today() - timedelta(days=1)
    issues = _scan(root, lookback_days, assets, end, on_progress)
    report = _make_report(root, lookback_days, assets, end, issues)
    if on_progress:
        on_progress("done", 100, f"自检完成: {len(report['issues'])} 个待处理分区", stage_pct=100)
    return report


def _known_issues(root: Path, assets: list[str], end: date) -> list[data_integrity.IntegrityIssue]:
    grouped = {}
    for entry in daily_health.snapshot(root)["entries"]:
        if entry["asset_type"] in assets and entry["day"] <= end.isoformat():
            grouped.setdefault((entry["asset_type"], entry["day"]), []).append(entry)
    issues = []
    for (asset, day), entries in grouped.items():
        raw = any(e["needs_raw"] for e in entries)
        symbols = {e["symbol"] for e in entries}
        issues.append(data_integrity.IntegrityIssue(
            date.fromisoformat(day), ASSET_TABLES[asset], entries[0]["kind"] if raw else "recompute",
            symbols=tuple(sorted(symbols)) if None not in symbols else (),
            reason=entries[0]["reason"] if raw else "日K已校验, 受影响指标或共享统计尚未完成重算",
        ))
    return issues


def _make_report(root: Path, lookback_days: int, assets: list[str], end: date, issues: list) -> dict:
    daily_health.record_scan(root, issues, assets=assets,
                            start=end - timedelta(days=lookback_days - 1), end=end)
    seen = {(i.table, i.day) for i in issues}
    issues = [*issues, *(i for i in _known_issues(root, assets, end) if (i.table, i.day) not in seen)]
    no_data = [a for a in assets if not any(
        p.is_dir() and p.name[5:] <= end.isoformat()
        for p in (root / ASSET_TABLES[a]).glob("date=*")
    )]
    rows = [{
        "asset_type": data_integrity.TABLE_FAMILY[i.table],
        "asset_label": ASSET_LABELS[data_integrity.TABLE_FAMILY[i.table]],
        "date": i.day.isoformat(), "kind": i.kind, "kind_label": KIND_LABELS[i.kind],
        "reason": i.reason or KIND_LABELS[i.kind], "repairable": _repairable(i),
        "symbols": list(i.symbols[:10]), "symbol_count": len(i.symbols),
    } for i in issues]
    repairable_count = sum(r["repairable"] for r in rows)
    uncertain_count = sum(r["kind"] == "unconfirmed" for r in rows)
    status = ("issues" if len(rows) > uncertain_count else "uncertain" if rows
              else "empty" if len(no_data) == len(assets) else "clean")
    report = {
        "version": 1, "id": uuid.uuid4().hex, "checked_at": cn_now().isoformat(),
        "start_date": (end - timedelta(days=lookback_days - 1)).isoformat(),
        "end_date": end.isoformat(), "lookback_days": lookback_days, "asset_types": assets,
        "status": status, "issues": rows, "repairable_count": repairable_count,
        "uncertain_count": uncertain_count, "no_data_assets": no_data,
        "data_revisions": {asset: daily_health.revision(root, asset) for asset in ASSET_LABELS},
    }
    _save(root, report)
    return report


def run_repair(repo, capset, report_id: str, on_progress: Callable | None = None) -> dict:
    from app.services.repair_daily import repair_integrity_issues

    root = Path(repo.store.data_dir)
    previous = latest_report(root)
    if previous is None or previous.get("id") != report_id:
        raise ValueError("自检报告已变化, 请重新自检后再修补")
    days, assets = previous["lookback_days"], previous["asset_types"]
    end = date.fromisoformat(previous["end_date"])
    # 报告只作确认凭证。实际修补范围重新读当前文件及持久化的待重算状态。
    raw = _scan(root, days, assets, end, on_progress)
    _make_report(root, days, assets, end, raw)
    # 更早自检已发现的异常也会影响当前窗口, 逐日重读以区分取数和仅需重算。
    for known in _known_issues(root, assets, end):
        current = data_integrity.inspect_daily_partition(root, known.table, known.day)
        found = [current] if current else []
        daily_health.record_scan(root, found, assets=[data_integrity.TABLE_FAMILY[known.table]],
                                start=known.day, end=known.day)
    issues = [i for i in _known_issues(root, assets, end) if _repairable(i)]
    if not issues:
        return run_check(root, lookback_days=days, asset_types=assets, end_date=end)
    backup_id = uuid.uuid4().hex
    failure: BaseException | None = None
    try:
        repair_integrity_issues(
            repo, capset, issues, backup_dir=root / "integrity_backups" / backup_id,
            on_progress=on_progress,
        )
    except BaseException as exc:  # noqa: BLE001 — 取消也保留可重试状态, 保存报告后原样抛出
        failure = exc

    report = run_check(root, lookback_days=days, asset_types=assets, end_date=end)
    if (root / "integrity_backups" / backup_id).exists():
        report["backup_dir"] = f"integrity_backups/{backup_id}"
    if failure is not None:
        report["repair_error"] = str(failure)
    elif report["repairable_count"]:
        report["repair_error"] = f"修补后仍有 {report['repairable_count']} 个问题, 请查看同步日志和数据源状态"
    _save(root, report)
    if failure is not None:
        raise failure
    if report.get("repair_error"):
        raise RuntimeError(report["repair_error"])
    return report


def repair_known_data(repo, capset, on_progress=None) -> None:
    """常规管道在计算前处理已经发现的异常, 与手动修补共用同一入口。"""
    root = Path(repo.store.data_dir)
    active = {data_integrity.TABLE_FAMILY[t] for t in data_integrity.active_daily_tables()}
    assets = sorted({e["asset_type"] for e in daily_health.snapshot(root)["entries"]} & active)
    if assets:
        report = run_check(root, asset_types=assets, on_progress=on_progress)
        if report["repairable_count"]:
            run_repair(repo, capset, report["id"], on_progress=on_progress)
    for asset in active:
        daily_health.assert_usable(root, asset)
