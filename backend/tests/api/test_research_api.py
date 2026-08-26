import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
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
        research.create_hypothesis(
            research.HypothesisIn(title="x", thesis="y", status="bad"), request(tmp_path)
        )
    assert exc.value.status_code == 400


def test_generic_hypothesis_rejects_reserved_t_research_tags(tmp_path):
    with pytest.raises(HTTPException) as exc:
        research.create_hypothesis(
            research.HypothesisIn(
                title="伪造",
                thesis="伪造",
                tags=["market_concentration_v1"],
            ),
            request(tmp_path),
        )

    assert exc.value.status_code == 400
    assert research.list_hypotheses(request(tmp_path))["items"] == []


def test_generic_patch_rejects_reserved_t_research_tags(tmp_path):
    req = request(tmp_path)
    created = research.create_hypothesis(
        research.HypothesisIn(title="普通假设", thesis="普通"),
        req,
    )

    with pytest.raises(HTTPException) as exc:
        research.update_hypothesis(
            created["id"],
            research.HypothesisPatch(tags=["做T研究", "short_pool:" + "a" * 16]),
            req,
        )

    assert exc.value.status_code == 400
    assert research.get_hypothesis(created["id"], req)["tags"] == []


def test_generic_patch_updates_fields_normally(tmp_path):
    req = request(tmp_path)
    created = research.create_hypothesis(
        research.HypothesisIn(title="普通假设", thesis="普通"),
        req,
    )

    updated = research.update_hypothesis(
        created["id"],
        research.HypothesisPatch(title="改名", thesis="改论点", tags=["自定义"], status="testing"),
        req,
    )

    assert updated["title"] == "改名"
    assert updated["thesis"] == "改论点"
    assert updated["tags"] == ["自定义"]
    assert updated["status"] == "testing"


def test_confirm_t_research_recomputes_before_unique_write(tmp_path, monkeypatch):
    pool = {"pool_id": "a" * 16, "as_of": "2026-08-25"}
    calls = []
    monkeypatch.setattr(
        research,
        "run_short_pool",
        lambda _state, limit: calls.append(limit) or pool,
    )
    monkeypatch.setattr(
        research,
        "build_t_research_hypothesis",
        lambda verified: {
            "title": "做T研究 · AI短线研究池 · 2026-08-25",
            "thesis": f"verified={verified['pool_id']}",
            "status": "exploring",
            "tags": ["做T研究", "short_pool:" + verified["pool_id"]],
        },
    )

    created = research.confirm_t_research_hypothesis(
        research.TResearchConfirmIn(
            pool_id="a" * 16,
            as_of=date(2026, 8, 25),
            limit=8,
        ),
        request(tmp_path),
    )

    assert calls == [8]
    assert created["status"] == "exploring"
    assert created["tags"] == ["做T研究", "short_pool:" + "a" * 16]
    assert len(research.list_hypotheses(request(tmp_path))["items"]) == 1


def test_confirm_t_research_rejects_stale_pool_without_write(tmp_path, monkeypatch):
    monkeypatch.setattr(
        research,
        "run_short_pool",
        lambda _state, limit: {"pool_id": "b" * 16, "as_of": "2026-08-25"},
    )

    with pytest.raises(HTTPException) as exc:
        research.confirm_t_research_hypothesis(
            research.TResearchConfirmIn(
                pool_id="a" * 16,
                as_of=date(2026, 8, 25),
                limit=8,
            ),
            request(tmp_path),
        )

    assert exc.value.status_code == 409
    assert research.list_hypotheses(request(tmp_path))["items"] == []


def _confirmable_pool(monkeypatch, pool_id="a" * 16, as_of="2026-08-25"):
    pool = {"pool_id": pool_id, "as_of": as_of}
    monkeypatch.setattr(
        research,
        "run_short_pool",
        lambda _state, limit: pool,
    )
    monkeypatch.setattr(
        research,
        "build_t_research_hypothesis",
        lambda verified: {
            "title": f"做T研究 · AI短线研究池 · {verified['as_of']}",
            "thesis": f"verified={verified['pool_id']}",
            "status": "exploring",
            "tags": ["做T研究", f"short_pool:{verified['pool_id']}"],
        },
    )
    return pool


def _confirm(pool_id="a" * 16, day=date(2026, 8, 25), limit=8):
    return research.TResearchConfirmIn(pool_id=pool_id, as_of=day, limit=limit)


def _confirmed_t_hypothesis(tmp_path, monkeypatch):
    _confirmable_pool(monkeypatch)
    return research.confirm_t_research_hypothesis(_confirm(), request(tmp_path))


def test_confirm_t_research_same_pool_returns_same_hypothesis(tmp_path, monkeypatch):
    _confirmable_pool(monkeypatch)

    first = research.confirm_t_research_hypothesis(_confirm(), request(tmp_path))
    second = research.confirm_t_research_hypothesis(_confirm(), request(tmp_path))

    assert second["id"] == first["id"]
    assert research.get_hypothesis(first["id"], request(tmp_path))["id"] == first["id"]
    assert len(research.list_hypotheses(request(tmp_path))["items"]) == 1


def test_confirm_t_research_concurrent_same_pool_single_record(tmp_path, monkeypatch):
    _confirmable_pool(monkeypatch)
    req = request(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: research.confirm_t_research_hypothesis(_confirm(), req),
                range(8),
            )
        )

    assert len({item["id"] for item in results}) == 1
    assert len(research.list_hypotheses(request(tmp_path))["items"]) == 1


def test_confirm_t_research_different_pools_create_separately(tmp_path, monkeypatch):
    _confirmable_pool(monkeypatch, "a" * 16)
    first = research.confirm_t_research_hypothesis(_confirm("a" * 16), request(tmp_path))
    _confirmable_pool(monkeypatch, "b" * 16)
    second = research.confirm_t_research_hypothesis(_confirm("b" * 16), request(tmp_path))

    assert second["id"] != first["id"]
    assert len(research.list_hypotheses(request(tmp_path))["items"]) == 2


def test_patch_t_hypothesis_tags_clear_rejected(tmp_path, monkeypatch):
    created = _confirmed_t_hypothesis(tmp_path, monkeypatch)
    req = request(tmp_path)

    with pytest.raises(HTTPException) as exc:
        research.update_hypothesis(created["id"], research.HypothesisPatch(tags=[]), req)

    assert exc.value.status_code == 400
    assert research.get_hypothesis(created["id"], req)["tags"] == created["tags"]


def test_patch_t_hypothesis_non_reserved_tags_rewrite_rejected(tmp_path, monkeypatch):
    created = _confirmed_t_hypothesis(tmp_path, monkeypatch)
    req = request(tmp_path)

    with pytest.raises(HTTPException) as exc:
        research.update_hypothesis(created["id"], research.HypothesisPatch(tags=["自定义"]), req)

    assert exc.value.status_code == 400
    assert research.get_hypothesis(created["id"], req)["tags"] == created["tags"]


def test_patch_t_hypothesis_title_or_thesis_without_tags_rejected(tmp_path, monkeypatch):
    created = _confirmed_t_hypothesis(tmp_path, monkeypatch)
    req = request(tmp_path)
    patches = [
        research.HypothesisPatch(title="改写"),
        research.HypothesisPatch(thesis="改写"),
        research.HypothesisPatch(title="改写", thesis="改写"),
    ]

    for patch in patches:
        with pytest.raises(HTTPException) as exc:
            research.update_hypothesis(created["id"], patch, req)
        assert exc.value.status_code == 400

    stored = research.get_hypothesis(created["id"], req)
    assert stored["title"] == created["title"]
    assert stored["thesis"] == created["thesis"]


def test_patch_t_hypothesis_status_allowed(tmp_path, monkeypatch):
    created = _confirmed_t_hypothesis(tmp_path, monkeypatch)
    req = request(tmp_path)

    updated = research.update_hypothesis(
        created["id"], research.HypothesisPatch(status="rejected"), req
    )

    assert updated["status"] == "rejected"
    assert updated["title"] == created["title"]
    assert updated["thesis"] == created["thesis"]
    assert updated["tags"] == created["tags"]


def test_patch_t_hypothesis_status_with_protocol_fields_rejected(tmp_path, monkeypatch):
    created = _confirmed_t_hypothesis(tmp_path, monkeypatch)
    req = request(tmp_path)

    with pytest.raises(HTTPException) as exc:
        research.update_hypothesis(
            created["id"],
            research.HypothesisPatch(status="validated", title="改写"),
            req,
        )

    assert exc.value.status_code == 400
    stored = research.get_hypothesis(created["id"], req)
    assert stored["status"] == "exploring"
    assert stored["title"] == created["title"]


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
