import pytest

from app.services import research_registry as rr


@pytest.fixture()
def store(tmp_path):
    return rr.ResearchStore(tmp_path)


def test_create_update_and_search_hypothesis(store):
    h = store.create_hypothesis("低波动组合跑赢", "低波动因子在 A 股长期有效")
    assert h.status == "exploring"
    store.update_hypothesis(h.id, status="validated")
    assert store.get_hypothesis(h.id).status == "validated"
    assert [x.id for x in store.search(status="validated", query="低波动")] == [h.id]


def test_invalid_status_rejected(store):
    h = store.create_hypothesis("t", "x")
    with pytest.raises(ValueError):
        store.update_hypothesis(h.id, status="bad")


def test_evidence_append_and_order(store):
    h = store.create_hypothesis("t", "x")
    store.add_evidence(h.id, "note", "", "初步观察")
    store.add_evidence(h.id, "backtest", "run-1", "sharpe=1.2")
    got = store.get_hypothesis(h.id)
    assert [e["kind"] for e in got.evidence] == ["note", "backtest"]


def test_run_card_hashes_are_deterministic(store):
    cfg = {"strategy_id": "macd", "start": "2025-01-01", "end": "2025-06-30"}
    strategy = {"id": "macd", "v": 1}
    card = store.save_run_card("r1", "strategy", cfg, {"sharpe": 1.2}, strategy)
    assert (card.config_hash, card.strategy_hash) == rr.build_hashes(
        {"end": "2025-06-30", "start": "2025-01-01", "strategy_id": "macd"},
        {"v": 1, "id": "macd"},
    )
    assert store.get_run_card("r1").stats["sharpe"] == 1.2
