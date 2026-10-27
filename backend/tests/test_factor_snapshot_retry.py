"""#517 盘中快照读失败的有界重试与 API 降级。

盘中实时刷新 (默认 ~6s) 每轮换新 generation; factor 快照路径此前 attempts=1
读后校验失败直接抛、API 裸 500。锁定三层行为:
① service 层有界重试 (撞发布 → 重读新世代重跑, 耗尽上抛);
② API 层把耗尽的 EnrichedGenerationUnavailableError 映射为 400 中文指引。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as backtest_api
from app.backtest.factor import FactorBacktestService
from app.enriched_generation import EnrichedGenerationUnavailableError
from app.services import preferences


class _FakeEngine:
    """只做占位; _run 由 monkeypatch 接管。"""


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr(FactorBacktestService, "_SNAPSHOT_RETRY_SLEEP_S", 0.0)


def test_run_retries_once_on_generation_change(monkeypatch):
    svc = FactorBacktestService(_FakeEngine())
    calls = []

    def _fake_run(config, *, regime_by_date=None):
        calls.append(1)
        if len(calls) == 1:
            raise EnrichedGenerationUnavailableError("changed while reading")
        return "ok"

    monkeypatch.setattr(svc, "_run", _fake_run)
    assert svc.run(object()) == "ok"
    assert len(calls) == 2


def test_run_raises_after_attempts_exhausted(monkeypatch):
    svc = FactorBacktestService(_FakeEngine())
    calls = []

    def _fake_run(config, *, regime_by_date=None):
        calls.append(1)
        raise EnrichedGenerationUnavailableError("still changing")

    monkeypatch.setattr(svc, "_run", _fake_run)
    with pytest.raises(EnrichedGenerationUnavailableError):
        svc.run(object())
    assert len(calls) == FactorBacktestService._SNAPSHOT_MAX_ATTEMPTS


def test_run_batch_retries_on_generation_change(monkeypatch):
    svc = FactorBacktestService(_FakeEngine())
    calls = []

    def _fake_batch(config, *, regime_by_date=None):
        calls.append(1)
        if len(calls) < 3:
            raise EnrichedGenerationUnavailableError("changed")
        return "ok"

    monkeypatch.setattr(svc, "_run_batch", _fake_batch)
    assert svc.run_batch(object()) == "ok"
    assert len(calls) == 3


def _client(monkeypatch, tmp_path) -> TestClient:
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(preferences, "load", lambda: {})
    app = FastAPI()
    app.include_router(backtest_api.router)
    app.state.repo = None
    return TestClient(app)


def test_factor_run_api_maps_generation_error_to_400(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    def _boom(self, cfg):
        raise EnrichedGenerationUnavailableError("exhausted")

    monkeypatch.setattr(FactorBacktestService, "run", _boom)
    r = client.post("/api/backtest/factor/run", json={
        "factor_name": "momentum_5d", "start": "2026-01-01", "end": "2026-06-30",
    })
    assert r.status_code == 400
    assert "正在更新" in r.json()["detail"]


def test_factor_batch_api_maps_generation_error_to_400(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    def _boom(self, cfg):
        raise EnrichedGenerationUnavailableError("exhausted")

    monkeypatch.setattr(FactorBacktestService, "run_batch", _boom)
    r = client.post("/api/backtest/factor/batch", json={
        "factor_names": ["momentum_5d"], "start": "2026-01-01", "end": "2026-06-30",
    })
    assert r.status_code == 400
    assert "正在更新" in r.json()["detail"]
