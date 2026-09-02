from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.stock_analysis as stock_analysis_api
from app.services.agent_reach_research import AgentReachResearchAdapter


def _app(tmp_path):
    app = FastAPI()
    app.include_router(stock_analysis_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return app


def test_stock_analysis_public_research_defaults_off():
    request = stock_analysis_api.AnalyzeRequest(symbol="600519.SH")

    assert request.name == ""
    assert request.public_research_enabled is False
    assert [channel.value for channel in request.public_research_channels] == ["twitter"]


def test_stock_analysis_routes_public_research_config_and_exposes_sanitized_health(
    monkeypatch,
    tmp_path,
):
    calls: dict[str, object] = {}

    def runner(args, _timeout):
        calls["doctor"] = args
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "twitter": {
                        "status": "ok",
                        "active_backend": "OpenCLI",
                        "message": "private help must not escape",
                    }
                }
            ),
            "",
        )

    adapter = AgentReachResearchAdapter(runner=runner)
    app = _app(tmp_path)
    app.state.agent_reach_research_adapter = adapter

    async def fake_analyze(repo, data_dir, symbol, focus, document_text, profile_id, **kwargs):
        calls["analyze"] = {
            "repo": repo,
            "data_dir": data_dir,
            "symbol": symbol,
            "focus": focus,
            "document_text": document_text,
            "profile_id": profile_id,
            **kwargs,
        }
        yield json.dumps(
            {
                "type": "meta",
                "public_research": {
                    "status": "available",
                    "scope": "single_stock_analysis",
                },
            }
        )
        yield json.dumps({"type": "done"})

    monkeypatch.setattr(stock_analysis_api, "analyze_stock_stream", fake_analyze)
    client = TestClient(app)

    health = client.get("/api/stock-analysis/public-research/health")
    assert health.status_code == 200
    assert health.json() == {
        "default_enabled": False,
        "supported_channels": ["twitter"],
        "health": {
            "twitter": {
                "status": "ok",
                "active_backend": "OpenCLI",
                "runtime_state": "not_exercised",
            }
        },
    }

    response = client.post(
        "/api/stock-analysis/analyze",
        json={
            "symbol": "600519.SH",
            "name": "贵州茅台",
            "focus": "消息面",
            "profile_id": "profile-test",
            "public_research_enabled": True,
            "public_research_channels": ["twitter"],
        },
    )
    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == ["meta", "done"]
    routed = calls["analyze"]
    assert routed["name"] == "贵州茅台"
    assert routed["public_research_enabled"] is True
    assert [channel.value for channel in routed["public_research_channels"]] == ["twitter"]
    assert routed["research_adapter"] is adapter
    assert calls["doctor"] == ["agent-reach", "doctor", "--json"]
    assert "private help" not in response.text
