from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

TEMPLATES = {"market_recap_daily", "watchlist_recap_daily", "strategy_pool_weekly"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class ScheduledResearch:
    id: str
    name: str
    template: str
    cron: str
    enabled: bool = True
    params: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None


@dataclass
class ResearchRunResult:
    title: str
    summary: str
    artifacts: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


class ScheduledResearchStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "research" / "schedules"

    def list(self) -> list[ScheduledResearch]:
        if not self.root.exists():
            return []
        return [ScheduledResearch(**json.loads(p.read_text(encoding="utf-8"))) for p in sorted(self.root.glob("*.json"))]

    def get(self, sid: str) -> ScheduledResearch:
        path = self.root / f"{sid}.json"
        if not path.exists():
            raise KeyError(sid)
        return ScheduledResearch(**json.loads(path.read_text(encoding="utf-8")))

    def create(self, name: str, template: str, cron: str, enabled: bool = True, params: dict | None = None) -> ScheduledResearch:
        _check_template(template)
        _check_cron(cron)
        now = _now()
        item = ScheduledResearch(f"sr-{uuid.uuid4().hex[:8]}", name, template, cron, enabled, params or {}, now, now)
        self.save(item)
        return item

    def patch(self, sid: str, **fields) -> ScheduledResearch:
        item = self.get(sid)
        for key in ("name", "template", "cron", "enabled", "params"):
            if key in fields and fields[key] is not None:
                setattr(item, key, fields[key])
        _check_template(item.template)
        _check_cron(item.cron)
        item.updated_at = _now()
        self.save(item)
        return item

    def delete(self, sid: str) -> bool:
        path = self.root / f"{sid}.json"
        if not path.exists():
            return False
        path.unlink()
        return True

    def save(self, item: ScheduledResearch) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{item.id}.json").write_text(json.dumps(asdict(item), ensure_ascii=False, indent=2), encoding="utf-8")


def run_schedule(item: ScheduledResearch, app_state) -> dict:
    try:
        if item.template == "market_recap_daily":
            result = _market_recap(app_state)
        elif item.template == "watchlist_recap_daily":
            result = _watchlist_recap(app_state)
        elif item.template == "strategy_pool_weekly":
            result = _strategy_pool(app_state)
        else:
            raise ValueError(f"invalid template: {item.template}")
        item.last_status = "success"
        item.last_error = None
    except Exception as e:
        item.last_status = "failed"
        item.last_error = str(e)
        result = asdict(ResearchRunResult(item.name, "", warnings=[str(e)]))
    finally:
        item.last_run_at = _now()
    if item.last_status == "success":
        try:
            _persist_result(item, result, app_state)
        except Exception as e:
            item.last_status = "failed"
            item.last_error = str(e)
            result.setdefault("warnings", []).append(str(e))
    return result


def register_jobs(scheduler, store: ScheduledResearchStore, app_state) -> None:
    if scheduler is None:
        return
    _clear_research_jobs(scheduler)
    for item in store.list():
        if not item.enabled:
            continue
        minute, hour, day, month, day_of_week = item.cron.split()
        scheduler.add_job(
            _run_job,
            "cron",
            id=f"research:{item.id}",
            replace_existing=True,
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            args=[store, item.id, app_state],
        )


def _clear_research_jobs(scheduler) -> None:
    get_jobs = getattr(scheduler, "get_jobs", None)
    remove_job = getattr(scheduler, "remove_job", None)
    if not callable(get_jobs) or not callable(remove_job):
        return
    for job in get_jobs():
        job_id = getattr(job, "id", "")
        if str(job_id).startswith("research:"):
            remove_job(job_id)


def _run_job(store: ScheduledResearchStore, sid: str, app_state) -> None:
    item = store.get(sid)
    run_schedule(item, app_state)
    store.save(item)


def _market_recap(app_state) -> dict:
    from app.services.market_overview_builder import build_market_overview

    overview = build_market_overview(repo=app_state.repo, quote_service=getattr(app_state, "quote_service", None), depth_service=getattr(app_state, "depth_service", None))
    return asdict(ResearchRunResult("大盘复盘", f"as_of={overview.get('as_of')}"))


def _watchlist_recap(app_state) -> dict:
    data_dir = app_state.repo.store.data_dir
    path = data_dir / "user_data" / "watchlist.parquet"
    symbols = pl.read_parquet(path)["symbol"].to_list() if path.exists() else []
    latest, as_of = app_state.repo.get_enriched_latest()
    covered = latest.filter(pl.col("symbol").is_in(symbols)).height if symbols and not latest.is_empty() else 0
    quote_service = getattr(app_state, "quote_service", None)
    quotes = quote_service.get_quotes_compat() if quote_service is not None else pl.DataFrame()
    quote_count = quotes.filter(pl.col("symbol").is_in(symbols)).height if symbols and not quotes.is_empty() else 0
    return asdict(ResearchRunResult("自选复盘", f"watchlist={len(symbols)}; quotes={quote_count}; enriched={covered}; as_of={as_of}"))


def _strategy_pool(app_state) -> dict:
    engine = getattr(app_state, "strategy_engine", None)
    count = len(engine.list_strategies()) if engine else 0
    data_dir = app_state.repo.store.data_dir
    run_cards = len(list((data_dir / "research" / "run_cards").glob("*.json")))
    return asdict(ResearchRunResult("策略池周报", f"strategies={count}; run_cards={run_cards}"))


def _persist_result(item: ScheduledResearch, result: dict, app_state) -> None:
    from app.services.research_registry import ResearchStore

    data_dir = app_state.repo.store.data_dir
    store = ResearchStore(data_dir)
    run_id = f"{item.id}-{item.last_run_at}"
    card = store.save_run_card(run_id, "scheduled_research", {"schedule_id": item.id, "template": item.template, "params": item.params}, result)
    hyp_id = item.params.get("hypothesis_id")
    if hyp_id:
        store.add_evidence(str(hyp_id), "observation", card.run_id, result.get("summary", ""))


def _check_template(template: str) -> None:
    if template not in TEMPLATES:
        raise ValueError(f"invalid template: {template}")


def _check_cron(cron: str) -> None:
    if len(cron.split()) != 5:
        raise ValueError("cron must have 5 fields")
