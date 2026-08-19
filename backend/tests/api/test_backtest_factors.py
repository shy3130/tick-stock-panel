import json
import asyncio
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError


from app.api import backtest


def test_factor_manifest_does_not_need_engine():
    out = backtest.factor_manifest()

    assert len(out["factors"]) >= 10
    assert out["factors"][0]["id"].startswith("alpha101_")


def test_compare_rejects_unknown_factor_before_engine():
    req = backtest.FactorCompareRequest(factor_ids=["alpha101_missing"])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(HTTPException) as exc:
        backtest.factor_compare(req, request)

    assert exc.value.status_code == 400
    assert "unknown factor" in exc.value.detail


def test_factor_request_n_groups_bounded():
    """n_groups 必须有界 [2,10], 越界在请求校验阶段被 422 拒绝, 不进入引擎。"""
    assert backtest.FactorBacktestRequest(factor_name="alpha101_test", n_groups=10).n_groups == 10
    with pytest.raises(ValidationError):
        backtest.FactorBacktestRequest(factor_name="alpha101_test", n_groups=1)
    with pytest.raises(ValidationError):
        backtest.FactorBacktestRequest(factor_name="alpha101_test", n_groups=11)


def test_attach_methodology_adds_backtest_context():
    out = backtest._attach_methodology({"ok": True}, "backtest")

    assert out["warnings"] == []
    assert "回测诊断" in out["methodology_context"]


def test_attach_methodology_failure_warns(monkeypatch):
    def fail_safe_loader(scenario, max_chars=12_000, warnings=None):
        if warnings is not None:
            warnings.append(f"方法论库加载失败: {scenario}")
        return ""

    monkeypatch.setattr("app.services.skill_context.load_skill_context_safe", fail_safe_loader)
    out = backtest._attach_methodology({"ok": True}, "backtest")

    assert "methodology_context" not in out
    assert out["warnings"] == ["方法论库加载失败: backtest"]

def test_factor_config_uses_factor_default_window():
    """因子入口缺省区间是 180 天，不能误用策略的三年默认值。"""
    cfg = backtest._build_factor_config(
        factor_name="alpha101_001",
        symbols=None,
        start=None,
        end=date(2026, 8, 1),
        start_is_all_history=False,
        n_groups=5,
        rebalance="monthly",
        weight="equal",
        fees_pct=0.0002,
        slippage_bps=5.0,
        risk_free_rate=0.0,
    )

    assert cfg.start == date(2026, 2, 2)
    assert cfg.end == date(2026, 8, 1)


def test_factor_cancel_uses_normalized_stream_key(monkeypatch):
    """取消端点必须从冻结 query 还原与 SSE 完全相同的任务键。"""
    job_key = backtest._make_factor_job_key(
        "alpha101_001",
        "600000.SH",
        "2026-01-01",
        "2026-03-31",
        False,
        5,
        "daily",
        "equal",
        0.0002,
        5.0,
        0.0,
    )
    job = backtest._BacktestJob(job_key)
    monkeypatch.setattr(backtest, "_running_jobs", {job_key: job})

    class _Request:
        async def json(self):
            return {
                "qs": (
                    "factor_name=alpha101_001&symbols=600000.SH&start=2026-01-01"
                    "&end=2026-03-31&n_groups=5&rebalance=daily&weight=equal"
                    "&fees_pct=0.0002&slippage_bps=5&risk_free_rate=0"
                )
            }

    out = asyncio.run(backtest.factor_cancel(_Request()))

    assert out == {"ok": True}
    assert job.cancel_event.is_set()


def test_factor_stream_replays_progress_before_done(monkeypatch):
    """已完成任务的新订阅者也必须先拿到缓冲进度，再收到 done。"""
    monkeypatch.setattr(backtest, "_running_jobs", {})
    monkeypatch.setattr(backtest.settings, "backtest_range_guard", False)
    monkeypatch.setattr(backtest, "_get_engine", lambda _request: object())
    monkeypatch.setattr(
        backtest,
        "_factor_stream_done_event",
        lambda _request, _result: "event: done\ndata: {\"run_id\":\"factor-stream\"}\n\n",
    )

    class _FactorService:
        def __init__(self, _engine):
            pass

        def run(self, _config, progress_cb, _cancel_event):
            progress_cb({"stage": "ic", "label": "计算截面 IC", "completed": 1, "total": 1})
            return object()

    monkeypatch.setattr("app.backtest.factor.FactorBacktestService", _FactorService)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
        query_params={"start": "2026-01-01"},
    )

    async def _not_disconnected():
        return False

    request.is_disconnected = _not_disconnected
    response = asyncio.run(backtest.factor_stream(
        request,
        factor_name="alpha101_001",
        start="2026-01-01",
        end="2026-01-02",
    ))

    async def _collect() -> str:
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    stream = asyncio.run(_collect())
    assert stream.index("event: progress") < stream.index("event: done")
    assert '"completed": 1' in stream


def test_factor_stream_guard_does_not_create_ghost_job(monkeypatch):
    """被范围保护拒绝后，相同 query 不能遗留一个永不结束的任务。"""
    jobs: dict[str, object] = {}
    monkeypatch.setattr(backtest, "_running_jobs", jobs)
    monkeypatch.setattr(backtest.settings, "backtest_range_guard", True)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
        query_params={"start": "1900-01-01"},
    )
    response = asyncio.run(backtest.factor_stream(
        request,
        factor_name="alpha101_001",
        start="1900-01-01",
        end="2026-01-01",
    ))

    async def _collect() -> str:
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    stream = asyncio.run(_collect())
    assert "event: error" in stream
    assert backtest.BACKTEST_SERVER_GUARD_MESSAGE in stream
    assert jobs == {}


def test_factor_stream_done_event_exposes_persistence_failure(monkeypatch):
    """因子 SSE 与策略 SSE 一样，持久化失败必须在 done payload 中可见。"""
    monkeypatch.setattr(
        backtest,
        "_attach_run_provenance",
        lambda payload, *_args, **_kwargs: payload,
    )
    monkeypatch.setattr(
        backtest,
        "_attach_methodology",
        lambda payload, *_args, **_kwargs: payload,
    )
    monkeypatch.setattr(backtest, "_save_backtest_run", lambda *_args: None)

    @dataclass
    class _Result:
        run_id: str = "factor-persist-failed"
        config: dict | None = None
        ic_mean: float = 0.04
        error: str | None = None

    event = backtest._factor_stream_done_event(
        SimpleNamespace(),
        _Result(
            config={
                "factor_name": "alpha101_001",
                "start": "2026-01-01",
                "end": "2026-03-31",
                "rebalance": "daily",
                "fees_pct": 0.0002,
                "slippage_bps": 5.0,
            },
        ),
    )
    payload = json.loads(event.split("data: ", 1)[1])
    assert payload["persisted"] is False
    assert any(warning.startswith("persistence_failed:") for warning in payload["warnings"])
