"""结构化计划检查 API 测试 — feature flag / 流式顺序 / 取消 / list / read / export。

只覆盖 API 层: feature gate、NDJSON 事件顺序、取消注册清理、artifact list/read/export。
核心编排 (plan_check 模块) 由 P4CoreBackend 实现, 本测试用 fake 桩替代。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.trading_plans as tp
from app.services.ai_attempts import AttemptRegistry
from app.services.ai_structured import (
    AIUsage,
    AnalysisArtifact,
    AnalysisTraceNode,
)


# ── fake plan_check 模块 (核心由 P4CoreBackend 实现) ──────────
def _install_fake_plan_check(monkeypatch, *, status="ok", error=None):
    """注入一个假的 app.services.trading.plan_check 模块。"""
    purpose = "trading_plan_check"
    program_rules_version = "test-rules-v1"

    fake = types.ModuleType("app.services.trading.plan_check")
    fake.PURPOSE = purpose  # type: ignore[attr-defined]
    fake.PROGRAM_RULES_VERSION = program_rules_version  # type: ignore[attr-defined]
    fake.AnalysisGateResult = object  # type: ignore[attr-defined]
    fake.Stage1Diagnosis = object  # type: ignore[attr-defined]
    fake.Stage2PlanReview = object  # type: ignore[attr-defined]

    call_log: dict = {}

    async def run_plan_check(
        repo,
        data_dir,
        *,
        date,
        entry_id,
        profile_id=None,
        cancel_token=None,
        on_event=None,
        attempt_id=None,
        request_id=None,
        stage1_generate=None,
        stage2_generate=None,
        enable_continuity=False,
    ):
        call_log["kwargs"] = {
            "date": date,
            "entry_id": entry_id,
            "profile_id": profile_id,
            "attempt_id": attempt_id,
            "request_id": request_id,
            "enable_continuity": enable_continuity,
        }
        call_log["cancel_token"] = cancel_token
        # 模拟进度事件 (含应被过滤的 prompt 字段)
        if on_event:
            on_event("preflight", {"ok": True, "prompt": "SECRET-PROMPT", "messages": ["m"]})
            on_event("stage1_start", {"model": "gpt-4"})
        # 让出控制权, 使取消可生效
        for _ in range(3):
            await asyncio.sleep(0)
        artifact = _make_artifact(
            attempt_id=attempt_id or "att_x",
            request_id=request_id or "req_x",
            status=status,
        )
        return artifact

    fake.run_plan_check = run_plan_check  # type: ignore[attr-defined]

    def artifact_to_markdown(artifact):
        return f"# 计划检查报告\n\nattempt: {artifact.attempt_id}\nstatus: {artifact.status}\n"

    fake.artifact_to_markdown = artifact_to_markdown  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "app.services.trading.plan_check", fake)
    import app.services.trading as trading_pkg

    monkeypatch.setattr(trading_pkg, "plan_check", fake, raising=False)
    return fake, call_log


def _make_artifact(
    *,
    attempt_id="att_test",
    request_id="req_test",
    status="ok",
    symbol="000001.SZ",
    purpose="trading_plan_check",
):
    return AnalysisArtifact(
        id=f"art_{attempt_id}",
        attempt_id=attempt_id,
        request_id=request_id,
        purpose=purpose,
        status=status,
        symbol=symbol,
        result={
            "status": "review_ready" if status == "ok" else "no_action",
            "gate": {"mechanical": "proceed", "overall": "proceed"},
            "disclaimer": "本结果仅为输入充分性评估, 非交易信号。",
            "ai_meta": {},
        },
        trace=[
            AnalysisTraceNode(
                id="n1", kind="program_rule", label="机械门禁", status="pass", locked=True
            ),
        ],
        usage=AIUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
    )


def _client(monkeypatch, tmp_path):
    """构造带 trading_plans 路由的测试 client。"""
    registry = AttemptRegistry()
    monkeypatch.setattr(tp, "get_registry", lambda: registry)
    # 指向 tmp_path
    monkeypatch.setattr(tp.settings, "data_dir", tmp_path)
    app = FastAPI()
    app.include_router(tp.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app), registry


def _enable_feature(monkeypatch, on=True):
    import app.services.preferences as prefs

    monkeypatch.setattr(prefs, "get_structured_plan_check_enabled", lambda: on)


def _seed_plan(tmp_path, date="20260804", entries=None):
    """写一个已保存计划。"""
    from app.services.trading.plans import write_plan

    write_plan(
        tmp_path,
        date,
        {
            "entries": entries
            or [
                {
                    "id": "p1",
                    "symbol": "000001.SZ",
                    "action": "buy_new",
                    "trigger": "t",
                    "reason": "r",
                }
            ]
        },
    )


def _enable_ai(monkeypatch, available=True):
    monkeypatch.setattr("app.services.ai_provider.profile_configured", lambda pid=None: available)


# ════════════════════════════════════════════════════════════
# 1. Feature flag 关闭 → 403, 零 AI
# ════════════════════════════════════════════════════════════
def test_check_403_when_feature_off(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=False)
    _seed_plan(tmp_path)
    _fake, call_log = _install_fake_plan_check(monkeypatch)

    resp = client.post("/api/trading/plans/20260804/entries/p1/check")
    assert resp.status_code == 403
    assert "run_plan_check" not in call_log  # 零 AI


def test_list_403_when_feature_off(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=False)
    resp = client.get("/api/trading/plan-checks")
    assert resp.status_code == 403


def test_get_403_when_feature_off(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=False)
    resp = client.get("/api/trading/plan-checks/att_x")
    assert resp.status_code == 403


def test_export_403_when_feature_off(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=False)
    resp = client.get("/api/trading/plan-checks/att_x/export")
    assert resp.status_code == 403


# ════════════════════════════════════════════════════════════
# 2. AI profile 不可用 → 503
# ════════════════════════════════════════════════════════════
def test_check_503_when_ai_unavailable(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _enable_ai(monkeypatch, available=False)
    _seed_plan(tmp_path)
    _fake, call_log = _install_fake_plan_check(monkeypatch)

    resp = client.post("/api/trading/plans/20260804/entries/p1/check")
    assert resp.status_code == 503
    assert "run_plan_check" not in call_log


# ════════════════════════════════════════════════════════════
# 3. 缺计划/条目 → 404
# ════════════════════════════════════════════════════════════
def test_check_404_when_plan_missing(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _enable_ai(monkeypatch)
    _fake, call_log = _install_fake_plan_check(monkeypatch)

    resp = client.post("/api/trading/plans/20260804/entries/p1/check")
    assert resp.status_code == 404
    assert "run_plan_check" not in call_log


def test_check_404_when_entry_missing(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _enable_ai(monkeypatch)
    _seed_plan(tmp_path)  # 有计划但无 p99 条目
    _fake, call_log = _install_fake_plan_check(monkeypatch)

    resp = client.post("/api/trading/plans/20260804/entries/p99/check")
    assert resp.status_code == 404
    assert "run_plan_check" not in call_log


# ════════════════════════════════════════════════════════════
# 4. 流式事件顺序: meta → progress → result → done
# ════════════════════════════════════════════════════════════
def test_stream_event_order_and_no_prompt(monkeypatch, tmp_path):
    client, _registry = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _enable_ai(monkeypatch)
    _seed_plan(tmp_path)
    _fake, call_log = _install_fake_plan_check(monkeypatch)

    with client.stream("POST", "/api/trading/plans/20260804/entries/p1/check") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        attempt_header = resp.headers["x-ai-attempt-id"]
        request_header = resp.headers["x-ai-request-id"]
        lines = [ln for ln in resp.iter_lines() if ln]

    events = [json.loads(ln) for ln in lines]
    types_seq = [e["type"] for e in events]

    # 首事件必须是 meta
    assert types_seq[0] == "meta"
    meta = events[0]
    assert meta["attempt_id"] == attempt_header
    assert meta["request_id"] == request_header
    assert meta["date"] == "20260804"
    assert meta["entry_id"] == "p1"
    assert "prompt" not in meta

    # progress 事件不含 prompt/messages
    progress_events = [e for e in events if e["type"] == "progress"]
    assert len(progress_events) >= 1
    for pe in progress_events:
        assert "prompt" not in pe
        assert "messages" not in pe
        assert pe["kind"]  # 必须有 kind

    # result 事件
    result_events = [e for e in events if e["type"] == "result"]
    assert len(result_events) == 1
    assert result_events[0]["status"] == "ok"
    assert result_events[0]["result"]["status"] == "review_ready"

    # 末事件必须是 done
    assert types_seq[-1] == "done"
    assert events[-1]["attempt_id"] == attempt_header

    # overall IDs 贯穿
    assert call_log["kwargs"]["attempt_id"] == attempt_header
    assert call_log["kwargs"]["request_id"] == request_header


# ════════════════════════════════════════════════════════════
# 5. 取消注册清理
# ════════════════════════════════════════════════════════════
def test_registry_cleanup_after_stream(monkeypatch, tmp_path):
    client, registry = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _enable_ai(monkeypatch)
    _seed_plan(tmp_path)
    _fake, _ = _install_fake_plan_check(monkeypatch)

    with client.stream("POST", "/api/trading/plans/20260804/entries/p1/check") as resp:
        assert resp.status_code == 200
        attempt_id = resp.headers["x-ai-attempt-id"]
        list(resp.iter_lines())  # 消费完

    # 流结束后注册表应清理
    assert registry.get(attempt_id) is None


def test_cancel_token_propagated_and_registry_cleans(monkeypatch, tmp_path):
    """验证 cancel_token 被传给核心模块; 流结束后注册表清理 (含取消路径)。"""
    client, registry = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _enable_ai(monkeypatch)
    _seed_plan(tmp_path)
    _fake, call_log = _install_fake_plan_check(monkeypatch)

    # 正常完成路径: 验证 token 传递 + 注册表清理
    with client.stream("POST", "/api/trading/plans/20260804/entries/p1/check") as resp:
        assert resp.status_code == 200
        attempt_id = resp.headers["x-ai-attempt-id"]
        list(resp.iter_lines())

    assert call_log.get("cancel_token") is not None
    assert registry.get(attempt_id) is None
    # cancel 已失效 (流已结束), 幂等返回 False
    assert registry.cancel(attempt_id) is False


# ════════════════════════════════════════════════════════════
# 6. List — 过滤 & purpose 隔离
# ════════════════════════════════════════════════════════════
def test_list_filters_by_symbol(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _fake, _ = _install_fake_plan_check(monkeypatch)

    from app.services import analysis_artifacts

    analysis_artifacts.record(tmp_path, _make_artifact(attempt_id="att_a", symbol="000001.SZ"))
    analysis_artifacts.record(tmp_path, _make_artifact(attempt_id="att_b", symbol="600519.SH"))
    # 非 plan_check purpose 的 artifact 不应出现
    analysis_artifacts.record(
        tmp_path, _make_artifact(attempt_id="att_c", symbol="000001.SZ", purpose="other_purpose")
    )

    resp = client.get("/api/trading/plan-checks")
    assert resp.status_code == 200
    items = resp.json()["items"]
    ids = {i["attempt_id"] for i in items}
    assert ids == {"att_a", "att_b"}  # other_purpose 被排除

    # symbol 过滤
    resp2 = client.get("/api/trading/plan-checks?symbol=000001.sz")
    items2 = resp2.json()["items"]
    assert {i["attempt_id"] for i in items2} == {"att_a"}  # 大小写无关


def test_list_respects_limit(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _fake, _ = _install_fake_plan_check(monkeypatch)

    from app.services import analysis_artifacts

    analysis_artifacts.record(tmp_path, _make_artifact(attempt_id="att_a", symbol="000001.SZ"))
    analysis_artifacts.record(tmp_path, _make_artifact(attempt_id="att_b", symbol="000002.SZ"))

    resp = client.get("/api/trading/plan-checks?limit=1")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


# ════════════════════════════════════════════════════════════
# 7. Read — 路径安全 & purpose 隔离
# ════════════════════════════════════════════════════════════
def test_get_returns_safe_artifact(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _fake, _ = _install_fake_plan_check(monkeypatch)

    from app.services import analysis_artifacts

    analysis_artifacts.record(tmp_path, _make_artifact(attempt_id="att_a", symbol="000001.SZ"))

    resp = client.get("/api/trading/plan-checks/att_a")
    assert resp.status_code == 200
    body = resp.json()
    assert body["attempt_id"] == "att_a"
    assert body["purpose"] == "trading_plan_check"
    assert body["result"]["disclaimer"]


def test_get_404_for_wrong_purpose(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _fake, _ = _install_fake_plan_check(monkeypatch)

    from app.services import analysis_artifacts

    analysis_artifacts.record(tmp_path, _make_artifact(attempt_id="att_x", purpose="other_purpose"))

    resp = client.get("/api/trading/plan-checks/att_x")
    assert resp.status_code == 404


def test_get_404_for_missing(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    resp = client.get("/api/trading/plan-checks/att_nonexistent")
    assert resp.status_code == 404


def test_get_rejects_path_traversal(monkeypatch, tmp_path):
    """attempt_id 路径穿越由 analysis_artifacts 内部防御; API 层返回 404。"""
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    for bad in ("../../etc/passwd", "../foo", "a/b"):
        resp = client.get(f"/api/trading/plan-checks/{bad}")
        # 路径穿越字符被 analysis_artifacts 拒绝 → 404 或 400 (ArtifactError)
        assert resp.status_code in (404, 400)


# ════════════════════════════════════════════════════════════
# 8. Export — JSON & Markdown
# ════════════════════════════════════════════════════════════
def test_export_json(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _fake, _ = _install_fake_plan_check(monkeypatch)

    from app.services import analysis_artifacts

    analysis_artifacts.record(tmp_path, _make_artifact(attempt_id="att_a", symbol="000001.SZ"))

    resp = client.get("/api/trading/plan-checks/att_a/export?format=json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["attempt_id"] == "att_a"
    assert body["result"]["disclaimer"]


def test_export_markdown(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _fake, _ = _install_fake_plan_check(monkeypatch)

    from app.services import analysis_artifacts

    analysis_artifacts.record(tmp_path, _make_artifact(attempt_id="att_a", symbol="000001.SZ"))

    resp = client.get("/api/trading/plan-checks/att_a/export?format=markdown")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "attachment" in resp.headers["content-disposition"]
    assert "plan-check-att_a.md" in resp.headers["content-disposition"]
    body = resp.text
    assert "计划检查报告" in body
    assert "att_a" in body


def test_export_rejects_bad_format(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    _fake, _ = _install_fake_plan_check(monkeypatch)

    from app.services import analysis_artifacts

    analysis_artifacts.record(tmp_path, _make_artifact(attempt_id="att_a"))

    resp = client.get("/api/trading/plan-checks/att_a/export?format=xml")
    assert resp.status_code == 400


def test_export_404_missing(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _enable_feature(monkeypatch, on=True)
    resp = client.get("/api/trading/plan-checks/att_nonexistent/export")
    assert resp.status_code == 404


# ════════════════════════════════════════════════════════════
# 9. Preferences PUT & GET
# ════════════════════════════════════════════════════════════
def test_put_structured_plan_check_preference(monkeypatch, tmp_path):
    """直接测 preferences 服务函数 (API 路由由前端后续处理)。"""
    import app.services.preferences as prefs

    monkeypatch.setattr(prefs, "_path", lambda: tmp_path / "preferences.json")

    assert prefs.get_structured_plan_check_enabled() is False  # 默认关闭
    assert prefs.set_structured_plan_check_enabled(True) is True
    assert prefs.get_structured_plan_check_enabled() is True
    assert prefs.set_structured_plan_check_enabled(False) is False
    assert prefs.get_structured_plan_check_enabled() is False


def test_get_preferences_exposes_flag(monkeypatch):
    """GET /preferences 暴露 structured_plan_check_enabled。"""
    from app.api.settings import get_preferences

    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )
    monkeypatch.setattr("app.api.settings._realtime_allowed", lambda: False)
    _enable_feature(monkeypatch, on=False)
    out = get_preferences()
    assert out["structured_plan_check_enabled"] is False

    _enable_feature(monkeypatch, on=True)
    out = get_preferences()
    assert out["structured_plan_check_enabled"] is True
