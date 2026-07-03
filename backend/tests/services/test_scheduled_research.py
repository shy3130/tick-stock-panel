from types import SimpleNamespace

import polars as pl
import pytest

from app.services.scheduled_research import ScheduledResearchStore, register_jobs, run_schedule


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
    scheduler = SimpleNamespace(jobs=[], add_job=lambda *args, **kwargs: scheduler.jobs.append(kwargs))

    register_jobs(scheduler, store, SimpleNamespace())

    assert scheduler.jobs == []


def test_run_schedule_failure_is_recorded(tmp_path):
    item = ScheduledResearchStore(tmp_path).create("日报", "market_recap_daily", "0 18 * * 1-5")
    result = run_schedule(item, SimpleNamespace())

    assert item.last_status == "failed"
    assert result["warnings"]


def test_strategy_pool_summary(tmp_path):
    item = ScheduledResearchStore(tmp_path).create("周报", "strategy_pool_weekly", "0 18 * * 5")
    engine = SimpleNamespace(list_strategies=lambda: [1, 2])

    result = run_schedule(item, SimpleNamespace(strategy_engine=engine, repo=SimpleNamespace(get_instruments=lambda: pl.DataFrame())))

    assert item.last_status == "success"
    assert result["summary"] == "strategies=2"
