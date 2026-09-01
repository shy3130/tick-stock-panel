from __future__ import annotations

import json
import subprocess

from app.services import agent_reach_research as research
from app.services.agent_reach_research import (
    AgentReachChannel,
    AgentReachResearchAdapter,
    PublicResearchSubject,
)


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_twitter_is_doctor_routed_cached_and_only_receives_public_identity():
    calls: list[list[str]] = []

    def runner(args: list[str], timeout: float):
        calls.append(args)
        assert timeout > 0
        if args[:2] == ["agent-reach", "doctor"]:
            return completed(
                args,
                stdout=json.dumps(
                    {
                        "twitter": {
                            "status": "ok",
                            "active_backend": "OpenCLI",
                            "message": "secret help text must not escape",
                        }
                    }
                ),
            )
        return completed(
            args,
            stdout=json.dumps(
                [
                    {
                        "author": "public-user",
                        "text": "公开讨论，不是投资依据",
                        "created_at": "2026-08-31T01:00:00Z",
                        "url": "https://x.com/public-user/status/1",
                    },
                    {
                        "author": "bad",
                        "text": "unsafe url must be dropped",
                        "url": "javascript:alert(1)",
                    },
                ]
            ),
        )

    adapter = AgentReachResearchAdapter(runner=runner)
    subject = PublicResearchSubject(symbol="600519.SH", name="贵州茅台 OR from:attacker")
    first = adapter.fetch(
        subject,
        (AgentReachChannel.TWITTER,),
        scope="primary_position_only",
    )
    second = adapter.fetch(
        subject,
        (AgentReachChannel.TWITTER,),
        scope="primary_position_only",
    )
    stock_analysis = adapter.fetch(
        subject,
        (AgentReachChannel.TWITTER,),
        scope="single_stock_analysis",
    )

    assert first == second
    assert first.scope == "primary_position_only"
    assert stock_analysis.scope == "single_stock_analysis"
    assert first.status == "available"
    assert first.channels_used == ["twitter"]
    assert len(first.evidence) == 1
    assert first.evidence[0].evidence_grade == "C"
    assert first.evidence[0].unverified is True
    assert len(calls) == 3
    search = calls[1]
    assert search[:3] == ["opencli", "twitter", "search"]
    assert "贵州茅台ORfromattacker" in search[3]
    assert "quantity" not in " ".join(search)
    assert "cost" not in " ".join(search)
    assert "position_ratio" not in " ".join(search)


def test_command_runner_allowlists_environment(monkeypatch):
    captured = {}
    monkeypatch.setenv("AI_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("HOME", "/tmp/test-home")
    monkeypatch.setattr(
        research.shutil,
        "which",
        lambda name: f"/safe/bin/{name}",
    )

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return completed(args, stdout="{}")

    monkeypatch.setattr(research.subprocess, "run", fake_run)
    result = research._run_command(
        ["agent-reach", "doctor", "--json"],
        1.0,
    )

    assert result.returncode == 0
    assert captured["args"][0] == "/safe/bin/agent-reach"
    assert captured["kwargs"]["env"]["HOME"] == "/tmp/test-home"
    assert "AI_API_KEY" not in captured["kwargs"]["env"]
    assert "shell" not in captured["kwargs"]


def test_unhealthy_doctor_never_calls_platform_backend_and_health_is_redacted():
    calls: list[list[str]] = []

    def runner(args: list[str], _timeout: float):
        calls.append(args)
        return completed(
            args,
            stdout=json.dumps(
                {
                    "twitter": {
                        "status": "warn",
                        "active_backend": None,
                        "message": "cookie=/private/secret",
                    }
                }
            ),
        )

    adapter = AgentReachResearchAdapter(runner=runner)
    result = adapter.fetch(
        PublicResearchSubject(symbol="000001.SZ", name="平安银行"),
        (AgentReachChannel.TWITTER,),
        scope="single_stock_analysis",
    )

    assert result.status == "unavailable"
    assert result.warnings == ["twitter:backend_unavailable"]
    assert calls == [["agent-reach", "doctor", "--json"]]
    assert adapter.health() == {
        "twitter": {
            "status": "warn",
            "active_backend": None,
            "runtime_state": "recent_failure",
        }
    }
    assert "cookie" not in json.dumps(adapter.health())


def test_backend_failures_open_circuit_without_exposing_raw_error():
    calls: list[list[str]] = []

    def runner(args: list[str], _timeout: float):
        calls.append(args)
        if args[:2] == ["agent-reach", "doctor"]:
            return completed(
                args,
                stdout=json.dumps(
                    {"twitter": {"status": "ok", "active_backend": "OpenCLI"}}
                ),
            )
        return completed(
            args,
            returncode=69,
            stdout=json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "BROWSER_CONNECT",
                        "message": "private browser path /Users/example",
                    },
                }
            ),
        )

    adapter = AgentReachResearchAdapter(runner=runner)
    for code in ("000001.SZ", "000002.SZ", "000003.SZ"):
        result = adapter.fetch(
            PublicResearchSubject(symbol=code, name=code),
            (AgentReachChannel.TWITTER,),
            scope="primary_position_only",
        )
        assert result.warnings == ["twitter:BROWSER_CONNECT"]

    circuit = adapter.fetch(
        PublicResearchSubject(symbol="000004.SZ", name="000004"),
        (AgentReachChannel.TWITTER,),
        scope="primary_position_only",
    )
    assert circuit.warnings == ["twitter:circuit_open"]
    assert len(calls) == 4
    assert "/Users/example" not in json.dumps(circuit.model_dump())
