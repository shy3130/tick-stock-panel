import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import research
from app.api.backtest import _strategy_stream_done_event
from app.backtest.run_store import BacktestRunStore


def request(tmp_path):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def test_create_list_and_evidence(tmp_path):
    req = request(tmp_path)
    h = research.create_hypothesis(research.HypothesisIn(title="动量", thesis="有效"), req)
    assert h["status"] == "exploring"
    research.add_evidence(h["id"], research.EvidenceIn(kind="note", summary="观察"), req)
    items = research.list_hypotheses(req, query="动量")["items"]
    assert len(items) == 1
    assert items[0]["evidence"][0]["summary"] == "观察"


def test_invalid_status_maps_to_400(tmp_path):
    with pytest.raises(HTTPException) as exc:
        research.create_hypothesis(research.HypothesisIn(title="x", thesis="y", status="bad"), request(tmp_path))
    assert exc.value.status_code == 400


def test_missing_hypothesis_maps_to_404(tmp_path):
    with pytest.raises(HTTPException) as exc:
        research.get_hypothesis("hyp-missing", request(tmp_path))
    assert exc.value.status_code == 404


def test_strategy_stream_done_event_writes_backtest_run(tmp_path):
    @dataclass
    class Result:
        run_id: str = "run-stream"
        config: dict | None = None
        strategy_info: dict | None = None
        stats: dict | None = None
        error: str | None = None

    req = request(tmp_path)
    event = _strategy_stream_done_event(
        req,
        Result(
            config={
                "strategy_id": "macd",
                "start": "2026-01-01",
                "end": "2026-06-30",
            },
            strategy_info={"id": "macd"},
            stats={"sharpe": 2.0},
        ),
    )

    assert event.startswith("event: done")
    payload = json.loads(event.split("data: ", 1)[1])
    assert payload["persisted"] is True
    assert BacktestRunStore(tmp_path).get("run-stream").stats["sharpe"] == 2.0


def test_strategy_stream_done_event_exposes_persistence_failure(tmp_path, monkeypatch):
    @dataclass
    class Result:
        run_id: str = "run-stream-failed"
        config: dict | None = None
        strategy_info: dict | None = None
        stats: dict | None = None
        error: str | None = None

    monkeypatch.setattr("app.api.backtest._save_backtest_run", lambda *_: None)
    event = _strategy_stream_done_event(
        request(tmp_path),
        Result(
            config={
                "strategy_id": "macd",
                "start": "2026-01-01",
                "end": "2026-06-30",
            },
            strategy_info={"id": "macd"},
            stats={"sharpe": 2.0},
        ),
    )

    payload = json.loads(event.split("data: ", 1)[1])
    assert payload["persisted"] is False
    assert any(warning.startswith("persistence_failed:") for warning in payload["warnings"])
