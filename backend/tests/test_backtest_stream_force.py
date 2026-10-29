"""strategy/stream 的 force 参数 — 绕过同参数任务结果缓存。

SSE 回测按全参数 md5 作 job_key, 已完成任务保留 5 分钟内同 key 连接直接复用
结果 (缓存)。force=1 掺入 job_key 使其不命中缓存必然重算; cancel 从 qs 重算
同一 key, 必须同步解析 force, 否则强制重跑的任务取消不到。
"""
from __future__ import annotations

import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as bt
from app.api.backtest import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_force_changes_job_key_and_is_stable():
    args = ("ma_cross", None, "2024-01-01", "2024-12-31", "open_t+1", None, None,
            0.0002, 5.0, 10, 1.0, 1_000_000.0, "equal", None, None)
    cached = bt._make_job_key(*args)
    forced = bt._make_job_key(*args, force=True)
    assert cached != forced
    # 各自幂等: 同参数重复计算 key 稳定 (重连/取消依赖)
    assert bt._make_job_key(*args) == cached
    assert bt._make_job_key(*args, force=True) == forced
    # force=False 是默认值: 不显式传参与显式 False 同 key
    assert bt._make_job_key(*args, force=False) == cached


def test_cancel_resolves_force_key_from_qs(monkeypatch):
    """带 force=1 的任务, cancel 用带 force=1 的 qs 能取消到; 不带 force 的 qs 取消不到。

    手工往任务表塞一个未完成 job (key 按 force 计算), 走真实 cancel 端点的
    qs→key 解析路径, 不启动任何回测线程。
    """
    args = ("ma_cross", None, None, None, "open_t+1", None, None,
            0.0002, 5.0, 10, 1.0, 1_000_000.0, "equal", None, None)
    forced_key = bt._make_job_key(*args, force=True)
    job = bt._BacktestJob(forced_key)
    job.cancel_event = threading.Event()
    monkeypatch.setitem(bt._running_jobs, forced_key, job)

    qs_force = "strategy_id=ma_cross&force=1"
    resp = _client().post("/api/backtest/strategy/cancel", json={"qs": qs_force})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert job.cancel_event.is_set()

    # 不带 force 的 qs 算出的是缓存任务的 key, 取消不到 force 任务
    job2 = bt._BacktestJob(forced_key)
    job2.cancel_event = threading.Event()
    monkeypatch.setitem(bt._running_jobs, forced_key, job2)
    qs_plain = "strategy_id=ma_cross"
    resp2 = _client().post("/api/backtest/strategy/cancel", json={"qs": qs_plain})
    assert resp2.status_code == 200
    assert resp2.json()["ok"] is False
    assert not job2.cancel_event.is_set()


def test_cancel_resolves_minute_fill_and_regime_filter(monkeypatch):
    """minute_fill / regime_filter 掺在 job_key 里, cancel 必须同步解析, 否则取消不到。

    曾为预存 bug: stream 侧 key 含这两个参数, cancel 重算漏掉 —— 开了分钟K
    精确回测或市场环境过滤的任务停止按钮失效。
    """
    from urllib.parse import quote

    args = ("ma_cross", None, None, None, "open_t+1", None, None,
            0.0002, 5.0, 10, 1.0, 1_000_000.0, "equal", None, None)
    regime_json = '{"states":["bull","shake"]}'
    key = bt._make_job_key(*args, minute_fill=True, regime_filter=regime_json)
    job = bt._BacktestJob(key)
    job.cancel_event = threading.Event()
    monkeypatch.setitem(bt._running_jobs, key, job)

    qs = f"strategy_id=ma_cross&minute_fill=true&regime_filter={quote(regime_json)}"
    resp = _client().post("/api/backtest/strategy/cancel", json={"qs": qs})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert job.cancel_event.is_set()

    # 缺这两个参数的 qs 算出不同 key, 取消不到
    job2 = bt._BacktestJob(key)
    job2.cancel_event = threading.Event()
    monkeypatch.setitem(bt._running_jobs, key, job2)
    resp2 = _client().post("/api/backtest/strategy/cancel", json={"qs": "strategy_id=ma_cross"})
    assert resp2.status_code == 200
    assert resp2.json()["ok"] is False
    assert not job2.cancel_event.is_set()
