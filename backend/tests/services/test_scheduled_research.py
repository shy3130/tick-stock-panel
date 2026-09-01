from types import SimpleNamespace

import polars as pl
import pytest

from app.services.research_registry import ResearchStore
from app.services.scheduled_research import ScheduledResearchStore, register_jobs, run_schedule


def app_state(tmp_path, *, strategies=None):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_latest=lambda: (pl.DataFrame({"symbol": ["600519.SH"]}), "2026-07-03"),
    )
    engine = SimpleNamespace(list_strategies=lambda: strategies or [])
    quote_service = SimpleNamespace(
        get_quotes_compat=lambda: pl.DataFrame({"symbol": ["000001.SZ"]})
    )
    return SimpleNamespace(repo=repo, strategy_engine=engine, quote_service=quote_service)


def test_create_list_and_invalid_inputs(tmp_path):
    store = ScheduledResearchStore(tmp_path)
    item = store.create("日报", "market_recap_daily", "0 18 * * 1-5")

    assert store.list()[0].id == item.id
    with pytest.raises(ValueError):
        store.create("x", "bad", "0 1 * * *")
    with pytest.raises(ValueError):
        store.create("x", "market_recap_daily", "* * *")


def test_disabled_schedule_not_registered(tmp_path):
    store = ScheduledResearchStore(tmp_path)
    store.create("日报", "market_recap_daily", "0 18 * * 1-5", enabled=False)
    scheduler = SimpleNamespace(
        jobs=[], add_job=lambda *args, **kwargs: scheduler.jobs.append(kwargs)
    )

    register_jobs(scheduler, store, SimpleNamespace())

    assert scheduler.jobs == []


def test_register_jobs_removes_stale_research_jobs(tmp_path):
    store = ScheduledResearchStore(tmp_path)
    store.create("日报", "market_recap_daily", "0 18 * * 1-5", enabled=False)
    removed = []
    old_jobs = [SimpleNamespace(id="research:old"), SimpleNamespace(id="daily:pipeline")]
    scheduler = SimpleNamespace(
        jobs=[],
        get_jobs=lambda: old_jobs,
        remove_job=lambda job_id: removed.append(job_id),
        add_job=lambda *args, **kwargs: scheduler.jobs.append(kwargs),
    )

    register_jobs(scheduler, store, SimpleNamespace())

    assert removed == ["research:old"]
    assert scheduler.jobs == []


def test_run_schedule_failure_is_recorded(tmp_path):
    item = ScheduledResearchStore(tmp_path).create("日报", "market_recap_daily", "0 18 * * 1-5")
    result = run_schedule(item, SimpleNamespace())

    assert item.last_status == "failed"
    assert result["warnings"]


def test_strategy_pool_summary(tmp_path):
    item = ScheduledResearchStore(tmp_path).create("周报", "strategy_pool_weekly", "0 18 * * 5")
    engine = SimpleNamespace(list_strategies=lambda: [1, 2])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    ResearchStore(tmp_path).save_run_card("old", "strategy", {}, {})

    result = run_schedule(item, SimpleNamespace(strategy_engine=engine, repo=repo))

    assert item.last_status == "success"
    assert result["summary"] == "strategies=2; run_cards=1"


def test_watchlist_summary_reads_watchlist_file(tmp_path):
    item = ScheduledResearchStore(tmp_path).create("自选", "watchlist_recap_daily", "0 18 * * 1-5")
    path = tmp_path / "user_data" / "watchlist.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600519.SH", "000001.SZ"]}).write_parquet(path)

    result = run_schedule(item, app_state(tmp_path))

    assert result["summary"] == "watchlist=2; quotes=1; enriched=1; as_of=2026-07-03"


def test_successful_schedule_saves_run_card(tmp_path):
    item = ScheduledResearchStore(tmp_path).create("周报", "strategy_pool_weekly", "0 18 * * 5")

    run_schedule(item, app_state(tmp_path, strategies=[1]))

    card = ResearchStore(tmp_path).get_run_card(f"{item.id}-{item.last_run_at}")
    assert card.kind == "scheduled_research"
    assert card.stats["summary"] == "strategies=1; run_cards=0"


def test_schedule_with_hypothesis_adds_evidence(tmp_path):
    hyp = ResearchStore(tmp_path).create_hypothesis("题目", "假设")
    item = ScheduledResearchStore(tmp_path).create(
        "周报", "strategy_pool_weekly", "0 18 * * 5", params={"hypothesis_id": hyp.id}
    )

    run_schedule(item, app_state(tmp_path, strategies=[1]))

    updated = ResearchStore(tmp_path).get_hypothesis(hyp.id)
    assert updated.evidence[-1]["kind"] == "observation"
    assert updated.evidence[-1]["summary"] == "strategies=1; run_cards=0"


def test_factor_run_schedule_requires_exact_params(tmp_path):
    store = ScheduledResearchStore(tmp_path)
    with pytest.raises(ValueError):
        store.create("因子", "factor_run", "0 18 * * 1-5", params={})
    with pytest.raises(ValueError):
        store.create(
            "因子",
            "factor_run",
            "0 18 * * 1-5",
            params={
                "factor_id": "n-shape",
                "scope": {"type": "symbols", "symbols": ["000001.SZ"]},
                "parameters": {"start": "2024-01-01", "end": "2024-12-31"},
                "extra": True,
            },
        )


def test_factor_run_schedule_creates_durable_run_without_run_card(tmp_path, monkeypatch):
    from app.research.contracts import PreflightResult

    item = ScheduledResearchStore(tmp_path).create(
        "因子",
        "factor_run",
        "0 18 * * 1-5",
        params={
            "factor_id": "n-shape",
            "scope": {"type": "symbols", "symbols": ["000001.SZ"]},
            "parameters": {"start": "2024-01-01", "end": "2024-12-31"},
        },
    )
    monkeypatch.setattr(
        "app.research.control.preflight",
        lambda repo, request: PreflightResult(
            ready=True,
            factor_id=request.factor_id,
            normalized_request=request.parameters,
        ),
    )
    monkeypatch.setattr(
        "app.research.adapters.execute_factor",
        lambda *args: __import__(
            "app.research.contracts", fromlist=["NormalizedResearchResult"]
        ).NormalizedResearchResult(
            profile="event_signal",
            status="unavailable",
            verdict="unavailable",
        ),
    )
    state = app_state(tmp_path)
    result = run_schedule(item, state)
    assert item.last_status == "success"
    assert "run_id=rr-" in result["summary"]
    assert not list((tmp_path / "research" / "run_cards").glob("*.json"))


def test_factor_run_full_market_registers_process_watcher(tmp_path, monkeypatch):
    from app.research.contracts import PreflightResult

    item = ScheduledResearchStore(tmp_path).create(
        "全市场因子",
        "factor_run",
        "0 18 * * 1-5",
        params={
            "factor_id": "macd-arms",
            "scope": {"type": "full_market"},
            "parameters": {
                "start": "2024-01-01",
                "end": "2025-01-01",
                "oos_start": "2024-07-01",
            },
        },
    )
    monkeypatch.setattr(
        "app.research.control.preflight",
        lambda repo, request: PreflightResult(
            ready=True,
            factor_id=request.factor_id,
            normalized_request=request.parameters,
        ),
    )
    process = object()
    watched = []
    monkeypatch.setattr(
        "app.research.control.spawn_full_market_worker",
        lambda data_dir, run_id: process,
    )
    monkeypatch.setattr(
        "app.research.control.watch_full_market_process",
        lambda received, jobs, run_id: watched.append((received, jobs, run_id)),
    )
    state = app_state(tmp_path)

    result = run_schedule(item, state)

    assert item.last_status == "success"
    assert "status=pending" in result["summary"]
    assert state.full_market_process is process
    assert watched and watched[0][0] is process


def test_factor_run_mutated_persisted_params_fail_closed(tmp_path):
    from app.services.scheduled_research import ScheduledResearch

    item = ScheduledResearch(
        id="sr-invalid",
        name="因子",
        template="factor_run",
        cron="0 18 * * 1-5",
        params={
            "factor_id": "n-shape",
            "scope": {"type": "symbols", "symbols": ["000001.SZ"]},
            "parameters": {"start": "2024-01-01", "end": "2024-12-31"},
            "hypothesis_id": "hyp-bypass",
        },
    )
    result = run_schedule(item, app_state(tmp_path))
    assert item.last_status == "failed"
    assert result["warnings"]
    assert "exactly" in result["warnings"][0]
