"""#516 内存保护 — /health 可观测性、重任务内存准入、worker RSS 上限配置。"""
from __future__ import annotations

import threading

import psutil
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes as routes_api
from app.services import heavy_job_limiter as hjl


class _VM:
    def __init__(self, available: int):
        self.available = available


# ── /health 内存块 ────────────────────────────────────────────────────


def test_health_reports_memory_snapshot():
    app = FastAPI()
    app.include_router(routes_api.router)
    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    mem = body["memory"]
    assert mem["rss_bytes"] > 0
    assert mem["system_total_bytes"] > 0
    assert mem["system_available_bytes"] >= 0
    assert 0 <= mem["system_used_pct"] <= 100


# ── 重任务内存准入 (psutil 在 _memory_admission_ok 内函数级 import,
#    patch 真模块的 virtual_memory) ───────────────────────────────────


def test_memory_admission_blocks_and_recovers(monkeypatch):
    monkeypatch.setattr(hjl, "_HEAVY_MIN_AVAILABLE_BYTES", 1024**3)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM(512 * 1024**2))
    limiter = hjl.HeavyJobLimiter(capacity=2)
    # 内存不足: 有空位也不放行, 超时返回 False
    assert limiter.acquire("normal", timeout=0.2) is False
    # 内存恢复 (轮询重查, 无需 notify): 正常获准
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM(4 * 1024**3))
    assert limiter.acquire("normal", timeout=5.0) is True
    limiter.release("normal")


def test_memory_admission_disabled(monkeypatch):
    monkeypatch.setattr(hjl, "_HEAVY_MIN_AVAILABLE_BYTES", 0)
    limiter = hjl.HeavyJobLimiter(capacity=1)
    assert limiter.acquire("exclusive", timeout=0.1) is True
    limiter.release("exclusive")


def test_memory_gate_keeps_cancel_semantics(monkeypatch):
    monkeypatch.setattr(hjl, "_HEAVY_MIN_AVAILABLE_BYTES", 1024**3)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM(0))
    limiter = hjl.HeavyJobLimiter(capacity=1)
    cancel = threading.Event()
    t = threading.Timer(0.1, cancel.set)
    t.start()
    assert limiter.acquire("normal", timeout=5.0, cancel_event=cancel) is False
    t.join()


# ── worker RSS 上限配置解析 ──────────────────────────────────────────


def test_worker_rss_limit_env_parsing(monkeypatch):
    from app.backtest import worker

    monkeypatch.setenv("TICK_WORKER_RSS_LIMIT_BYTES", "0")
    assert worker._env_int("TICK_WORKER_RSS_LIMIT_BYTES", 123) == 0
    monkeypatch.setenv("TICK_WORKER_RSS_LIMIT_BYTES", "2147483648")
    assert worker._env_int("TICK_WORKER_RSS_LIMIT_BYTES", 123) == 2147483648
    monkeypatch.delenv("TICK_WORKER_RSS_LIMIT_BYTES")
    assert worker._env_int("TICK_WORKER_RSS_LIMIT_BYTES", 123) == 123
    monkeypatch.setenv("TICK_WORKER_RSS_LIMIT_BYTES", "not-a-number")
    assert worker._env_int("TICK_WORKER_RSS_LIMIT_BYTES", 123) == 123


def test_process_rss_bytes_for_invalid_pid():
    from app.backtest.worker import _process_rss_bytes

    assert _process_rss_bytes(None) == 0
    assert _process_rss_bytes(999999999) == 0
