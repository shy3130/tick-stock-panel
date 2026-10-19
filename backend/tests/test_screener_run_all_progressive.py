"""run_all 渐进式返回的端点行为 — 快策略先返回、慢策略后台落缓存、搭车与旧路径兼容。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import screener as screener_api
from app.config import settings
from app.services import strategy_cache, strategy_run_queue

AS_OF = "2026-09-04"


@dataclass
class _FakeResult:
    total: int = 1
    rows: list = field(default_factory=lambda: [{"symbol": "000001.SZ", "close": 1.0}])
    as_of: str = AS_OF


class _FakeEngine:
    def __init__(self, delays: dict[str, float]):
        self._delays = delays
        self.executed: list[str] = []

    def has(self, sid: str) -> bool:
        return sid in self._delays

    def get(self, sid: str):
        return SimpleNamespace(meta={})

    def run_all(self, context, params_map=None, overrides_map=None, *, strategy_ids=None, parallel=True):
        out = {}
        for sid in strategy_ids or []:
            self.executed.append(sid)
            time.sleep(self._delays[sid])
            out[sid] = _FakeResult()
        return out


class _FakeService:
    def __init__(self, repo, asset_type="stock"):
        pass

    def latest_date(self):
        return date.fromisoformat(AS_OF)

    def build_strategy_context(self, *args, **kwargs):
        return SimpleNamespace()


def _request(tmp_path, engine):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = SimpleNamespace(repo=repo, strategy_engine=engine, monitor_engine=None)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _wait_cache_results(tmp_path, want_ids, timeout=8.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        results = (strategy_cache.read_cache(tmp_path) or {}).get("results") or {}
        if all(i in results for i in want_ids):
            return results
        time.sleep(0.05)
    return (strategy_cache.read_cache(tmp_path) or {}).get("results") or {}


@pytest.fixture()
def fast_first_return(monkeypatch):
    monkeypatch.setattr(settings, "strategy_run_all_first_return_s", 0.4)


def test_run_all_returns_fast_first_then_background_fills_cache(
    monkeypatch, tmp_path, fast_first_return
):
    engine = _FakeEngine({"fast_a": 0.02, "fast_b": 0.02, "slow_c": 0.8})
    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)

    resp = screener_api.run_all(
        _request(tmp_path, engine),
        {
            "as_of": AS_OF,
            "strategy_ids": ["fast_a", "fast_b", "slow_c"],
            "asset_type": "stock",
            "timeframe": "1d",
            "summary_only": True,
        },
    )
    # 首返: 快策略已完成 (带 total, 无 rows), 慢策略 pending
    assert set(resp["results"]) == {"fast_a", "fast_b"}
    assert resp["results"]["fast_a"]["total"] == 1
    assert "rows" not in resp["results"]["fast_a"]
    assert resp["pending"] == ["slow_c"]
    assert resp["complete"] is False
    assert isinstance(resp["started_at"], int)

    # 后台继续: 慢策略最终也落进缓存, 且带 computed_at
    results = _wait_cache_results(tmp_path, ["fast_a", "fast_b", "slow_c"])
    assert set(results) == {"fast_a", "fast_b", "slow_c"}
    assert all(r.get("computed_at") for r in results.values())

    # 耗时已记录 → 下次按耗时升序 (快策略先算)
    timings = strategy_run_queue.load_run_timings(tmp_path)
    assert set(timings) == {"fast_a", "fast_b", "slow_c"}
    assert timings["slow_c"] > timings["fast_a"]


def test_run_all_second_run_orders_by_recorded_timings(
    monkeypatch, tmp_path, fast_first_return
):
    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)
    engine = _FakeEngine({"slow_a": 0.5, "fast_b": 0.01})
    body = {
        "as_of": AS_OF,
        "strategy_ids": ["slow_a", "fast_b"],
        "asset_type": "stock",
        "timeframe": "1d",
        "summary_only": True,
    }
    req = _request(tmp_path, engine)

    def _wait_executed(count, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(engine.executed) >= count:
                return
            time.sleep(0.05)
        raise AssertionError(f"执行未到 {count} 个: {engine.executed}")

    # 第一轮: 无耗时记录 → 按传入顺序 slow_a 先算
    screener_api.run_all(req, body)
    _wait_executed(2)
    assert engine.executed == ["slow_a", "fast_b"]

    # 第二轮: 有耗时记录 → fast_b (快) 升序在前, 且首返带上 fast_b。
    # 若第一轮 handle 还在收尾 (终写缓存/记录耗时), 请求会搭车旧 handle;
    # 重试直到真正触发新一轮执行。
    resp2 = None
    for _ in range(40):
        engine.executed.clear()
        resp2 = screener_api.run_all(req, body)
        if engine.executed:
            break
        time.sleep(0.05)
    assert resp2 is not None and engine.executed, "第二轮未触发新执行"
    assert engine.executed[0] == "fast_b"
    assert set(resp2["results"]) == {"fast_b"}
    _wait_executed(2)
    assert engine.executed == ["fast_b", "slow_a"]


def test_run_all_same_key_piggybacks_running_execution(
    monkeypatch, tmp_path, fast_first_return
):
    engine = _FakeEngine({"fast_a": 0.02, "slow_c": 1.2})
    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)
    body = {
        "as_of": AS_OF,
        "strategy_ids": ["fast_a", "slow_c"],
        "asset_type": "stock",
        "timeframe": "1d",
        "summary_only": True,
    }
    req = _request(tmp_path, engine)
    resp1 = screener_api.run_all(req, body)
    # 第一笔仍在后台跑 slow_c 时, 相同请求搭车: 同一起点, 不重复执行
    resp2 = screener_api.run_all(_request(tmp_path, engine), body)
    assert resp2["started_at"] == resp1["started_at"]
    assert engine.executed.count("fast_a") == 1
    _wait_cache_results(tmp_path, ["fast_a", "slow_c"])
    assert engine.executed.count("slow_c") == 1
    assert engine.executed.count("fast_a") == 1


def test_run_all_job_level_error_without_results_is_500(
    monkeypatch, tmp_path, fast_first_return
):
    """job 级失败 (如 context 构建崩溃) 且无任何结果 → 500, 语义不变。

    策略级失败 (engine.run_all 对单个 sid 抛错) 已改为逐策略隔离, 见
    test_run_all_isolates_single_strategy_failure。
    """

    class _BoomCtxService(_FakeService):
        def build_strategy_context(self, *args, **kwargs):
            raise ValueError("缺少列: volume")

    monkeypatch.setattr(screener_api, "ScreenerService", _BoomCtxService)
    with pytest.raises(HTTPException) as excinfo:
        screener_api.run_all(
            _request(tmp_path, _FakeEngine({"bad_a": 0.01})),
            {
                "as_of": AS_OF,
                "strategy_ids": ["bad_a"],
                "asset_type": "stock",
                "timeframe": "1d",
                "summary_only": True,
            },
        )
    assert excinfo.value.status_code == 500
    assert "volume" in excinfo.value.detail


def test_run_all_minute_timeframe_now_progressive(monkeypatch, tmp_path, fast_first_return):
    """分钟 summary 请求也走渐进式: 后台队列执行、响应带 pending 形状、不写日线缓存。

    旧行为 (整段阻塞) 会把 HTTP 线程占到全部分钟策略算完 — 分钟批量曾是最长的
    阻塞路径; 现在与日线同走单飞队列, 结果经 handle 快照合入 cached-summary。
    """
    engine = _FakeEngine({"m1": 0.01})
    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)
    resp = screener_api.run_all(
        _request(tmp_path, engine),
        {
            "as_of": AS_OF,
            "strategy_ids": ["m1"],
            "asset_type": "stock",
            "timeframe": "1m",
            "summary_only": True,
        },
    )
    # 渐进式响应形状: 结果 + pending 字段; fast_first_return 内策略已完成
    assert set(resp["results"]) == {"m1"}
    assert resp["complete"] is True
    assert resp["pending"] == []
    # 分钟结果不落 strategy_cache (日线语义) — 只能经 handle 快照合入 summary
    assert not ((strategy_cache.read_cache(tmp_path) or {}).get("results") or {})


def test_run_all_full_detail_stays_blocking(monkeypatch, tmp_path, fast_first_return):
    engine = _FakeEngine({"d1": 0.01})
    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)
    resp = screener_api.run_all(
        _request(tmp_path, engine),
        {
            "as_of": AS_OF,
            "strategy_ids": ["d1"],
            "asset_type": "stock",
            "timeframe": "1d",
        },
    )
    # 非 summary 请求: 保持整段阻塞并返回明细
    assert resp["results"]["d1"]["rows"][0]["symbol"] == "000001.SZ"
    assert "pending" not in resp


def test_run_all_progressive_builds_matrix_once_and_shares_it(
    monkeypatch, tmp_path, fast_first_return
):
    """渐进式逐策略执行前, 并集矩阵只建一次并放进 context.market 复用。

    旧行为: 每个策略单独 run_all, context.market 不回写 → 每个矩阵策略都
    重建全市场矩阵 (小服务器上单次数秒到十余秒)。
    """

    @dataclass
    class _Context:
        market: object = None

    class _MatrixEngine(_FakeEngine):
        def __init__(self, delays):
            super().__init__(delays)
            self.matrix_builds = 0
            self.seen_markets: list[object] = []

        def build_shared_matrix(self, context, selected, params_map=None, overrides_map=None):
            self.matrix_builds += 1
            return {"fields": len(selected)}

        def run_all(self, context, params_map=None, overrides_map=None, *, strategy_ids=None, parallel=True):
            self.seen_markets.append(context.market)
            return super().run_all(
                context, params_map=params_map, overrides_map=overrides_map,
                strategy_ids=strategy_ids, parallel=parallel,
            )

    class _CtxService(_FakeService):
        def build_strategy_context(self, *args, **kwargs):
            return _Context()

    engine = _MatrixEngine({"s1": 0.01, "s2": 0.01, "s3": 0.01})
    monkeypatch.setattr(screener_api, "ScreenerService", _CtxService)

    resp = screener_api.run_all(
        _request(tmp_path, engine),
        {
            "as_of": AS_OF,
            "strategy_ids": ["s1", "s2", "s3"],
            "asset_type": "stock",
            "timeframe": "1d",
            "summary_only": True,
        },
    )
    results = _wait_cache_results(tmp_path, ["s1", "s2", "s3"])
    assert set(results) == {"s1", "s2", "s3"}
    assert resp["complete"] is True or resp["pending"] == []

    # 矩阵只构建一次; 每个策略拿到的都是同一个 market 对象
    assert engine.matrix_builds == 1
    assert engine.seen_markets and all(m == {"fields": 3} for m in engine.seen_markets)
    assert len(engine.seen_markets) == 3


def test_run_all_isolates_single_strategy_failure(
    monkeypatch, tmp_path, fast_first_return
):
    """单个策略执行崩溃只跳过它自己: 其余策略照常算完落缓存, 整批不失败。"""

    class _FlakyEngine(_FakeEngine):
        def run_all(self, context, params_map=None, overrides_map=None, *, strategy_ids=None, parallel=True):
            for sid in strategy_ids or []:
                if sid == "broken":
                    raise ValueError("boom: schema mismatch")
            return super().run_all(
                context, params_map=params_map, overrides_map=overrides_map,
                strategy_ids=strategy_ids, parallel=parallel,
            )

    engine = _FlakyEngine({"ok_a": 0.01, "broken": 0.01, "ok_b": 0.5})
    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)

    resp = screener_api.run_all(
        _request(tmp_path, engine),
        {
            "as_of": AS_OF,
            "strategy_ids": ["ok_a", "broken", "ok_b"],
            "asset_type": "stock",
            "timeframe": "1d",
            "summary_only": True,
        },
    )
    # 后台继续: 好策略都落缓存; broken 不在结果也不在 pending, 而是进 errors
    results = _wait_cache_results(tmp_path, ["ok_a", "ok_b"])
    assert set(results) == {"ok_a", "ok_b"}
    assert "broken" not in results
    assert "boom: schema mismatch" in (resp["errors"] or {}).get("broken", "")
