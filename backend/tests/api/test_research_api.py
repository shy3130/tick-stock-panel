from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import research
from app.api.backtest import _save_strategy_run_card
from app.services.research_registry import ResearchStore


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


def test_strategy_run_card_hook_writes_file(tmp_path):
    req = request(tmp_path)
    result = SimpleNamespace(
        run_id="run-1",
        config={"strategy_id": "macd"},
        strategy_info={"id": "macd"},
        stats={"sharpe": 1.0},
    )
    _save_strategy_run_card(req, result)
    assert ResearchStore(tmp_path).get_run_card("run-1").stats["sharpe"] == 1.0
