"""F11 回测定时复跑 — 偏好端点 + job 核心逻辑测试。

- 偏好: 默认值 / POST-GET 往返 / 非法字段 422 / 保存即 reschedule job。
- job 逻辑: 零收藏 no-op / 写新 Run (label='定时复跑' + source_run_id +
  滚动窗区间) / 单失败不阻塞其余 / 只取收藏的策略 Run 且按创建时间倒序前 10。

execute (service 路径) 全部 stub 注入, store 落 tmp_path;
不跑真实回测、不读行情、不写真实 data/。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import settings as settings_api
from app.backtest.run_store import BacktestRun, BacktestRunStore, RunSubject
from app.jobs.backtest_favorite_rerun import (
    RERUN_LABEL,
    run_backtest_favorite_rerun_job,
    run_favorite_reruns,
)
from app.services import preferences

# ── 测试基建 ─────────────────────────────────────────────

class _FakeScheduler:
    """记录 add_job/remove_job 调用, 不真正调度。"""

    def __init__(self) -> None:
        self.added: list[str] = []
        self.removed: list[str] = []

    def add_job(self, fn, **kwargs):
        self.added.append(kwargs.get("id"))

    def remove_job(self, job_id):
        self.removed.append(job_id)


def _client(tmp_path: Path, monkeypatch, scheduler=None) -> TestClient:
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    app = FastAPI()
    app.include_router(settings_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    if scheduler is not None:
        app.state.scheduler = scheduler
    return TestClient(app)


def _make_run(run_id: str = "favrun00001", **overrides) -> BacktestRun:
    defaults = dict(
        run_id=run_id,
        kind="strategy",
        created_at="2026-08-01T00:00:00+00:00",
        subject=RunSubject(id="macd", name="MACD", hash="h1"),
        config={
            "strategy_id": "macd", "symbols": ["600000.SH"],
            "start": "2026-01-01", "end": "2026-06-30",
            "fees_pct": 0.0002, "slippage_bps": 5.0, "matching": "open_t+1",
            "params": {"fast": 12, "slow": 26},
        },
        data_snapshot={"snapshot_hash": "snap-1"},
        stats={"sharpe": 1.5, "total_return": 0.2},
        equity_curve=[{"date": "2026-01-02", "equity": 1.01}],
        trades=[{"symbol": "600000.SH", "pnl": 100.0}],
        favorite=True,
    )
    defaults.update(overrides)
    return BacktestRun(**defaults)


def _stub_payload(cfg: dict) -> dict:
    """service 路径的 stub 返回 — 形状对齐 StrategyBacktestService.run 的 payload。"""
    return {
        "run_id": "svcstub000001",
        "config": dict(cfg),
        "strategy_info": {"id": cfg.get("strategy_id"), "name": "MACD", "source": "builtin"},
        "stats": {"sharpe": 1.2, "total_return": 0.1},
        "equity_curve": [{"date": "2026-08-19", "equity": 1.01}],
        "drawdown_curve": [],
        "benchmark_curve": [],
        "trades": [],
        "per_symbol_stats": [],
        "warnings": [],
        "data_snapshot": {"snapshot_hash": "snap-new"},
    }


def _ok_execute(captured: list):
    def execute(run: BacktestRun) -> tuple[dict, str]:
        captured.append({
            "run_id": run.run_id,
            "start": run.config.get("start"),
            "end": run.config.get("end"),
            "params": run.config.get("params"),
        })
        return _stub_payload(run.config), "strategy"
    return execute


# ── 偏好端点 ─────────────────────────────────────────────

def test_get_defaults(tmp_path, monkeypatch):
    resp = _client(tmp_path, monkeypatch).get("/api/settings/preferences/backtest-auto-rerun")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "hour": 16, "minute": 40, "window_days": 90}


def test_post_roundtrip(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.post(
        "/api/settings/preferences/backtest-auto-rerun",
        json={"enabled": True, "hour": 17, "minute": 5, "window_days": 120},
    )
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "hour": 17, "minute": 5, "window_days": 120}
    # GET 读回持久化结果 (往返)
    again = client.get("/api/settings/preferences/backtest-auto-rerun").json()
    assert again == {"enabled": True, "hour": 17, "minute": 5, "window_days": 120}


def test_invalid_hour_rejected_422(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post(
        "/api/settings/preferences/backtest-auto-rerun",
        json={"enabled": True, "hour": 24, "minute": 40, "window_days": 90},
    ).status_code == 422
    # 窗口天数越界同样 422
    assert client.post(
        "/api/settings/preferences/backtest-auto-rerun",
        json={"enabled": True, "hour": 16, "minute": 40, "window_days": 10},
    ).status_code == 422


def test_save_reschedules_job(tmp_path, monkeypatch):
    sched = _FakeScheduler()
    client = _client(tmp_path, monkeypatch, scheduler=sched)
    # 开启 → 注册 job
    client.post(
        "/api/settings/preferences/backtest-auto-rerun",
        json={"enabled": True, "hour": 18, "minute": 0, "window_days": 90},
    )
    assert sched.added == ["backtest_favorite_rerun"]
    # 关闭 → 移除 job
    client.post(
        "/api/settings/preferences/backtest-auto-rerun",
        json={"enabled": False, "hour": 18, "minute": 0, "window_days": 90},
    )
    assert sched.removed == ["backtest_favorite_rerun"]


# ── job 核心逻辑 ─────────────────────────────────────────

def test_job_noop_without_favorites(tmp_path):
    store = BacktestRunStore(tmp_path)
    captured: list = []
    result = run_favorite_reruns(store, execute=_ok_execute(captured))
    assert result == {"total": 0, "success": 0, "failed": 0, "failed_run_ids": []}
    assert captured == []  # execute 从未被调用
    assert store.list_runs()["total"] == 0


def test_job_writes_new_run_with_label_and_source(tmp_path):
    store = BacktestRunStore(tmp_path)
    store.save(_make_run("favrun00001"))
    captured: list = []
    result = run_favorite_reruns(
        store,
        execute=_ok_execute(captured),
        today=date(2026, 8, 20),
        window_days=90,
    )
    assert result == {"total": 1, "success": 1, "failed": 0, "failed_run_ids": []}
    # execute 收到的是滚动窗区间覆盖、其余参数照快照的 config
    assert captured == [{
        "run_id": "favrun00001",
        "start": "2026-05-22",  # 2026-08-20 - 90 天
        "end": "2026-08-20",
        "params": {"fast": 12, "slow": 26},
    }]
    items = store.list_runs()["items"]
    assert len(items) == 2
    new = next(r for r in items if r["run_id"] != "favrun00001")
    assert new["label"] == RERUN_LABEL
    assert new["source_run_id"] == "favrun00001"
    assert new["kind"] == "strategy"
    full = store.get(new["run_id"])
    # 快照变化 → 显式警告 (与手动复跑端点同款)
    assert any(w.startswith("rerun_data_snapshot_changed") for w in full.warnings)


def test_job_continues_after_single_failure(tmp_path):
    store = BacktestRunStore(tmp_path)
    for i in (1, 2, 3):
        store.save(_make_run(f"favrun0000{i}", created_at=f"2026-08-0{i}T00:00:00+00:00"))
    calls: list[str] = []

    def flaky_execute(run: BacktestRun) -> tuple[dict, str]:
        calls.append(run.run_id)
        if run.run_id == "favrun00002":
            raise RuntimeError("引擎爆炸")
        return _stub_payload(run.config), "strategy"

    result = run_favorite_reruns(store, execute=flaky_execute, today=date(2026, 8, 20))
    assert result["total"] == 3
    assert result["success"] == 2
    assert result["failed"] == 1
    assert result["failed_run_ids"] == ["favrun00002"]
    # 失败不阻塞: 三个都被尝试, 两个新 Run 落盘
    assert calls == ["favrun00003", "favrun00002", "favrun00001"]  # 创建时间倒序
    new_runs = [r for r in store.list_runs()["items"] if r["source_run_id"]]
    assert {r["source_run_id"] for r in new_runs} == {"favrun00001", "favrun00003"}


def test_job_only_favorite_strategy_runs_top10_newest(tmp_path):
    store = BacktestRunStore(tmp_path)
    # 12 个收藏策略 Run (创建时间递增, 应取最新 10 个)
    for day in range(1, 13):
        store.save(_make_run(
            f"favstrat{day:06d}",
            created_at=f"2026-08-{day:02d}T00:00:00+00:00",
        ))
    # 干扰项: 收藏的因子 Run 与未收藏的策略 Run — 都不应被复跑
    store.save(_make_run("favfactor001", kind="factor", favorite=True))
    store.save(_make_run("plainrun0001", favorite=False))
    captured: list = []
    result = run_favorite_reruns(store, execute=_ok_execute(captured), today=date(2026, 8, 20))
    assert result["total"] == 10
    assert [c["run_id"] for c in captured] == [f"favstrat{d:06d}" for d in range(12, 2, -1)]


def test_job_entry_disabled_pref_is_zero_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    store = BacktestRunStore(tmp_path)
    store.save(_make_run("favrun00001"))
    result = run_backtest_favorite_rerun_job(repo)
    # 开关默认关闭: 直接返回, 不触碰 store (无新 Run)
    assert result["skipped"] == "disabled"
    assert store.list_runs()["total"] == 1
