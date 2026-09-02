from __future__ import annotations

import json
import shutil
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.agent import router as agent_router
from app.config import settings
from app.data_providers.base import ProviderCapabilities
from app.services import agent_runtime
from app.services.agent_reach_research import (
    PublicResearchBundle,
    PublicResearchEvidence,
)
from app.services.position_analysis_agent import PositionAnalysisService

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_WORKER = _PROJECT_ROOT / "pi-agent-worker" / "src" / "worker.js"
_NODE_MODULES = _PROJECT_ROOT / "pi-agent-worker" / "node_modules"
_RUNTIME_AVAILABLE = shutil.which("node") is not None and _WORKER.is_file() and _NODE_MODULES.is_dir()


class _ModelHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict]] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).requests.append(body)
        chunks = [
            {
                "id": "chatcmpl-position-analysis",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "runtime-e2e-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_position_analysis",
                                    "type": "function",
                                    "function": {
                                        "name": "analyze_position_snapshot",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-position-analysis",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "runtime-e2e-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        ]
        payload = "".join(
            f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks
        ) + "data: [DONE]\n\n"
        encoded = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        return


@contextmanager
def _model_server():
    _ModelHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", _ModelHandler.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _Provider:
    name = "runtime-e2e-local"
    capabilities = ProviderCapabilities(daily=True)

    def get_daily(self, symbols, _start, _end, _asset_type):
        return pl.DataFrame(
            [
                {
                    "symbol": symbol,
                    "date": "2026-08-28",
                    "close": 100.0,
                }
                for symbol in symbols
            ]
        )

    def get_moneyflow_status(self):
        return {"moneyflow_minute_stock": {"available": False}}


class _QuoteService:
    @staticmethod
    def status():
        return {"has_recent_data": True, "source_as_of": "2026-08-31"}

    @staticmethod
    def get_quotes_compat():
        return pl.DataFrame(
            [
                {
                    "symbol": "600519.SH",
                    "last_price": 102.0,
                    "open": 101.0,
                    "high": 103.0,
                    "low": 100.5,
                    "source": "runtime-e2e",
                }
            ]
        )


class _ResearchAdapter:
    def __init__(self, bundle: PublicResearchBundle) -> None:
        self.bundle = bundle
        self.subjects = []

    def fetch(self, subject, channels, *, scope):
        self.subjects.append((subject.model_dump(), channels, scope))
        return self.bundle


@pytest.mark.skipif(not _RUNTIME_AVAILABLE, reason="real Pi worker dependencies unavailable")
@pytest.mark.parametrize("research_available", [True, False])
def test_http_stream_runs_real_pi_worker_and_keeps_private_facts_out_of_model(
    monkeypatch,
    tmp_path,
    research_available,
):
    if research_available:
        bundle = PublicResearchBundle(
            status="available",
            subject_symbol="600519.SH",
            channels_requested=["twitter"],
            channels_used=["twitter"],
            retrieved_at="2026-08-31T02:00:00+00:00",
            evidence=[
                PublicResearchEvidence(
                    platform="twitter",
                    source="agent-reach:twitter:OpenCLI",
                    url="https://x.com/public/status/1",
                    author="public",
                    excerpt="runtime-e2e-public-context",
                    retrieved_at="2026-08-31T02:00:00+00:00",
                )
            ],
        )
    else:
        bundle = PublicResearchBundle(
            status="unavailable",
            subject_symbol="600519.SH",
            channels_requested=["twitter"],
            warnings=["twitter:TIMEOUT"],
            retrieved_at="2026-08-31T02:00:00+00:00",
        )
    research = _ResearchAdapter(bundle)
    provider = _Provider()
    service = PositionAnalysisService(
        holdings_fetcher=lambda: {
            "available": True,
            "positions": [
                {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "qty": 9876,
                    "costPrice": 731.234,
                    "marketValue": 1_007_352,
                }
            ],
        },
        provider_getter=lambda: provider,
        research_adapter=research,
    )
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_range=lambda *_args, **_kwargs: pl.DataFrame(),
    )
    app = FastAPI()
    app.include_router(agent_router)
    app.state.repo = repo
    app.state.quote_service = _QuoteService()
    app.state.position_analysis_service = service

    monkeypatch.setattr(settings, "agent_pi_worker_path", str(_WORKER))
    monkeypatch.setattr(settings, "agent_pi_node_command", "node")
    monkeypatch.setattr(settings, "agent_pi_ready_timeout_s", 5.0)
    monkeypatch.setattr(settings, "agent_pi_response_timeout_s", 10.0)

    with _model_server() as (base_url, model_requests):
        profile = {
            "provider": "openai_compat",
            "base_url": base_url,
            "api_key": "sk-runtime-e2e-secret",
            "model": "runtime-e2e-model",
            "context_window": 128_000,
            "max_tokens": 1_600,
            "temperature": 0.0,
            "thinking_level": "off",
        }
        monkeypatch.setattr(
            agent_runtime,
            "_resolve_pi_profile",
            lambda _profile_id: (profile, profile["api_key"]),
        )
        response = TestClient(app).post(
            "/api/agent/position-analysis/stream",
            json={
                "public_research_enabled": True,
                "public_research_channels": ["twitter"],
            },
        )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == ["delta", "done"]
    markdown = events[0]["content"]
    assert "持仓盯盘" in markdown
    if research_available:
        assert "[UNVERIFIED]" in markdown
        assert "runtime-e2e-public-context" in markdown
    else:
        assert "unavailable（twitter:TIMEOUT）" in markdown
    assert research.subjects[0][0] == {"symbol": "600519.SH", "name": "贵州茅台"}
    assert research.subjects[0][2] == "primary_position_only"

    assert len(model_requests) == 1
    model_payload = json.dumps(model_requests[0], ensure_ascii=False)
    assert "analyze_position_snapshot" in model_payload
    assert "9876" not in model_payload
    assert "731.234" not in model_payload
    assert "runtime-e2e-public-context" not in model_payload
    assert "costPrice" not in model_payload
    assert "positions" not in model_payload
    assert "9876" not in response.text
    assert "731.234" not in response.text
