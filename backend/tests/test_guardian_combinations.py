"""P2d 组合扫描/创建 + P3 名额门槛与实验室战报测试。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.backtest.guardian_runtime import run_guardian_runtime
from app.services.guardian_combinations import create_guardian_composite
from app.services.guardian_store import (
    GuardianStore,
    read_combinations,
    write_combinations,
)


def _write_partitions(data_dir: Path, dates: list[date]) -> None:
    for value in dates:
        partition = data_dir / "kline_daily_enriched" / f"date={value.isoformat()}"
        partition.mkdir(parents=True, exist_ok=True)
        (partition / "part.parquet").write_bytes(b"")


class _CurveService:
    """按策略返回构造的日净值曲线; returns_patterns: {sid: [日收益循环模式]}。"""

    def __init__(self, patterns: dict[str, list[float]], dates: list[str]) -> None:
        self.patterns = patterns
        self.dates = dates
        self.calls: list[str] = []

    def run(self, config, cancel_event=None, prepared=None, result_policy=None):
        self.calls.append(config.strategy_id)
        pattern = self.patterns.get(config.strategy_id)
        if pattern is None:
            return SimpleNamespace(equity_curve=[], error=None, stats={})
        value = 100.0
        curve = [{"date": self.dates[0], "value": value}]
        for i, day in enumerate(self.dates[1:]):
            value *= 1.0 + pattern[i % len(pattern)]
            curve.append({"date": day, "value": round(value, 4)})
        return SimpleNamespace(equity_curve=curve, error=None, stats={})


class _Engine:
    def __init__(self, known: set[str]) -> None:
        self.known = known

    def get(self, strategy_id):
        if strategy_id not in self.known:
            raise ValueError(f"unknown strategy: {strategy_id}")
        return SimpleNamespace(
            meta={"asset_types": ["stock"], "name": strategy_id, "params": []},
        )

    def resolve_params(self, strategy, params=None, overrides=None):
        return {}


def _seed_healthy(store: GuardianStore, *strategy_ids: str) -> None:
    for sid in strategy_ids:
        store.create_baseline(
            sid,
            kind="mining_gate",
            data_as_of="2026-01-01",
            objective="sharpe",
            metrics={"score": 1.0, "n_trades": 100, "max_drawdown": -0.1},
        )
        store.append_check(sid, {"verdict": "healthy"})
        store.set_status(sid, "healthy")


def test_combination_scan_finds_low_corr_pair(tmp_path):
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(130)]
    _write_partitions(tmp_path, dates)
    day_strs = [d.isoformat() for d in dates[-41:]]
    store = GuardianStore(tmp_path)
    _seed_healthy(store, "mined_a", "mined_b", "mined_c")
    # a 与 b 完全正相关 (同模式), c 与 a 负相关 → 只建议 (a,c) / (b,c)
    service = _CurveService(
        {
            "mined_a": [0.01, 0.02],
            "mined_b": [0.01, 0.02],
            "mined_c": [0.03, -0.01],
        },
        day_strs,
    )

    result = run_guardian_runtime(
        {"mode": "combinations", "run_id": "r1", "strategy_ids": None,
         "thresholds": {}},
        data_dir=tmp_path,
        service=service,
        strategy_engine=_Engine({"mined_a", "mined_b", "mined_c"}),
    )

    assert result["mode"] == "combinations"
    assert result["healthy_count"] == 3
    assert result["pairs_evaluated"] == 3
    suggestions = read_combinations(tmp_path)["suggestions"]
    pairs = {frozenset((s["a"], s["b"])) for s in suggestions}
    assert pairs == {frozenset(("mined_a", "mined_c")),
                     frozenset(("mined_b", "mined_c"))}
    for item in suggestions:
        assert item["corr"] <= 0.70
        assert item["common_days"] >= 30


def test_combination_scan_needs_two_healthy(tmp_path):
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(40)]
    _write_partitions(tmp_path, dates)
    store = GuardianStore(tmp_path)
    _seed_healthy(store, "mined_a")

    result = run_guardian_runtime(
        {"mode": "combinations", "run_id": "r1", "strategy_ids": None,
         "thresholds": {}},
        data_dir=tmp_path, service=_CurveService({}, []), strategy_engine=_Engine(set()),
    )
    assert result["pairs_evaluated"] == 0
    assert read_combinations(tmp_path) is None


def test_create_composite_requires_known_suggestion(tmp_path):
    from app.strategy.engine import StrategyEngine

    write_combinations(
        tmp_path, window_start="2026-01-01", window_end="2026-06-01",
        suggestions=[{"a": "x", "b": "y", "corr": 0.2, "common_days": 40}],
        pairs_evaluated=1,
    )
    engine = StrategyEngine(strategy_dirs=[tmp_path / "strategies" / "composite"])
    with pytest.raises(ValueError, match="不在最新组合建议中"):
        create_guardian_composite(
            tmp_path, engine, left="x", right="z",
            latest_enriched=date(2026, 6, 1),
        )
    with pytest.raises(ValueError):
        create_guardian_composite(
            tmp_path, engine, left="x", right="y", latest_enriched=None
        )


def test_create_composite_happy_path_and_idempotent(tmp_path):
    from app.strategy.engine import StrategyEngine

    builtin = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "strategies"
    engine = StrategyEngine(
        strategy_dirs=[builtin, tmp_path / "strategies" / "custom",
                       tmp_path / "strategies" / "composite"]
    )
    assert engine.has("ma_golden_cross") and engine.has("boll_breakout")
    write_combinations(
        tmp_path, window_start="2026-01-01", window_end="2026-06-01",
        suggestions=[{"a": "ma_golden_cross", "b": "boll_breakout",
                      "corr": 0.15, "common_days": 60}],
        pairs_evaluated=1,
    )

    result = create_guardian_composite(
        tmp_path, engine, left="ma_golden_cross", right="boll_breakout",
        latest_enriched=date(2026, 6, 1),
    )
    assert result["created"] is True
    combo_id = result["strategy_id"]
    assert combo_id.startswith("guardian_combo_")
    strategy = engine.get(combo_id)
    assert strategy.execution_backend == "composite"
    assert {c.strategy_id for c in strategy.composite.children} == {
        "ma_golden_cross", "boll_breakout"
    }
    # 自动入护: trailing_reference 基线
    profile = GuardianStore(tmp_path).get(combo_id)
    assert profile is not None
    assert profile["baseline"]["kind"] == "trailing_reference"
    assert profile["baseline"]["data_as_of"] == "2026-06-01"

    # 幂等: 已存在时不再写文件, 基线保留
    profile["baseline"]["metrics"] = {"score": 9.9}
    GuardianStore(tmp_path)._write(profile)
    again = create_guardian_composite(
        tmp_path, engine, left="boll_breakout", right="ma_golden_cross",
        latest_enriched=date(2026, 6, 2),
    )
    assert again["created"] is False
    refreshed = GuardianStore(tmp_path).get(combo_id)
    assert refreshed["baseline"]["metrics"] == {"score": 9.9}


# ── P3: 名额门槛 + 实验室战报 ─────────────────────────────────────


class _Repo:
    def __init__(self, data_dir: Path) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)

    @staticmethod
    def latest_enriched_date(asset_type="stock"):
        return date(2026, 6, 1)


class _FakeEngine:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids
        self.reloaded = 0

    def list_strategies(self):
        return [{"id": value} for value in self.ids]

    def get(self, strategy_id):
        if strategy_id not in self.ids:
            raise ValueError(f"unknown strategy: {strategy_id}")
        return SimpleNamespace(
            meta={"asset_types": ["stock"], "name": strategy_id, "params": []}
        )

    def has(self, strategy_id):
        return strategy_id in self.ids

    def reload(self):
        self.reloaded += 1


def test_publish_slot_limit_blocks_when_full(tmp_path, monkeypatch):
    """名额满: publish 拒绝并给出指引; 上限 0 = 不限。"""
    from tests.test_mining_candidates import _create_run, _real_service

    store, run_id, signature = _create_run(tmp_path)
    service, engine = _real_service(tmp_path, store)
    from app.services import preferences

    monkeypatch.setattr(
        preferences, "get_guardian_schedule",
        lambda: {"guardian_schedule_enabled": False, "guardian_watch_pct": 0.2,
                 "guardian_retire_pct": 0.5, "guardian_k_max": 4,
                 "lab_mined_slot_limit": 1},
    )
    original_list = engine.list_strategies
    engine.list_strategies = (  # type: ignore[method-assign]
        lambda: [*original_list(), {"id": "mined_factor_occupied"}]
    )
    with pytest.raises(ValueError, match="名额已满"):
        service.publish(run_id, signature)

    monkeypatch.setattr(
        preferences, "get_guardian_schedule",
        lambda: {"guardian_schedule_enabled": False, "guardian_watch_pct": 0.2,
                 "guardian_retire_pct": 0.5, "guardian_k_max": 4,
                 "lab_mined_slot_limit": 0},
    )
    assert service.publish(run_id, signature)["ok"] is True


def test_lab_report_shape_and_slots(tmp_path, monkeypatch):
    from app.api.lab import router as lab_router
    from app.services import preferences
    from app.services.mining_jobs import MiningRunStore

    monkeypatch.setattr(
        preferences, "get_guardian_schedule",
        lambda: {"guardian_schedule_enabled": True, "guardian_watch_pct": 0.2,
                 "guardian_retire_pct": 0.5, "guardian_k_max": 4,
                 "lab_mined_slot_limit": 2},
    )
    store = GuardianStore(tmp_path)
    _seed_healthy(store, "mined_factor_a")
    store.append_check("mined_factor_a", {"verdict": "watch"})
    store.set_status("mined_factor_a", "watch")

    app = FastAPI()
    app.include_router(lab_router)
    app.state.repo = _Repo(tmp_path)
    app.state.strategy_engine = _FakeEngine(
        ["mined_factor_a", "mined_factor_b", "other"]
    )
    app.state.mining_manager = SimpleNamespace(
        store=MiningRunStore(tmp_path, namespace="guardian")
    )
    app.state.guardian_manager = SimpleNamespace(
        store=MiningRunStore(tmp_path, namespace="guardian")
    )

    body = TestClient(app).get("/api/backtest/lab/report").json()
    assert body["slots"] == {
        "active_mined": 2,
        "active_mined_ids": ["mined_factor_a", "mined_factor_b"],
        "limit": 2, "full": True,
    }
    assert body["guardian"]["by_status"] == {"watch": 1}
    assert body["guardian"]["profiles"] == 1
    assert body["combinations"]["suggestions"] == []
    assert body["mining"]["last_run"] is None


def test_guardian_api_combination_endpoints(tmp_path, monkeypatch):
    from app.api.guardian import router as guardian_router
    from app.services import preferences
    from app.services.mining_jobs import MiningRunStore

    monkeypatch.setattr(
        preferences, "get_guardian_schedule",
        lambda: {"guardian_schedule_enabled": False, "guardian_watch_pct": 0.2,
                 "guardian_retire_pct": 0.5, "guardian_k_max": 4,
                 "lab_mined_slot_limit": 10},
    )

    class _Manager:
        def __init__(self) -> None:
            self.store = MiningRunStore(tmp_path, namespace="guardian")
            self.starts = []

        def start(self, request, fingerprint, force=False, source="manual",
                  run_id=None):
            self.starts.append(request)
            return self.store.create(request, fingerprint)

    app = FastAPI()
    app.include_router(guardian_router)
    app.state.repo = _Repo(tmp_path)
    app.state.strategy_engine = _FakeEngine([])
    manager = _Manager()
    app.state.guardian_manager = manager
    client = TestClient(app)

    response = client.post("/api/backtest/guardian/combinations/scan")
    assert response.status_code == 200
    assert manager.starts[-1]["mode"] == "combinations"

    # 无建议文件时创建 → 400
    response = client.post(
        "/api/backtest/guardian/combinations/create",
        json={"a": "x", "b": "y"},
    )
    assert response.status_code == 400
    assert "组合扫描" in response.json()["detail"]

    # overview 带组合建议
    write_combinations(
        tmp_path, window_start="2026-01-01", window_end="2026-06-01",
        suggestions=[{"a": "x", "b": "y", "corr": 0.1, "common_days": 40}],
        pairs_evaluated=1,
    )
    body = client.get("/api/backtest/guardian/overview").json()
    assert body["combinations"]["suggestions"][0]["a"] == "x"


def test_create_composite_accepts_symlinked_data_dir(tmp_path):
    """data_dir 含符号链接时 (如 macOS /tmp) 不能误判为路径逃逸。"""
    from app.strategy.engine import StrategyEngine

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    builtin = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "strategies"
    engine = StrategyEngine(
        strategy_dirs=[builtin, link / "strategies" / "custom",
                       link / "strategies" / "composite"]
    )
    write_combinations(
        link, window_start="2026-01-01", window_end="2026-06-01",
        suggestions=[{"a": "ma_golden_cross", "b": "boll_breakout",
                      "corr": 0.15, "common_days": 60}],
        pairs_evaluated=1,
    )
    result = create_guardian_composite(
        link, engine, left="ma_golden_cross", right="boll_breakout",
        latest_enriched=date(2026, 6, 1),
    )
    assert result["created"] is True
    assert (real / "strategies" / "composite"
            / f"{result['strategy_id']}.py").is_file()
