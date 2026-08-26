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


def test_create_or_get_hypothesis_by_tag_is_idempotent_across_instances(tmp_path):
    tag = "short_pool:" + "a" * 16
    first = rr.ResearchStore(tmp_path).create_or_get_hypothesis_by_tag(
        tag, "做T研究", "论点", tags=["做T研究", tag]
    )

    again = rr.ResearchStore(tmp_path).create_or_get_hypothesis_by_tag(
        tag, "做T研究", "论点", tags=["做T研究", tag]
    )

    assert again.id == first.id
    assert again.updated_at == first.updated_at
    assert len(list((tmp_path / "research" / "hypotheses").glob("*.json"))) == 1


def test_create_or_get_hypothesis_by_tag_concurrent_single_record(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    tag = "short_pool:" + "b" * 16
    kwargs = {"title": "做T研究", "thesis": "论点", "tags": ["做T研究", tag]}

    def create(_):
        # 每次调用新建 store 实例，模拟 API 层每请求一实例的真实形态
        return rr.ResearchStore(tmp_path).create_or_get_hypothesis_by_tag(tag, **kwargs)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(create, range(8)))

    assert len({h.id for h in results}) == 1
    assert len(list((tmp_path / "research" / "hypotheses").glob("*.json"))) == 1


def test_create_or_get_hypothesis_by_tag_requires_tag_present(tmp_path):
    store = rr.ResearchStore(tmp_path)
    with pytest.raises(ValueError):
        store.create_or_get_hypothesis_by_tag(
            "short_pool:" + "c" * 16, "做T研究", "论点", tags=["做T研究"]
        )
    assert store.search() == []
