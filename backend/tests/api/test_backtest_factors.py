from types import SimpleNamespace

import pytest
from fastapi import HTTPException

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
