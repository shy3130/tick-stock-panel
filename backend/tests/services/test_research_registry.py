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


def _factor_run_id(seed: str = "a") -> str:
    return "rr-" + seed * 16


def _save_factor_run(data_dir, run_id: str) -> str:
    from app.research.job_store import FactorJobStore

    FactorJobStore(data_dir).create(
        {"run_id": run_id, "factor_id": "n-shape", "job_status": "completed"}
    )
    return run_id


def test_add_evidence_rejects_factor_run_kind(store, tmp_path):
    hyp = store.create_hypothesis("t", "x")
    run_id = _save_factor_run(tmp_path, _factor_run_id())
    with pytest.raises(ValueError, match="link_factor_run"):
        store.add_evidence(hyp.id, "factor_run", run_id, "绕过验证")
    assert store.get_hypothesis(hyp.id).evidence == []


def test_link_factor_run_is_idempotent(store, tmp_path):
    hyp = store.create_hypothesis("t", "x")
    run_id = _save_factor_run(tmp_path, _factor_run_id())
    first = store.link_factor_run(hyp.id, run_id, "首次")
    updated_at = first.updated_at
    again = store.link_factor_run(hyp.id, run_id, "第二次")
    links = [e for e in again.evidence if e["kind"] == "factor_run"]
    assert len(links) == 1
    assert links[0]["summary"] == "首次"
    assert again.updated_at == updated_at


def test_link_factor_run_concurrent_single_entry(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    hyp = rr.ResearchStore(tmp_path).create_hypothesis("t", "x")
    run_id = _save_factor_run(tmp_path, _factor_run_id("b"))

    def link(_):
        rr.ResearchStore(tmp_path).link_factor_run(hyp.id, run_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(link, range(8)))
    final = rr.ResearchStore(tmp_path).get_hypothesis(hyp.id)
    links = [e for e in final.evidence if e["kind"] == "factor_run"]
    assert [e["ref"] for e in links] == [run_id]


def test_link_factor_run_requires_existing_hypothesis(store, tmp_path):
    run_id = _save_factor_run(tmp_path, _factor_run_id("c"))
    with pytest.raises(KeyError):
        store.link_factor_run("hyp-" + "0" * 8, run_id)


def test_link_factor_run_requires_existing_run(store):
    hyp = store.create_hypothesis("t", "x")
    with pytest.raises(ValueError, match="factor run not found"):
        store.link_factor_run(hyp.id, _factor_run_id("d"))
    assert store.get_hypothesis(hyp.id).evidence == []


def test_link_factor_run_rejects_unsafe_run_id(store):
    hyp = store.create_hypothesis("t", "x")
    for run_id in ("../evil", "run id", "", "rr-SHORT"):
        with pytest.raises(ValueError):
            store.link_factor_run(hyp.id, run_id)
    assert store.get_hypothesis(hyp.id).evidence == []


def test_hypotheses_for_run_is_stable_and_fail_closed(tmp_path):
    run_id = _save_factor_run(tmp_path, _factor_run_id("e"))
    store = rr.ResearchStore(tmp_path)
    first = store.create_hypothesis("一", "x")
    second = store.create_hypothesis("二", "x")
    store.link_factor_run(first.id, run_id)
    store.link_factor_run(second.id, run_id)
    got = store.hypotheses_for_run(run_id)
    assert [h.id for h in got] == sorted([first.id, second.id])
    assert store.hypotheses_for_run("../evil") == []
