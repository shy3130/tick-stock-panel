from __future__ import annotations

import sys
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import research_agent as research_api
from app.config import settings
from app.research_agent.harness import ResearchAgentHarness
from app.research_agent.models import evidence
from app.research_agent.skill import evidence_prompt, parse_plan
from app.research_agent.store import ResearchRunCapacityError, ResearchRunStore
from app.research_agent.tools import _relevant_rss_items, _rss_items, collect_evidence


def test_evidence_redacts_credential_values_and_urls():
    item = evidence(
        source="test",
        title="test",
        status="available",
        summary="test",
        data={
            "api_key": "not-for-output",
            "nested": {"session_url": "https://sys.hibor.com.cn/macsystem?abc=secret"},
            "title": "https://sys.hibor.com.cn/macsystem?abc=secret",
        },
        url="https://example.test/report?token=secret",
    )

    assert item["data"]["api_key"] == "[redacted]"
    assert item["data"]["nested"]["session_url"] == "[redacted]"
    assert item["data"]["title"] == "[redacted Hibor session URL]"
    assert item["url"] == "https://example.test/report"


def test_store_reaps_stale_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store = ResearchRunStore()
    created = store.create(
        symbol="600664.SH",
        name="哈药股份",
        question="",
        include_web_news=True,
    )
    store.update(
        created["id"],
        status="collecting",
        started_at="2000-01-01T00:00:00+08:00",
    )

    stale = store.get(created["id"])

    assert stale is not None
    assert stale["status"] == "failed"
    assert stale["completed_at"]
    assert "重新运行" in stale["error"]
    assert (tmp_path / "user_data" / "research_agent_runs.json").stat().st_mode & 0o777 == 0o600


def test_store_preserves_compact_nested_evidence_for_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store = ResearchRunStore()
    created = store.create(
        symbol="600664.SH",
        name="哈药股份",
        question="",
        include_web_news=True,
    )
    item = evidence(
        source="test",
        title="nested",
        status="available",
        summary="test",
        data={
            "latest": {"close": 8.91},
            "responses": {"income": {"items": [{"metrics": {"revenue": 123}}]}},
        },
    )
    store.update(created["id"], evidence=[item])

    restored = store.get(created["id"])

    assert restored is not None
    assert restored["evidence"][0]["data"]["latest"]["close"] == 8.91
    assert restored["evidence"][0]["data"]["responses"]["income"]["items"][0]["metrics"]["revenue"] == 123


def test_store_claim_allows_only_one_executor(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store = ResearchRunStore()
    created = store.create(
        symbol="600664.SH",
        name="哈药股份",
        question="",
        include_web_news=True,
    )

    first_claim = store.claim(created["id"])
    second_claim = store.claim(created["id"])

    assert first_claim is not None
    assert first_claim["status"] == "planning"
    assert first_claim["started_at"]
    assert second_claim is None
    assert store.get(created["id"])["status"] == "planning"


def test_store_limits_expensive_active_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store = ResearchRunStore()
    first = store.create(
        symbol="600664.SH",
        name="哈药股份",
        question="",
        include_web_news=True,
    )
    store.create(
        symbol="000001.SZ",
        name="平安银行",
        question="",
        include_web_news=True,
    )

    with pytest.raises(ResearchRunCapacityError):
        store.create(
            symbol="600519.SH",
            name="贵州茅台",
            question="",
            include_web_news=True,
        )

    store.update(first["id"], status="succeeded", completed_at="2026-08-14T10:00:00+08:00")
    replacement = store.create(
        symbol="600519.SH",
        name="贵州茅台",
        question="",
        include_web_news=True,
    )

    assert replacement["status"] == "queued"


def test_full_scope_plan_is_stable_and_honors_web_toggle():
    plan = parse_plan('{"tools":["web_news"]}', include_web_news=True, full_scope=True)
    no_web = parse_plan('{"tools":["web_news"]}', include_web_news=False, full_scope=True)

    assert plan == [
        "market_snapshot",
        "realtime_snapshot",
        "financials",
        "market_intelligence",
        "strategy_signals",
        "research_reports",
        "announcements",
        "web_news",
    ]
    assert no_web == plan[:-1]


def test_collect_evidence_degrades_when_optional_integrations_are_absent(tmp_path, monkeypatch):
    """The upstream install must remain useful without local vendor extensions."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for module_name in (
        "app.services.hithink_finance",
        "app.services.special_data",
        "app.services.research_reports",
        "app.services.eastmoney_data",
        "app.data_providers.hibor_research",
    ):
        monkeypatch.setitem(sys.modules, module_name, None)

    class EmptyRepo:
        store = SimpleNamespace(data_dir=tmp_path)

        @staticmethod
        def resolve_asset_type(_symbol: str) -> str:
            return "stock"

        @staticmethod
        def get_daily_asset(*_args, **_kwargs) -> pl.DataFrame:
            return pl.DataFrame()

    app = SimpleNamespace(state=SimpleNamespace(repo=EmptyRepo()))
    records = collect_evidence(
        app,
        symbol="600664.SH",
        name="哈药股份",
        tools=[
            "market_snapshot",
            "realtime_snapshot",
            "financials",
            "market_intelligence",
            "strategy_signals",
            "research_reports",
            "announcements",
        ],
    )

    assert len(records) == 10
    assert [record["citation"] for record in records] == [f"[S{index:02d}]" for index in range(1, 11)]
    assert all(record["status"] in {"available", "partial", "unavailable"} for record in records)
    assert {record["title"] for record in records} >= {
        "同花顺快照、估值与财务报表",
        "同花顺热度与龙虎榜",
        "个股机构研报",
        "慧博公司与深度研报",
        "公司公告与披露",
    }


def test_rss_parser_keeps_only_linked_items():
    parsed = _rss_items(b"""<?xml version='1.0'?><rss><channel>
      <item><title>Valid headline</title><link>https://example.test/a</link><description>Summary</description></item>
      <item><title>Incomplete</title></item>
    </channel></rss>""")

    assert parsed == [{
        "title": "Valid headline",
        "published_at": "",
        "summary": "Summary",
        "url": "https://example.test/a",
    }]


def test_rss_relevance_filters_unrelated_results():
    items = [
        {"title": "哈药股份近期披露", "summary": "600664 公告", "url": "https://example.test/a"},
        {"title": "无关结果", "summary": "not the stock", "url": "https://example.test/b"},
    ]

    assert _relevant_rss_items(items, name="哈药股份", code="600664") == [items[0]]


def test_evidence_prompt_allows_compacted_announcement_body():
    record = evidence(
        source="公告",
        title="公司公告与披露",
        status="available",
        summary="正文优先",
        data={"details": [{"text": "公告正文" * 900}], "listing": [{"title": "索引"}]},
    )

    prompt = evidence_prompt([record])

    assert "公告正文" * 900 in prompt
    assert "[truncated]" not in prompt


def test_evidence_prompt_reserves_space_for_late_sources():
    records = [
        evidence(
            source="large",
            title="large",
            status="available",
            summary="large",
            data={"payload": "x" * 10_000},
        ),
        evidence(
            source="late",
            title="late",
            status="available",
            summary="late",
            data={"marker": "late-source-must-remain-visible"},
        ),
    ]

    prompt = evidence_prompt(
        records,
        max_chars_per_source=10_000,
        max_total_chars=1_000,
        min_chars_per_source=300,
    )

    assert "late-source-must-remain-visible" in prompt


def test_evidence_prompt_honors_single_source_total_budget():
    record = evidence(
        source="source",
        title="title",
        status="available",
        summary="summary",
        data={"payload": "x" * 10_000},
    )

    prompt = evidence_prompt(
        [record],
        max_chars_per_source=10_000,
        max_total_chars=300,
    )

    assert len(prompt) <= 300
    assert "[truncated]" in prompt


@pytest.mark.asyncio
async def test_harness_persists_evidence_and_rewrites_unknown_citations(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store = ResearchRunStore()
    app = SimpleNamespace(state=SimpleNamespace())
    progress: list[str] = []

    def collector(_app, *, symbol, name, tools, on_progress):
        assert symbol == "600664.SH"
        assert "web_news" in tools
        on_progress("本地行情", 1)
        progress.append(name)
        return [evidence(
            source="本地行情",
            title="日线",
            status="available",
            summary="截至测试日",
            data={"name": "哈药股份", "close": 5.2},
            as_of="2026-08-14",
        )]

    async def generator(messages, **_kwargs):
        if "## Planning task" in messages[0]["content"]:
            return '{"tools":["web_news"]}'
        return "# 研究\n事实 [S1];错误引用 [S99]"

    created = store.create(
        symbol="600664.SH",
        name="哈药股份",
        question="全面研究",
        include_web_news=True,
    )
    result = await ResearchAgentHarness(
        app,
        store=store,
        collector=collector,
        text_generator=generator,
    ).run(created["id"])

    assert result is not None
    assert result["status"] == "succeeded"
    assert result["evidence"][0]["citation"] == "[S01]"
    assert "[S01]" in result["answer"]
    assert "[引用无效]" in result["answer"]
    assert result["runtime"]["invalid_citations_rewritten"] == ["[S99]"]
    assert progress == ["哈药股份"]


@pytest.mark.asyncio
async def test_harness_keeps_collected_evidence_when_synthesis_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store = ResearchRunStore()
    app = SimpleNamespace(state=SimpleNamespace())

    def collector(_app, **_kwargs):
        return [evidence(
            source="本地行情",
            title="日线",
            status="available",
            summary="已返回",
            data={"close": 5.2},
        )]

    async def generator(messages, **_kwargs):
        if "## Planning task" in messages[0]["content"]:
            return "{}"
        raise RuntimeError("upstream unavailable")

    created = store.create(
        symbol="600664.SH",
        name="哈药股份",
        question="",
        include_web_news=False,
    )
    result = await ResearchAgentHarness(
        app,
        store=store,
        collector=collector,
        text_generator=generator,
    ).run(created["id"])

    assert result is not None
    assert result["status"] == "failed"
    assert len(result["evidence"]) == 1
    assert "RuntimeError" in result["error"]
    assert "upstream unavailable" not in result["error"]


def test_api_validates_symbol_and_schedules_run(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store = ResearchRunStore()
    monkeypatch.setattr(research_api, "run_store", store)
    app = FastAPI()
    app.include_router(research_api.router)
    started: list[str] = []

    class FakeHarness:
        async def run(self, run_id: str):
            started.append(run_id)

    app.state.research_agent_harness = FakeHarness()
    client = TestClient(app)

    invalid = client.post("/api/research-agent/runs", json={"symbol": "600664"})
    response = client.post(
        "/api/research-agent/runs",
        json={"symbol": "600664.sh", "name": " 哈药股份 ", "include_web_news": False},
    )

    assert invalid.status_code == 422
    assert response.status_code == 202
    run = response.json()["run"]
    assert run["symbol"] == "600664.SH"
    assert run["name"] == "哈药股份"
    assert started == [run["id"]]
    assert client.get(f"/api/research-agent/runs/{run['id']}").status_code == 200
    assert client.get("/api/research-agent/runs/missing").status_code == 404


def test_api_returns_capacity_error(monkeypatch):
    class FullStore:
        def create(self, **_kwargs):
            raise ResearchRunCapacityError("研究任务较多,请等待当前任务完成后再试")

    monkeypatch.setattr(research_api, "run_store", FullStore())
    app = FastAPI()
    app.include_router(research_api.router)
    client = TestClient(app)

    response = client.post(
        "/api/research-agent/runs",
        json={"symbol": "600664.SH", "name": "哈药股份"},
    )

    assert response.status_code == 429
    assert "研究任务较多" in response.json()["detail"]
