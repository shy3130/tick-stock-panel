"""盘后策略缓存刷新服务回归测试。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.services import strategy_cache, strategy_refresh
from app.strategy.engine import StrategyResult


class _FakeRepo:
    def __init__(self, data_dir):
        self.store = SimpleNamespace(data_dir=data_dir)


class _FakeScreener:
    def __init__(self, repo, asset_type="stock"):
        self.repo = repo
        self.asset_type = asset_type

    def latest_date(self):
        return date(2026, 7, 29)

    def build_strategy_context(
        self,
        engine,
        as_of,
        strategy_ids,
        *,
        timeframe,
        params_map,
        overrides_map,
    ):
        return {
            "as_of": as_of,
            "strategy_ids": strategy_ids,
            "timeframe": timeframe,
        }


class _FakeEngine:
    def list_strategies(self):
        return [
            {"id": "alpha", "asset_types": ["stock"], "timeframes": ["1d"]},
            {"id": "beta", "asset_types": ["stock"], "timeframes": ["1d"]},
            {"id": "etf_only", "asset_types": ["etf"], "timeframes": ["1d"]},
        ]

    def has(self, strategy_id):
        return strategy_id in {"alpha", "beta", "etf_only"}

    def run_all(self, context, *, params_map, overrides_map, strategy_ids):
        as_of = context["as_of"]
        return {
            "alpha": StrategyResult(
                as_of=as_of,
                strategy_id="alpha",
                rows=[{"symbol": "600000.SH", "score": float("nan")}],
                total=1,
            ),
            "beta": StrategyResult(
                as_of=as_of,
                strategy_id="beta",
                rows=[],
                total=0,
            ),
        }


def test_refresh_strategy_cache_writes_latest_day_and_verifies_it(tmp_path, monkeypatch):
    monkeypatch.setattr(strategy_refresh, "ScreenerService", _FakeScreener)
    monkeypatch.setattr(
        strategy_refresh.strategy_config,
        "list_overrides",
        lambda _data_dir: {},
    )

    receipt = strategy_refresh.refresh_strategy_cache(
        _FakeRepo(tmp_path),
        _FakeEngine(),
    )

    cached = strategy_cache.read_cache(tmp_path)
    assert cached is not None
    assert cached["as_of"] == "2026-07-29"
    assert set(cached["results"]) == {"alpha", "beta"}
    assert cached["results"]["alpha"]["rows"][0]["score"] is None
    assert receipt == {
        "as_of": "2026-07-29",
        "strategy_count": 2,
        "matched_rows": 1,
        "results": cached["results"],
    }


def test_refresh_strategy_cache_fails_if_write_did_not_advance_date(tmp_path, monkeypatch):
    strategy_cache.write_cache(tmp_path, "2026-07-28", {"old": {"rows": [], "total": 0}})
    monkeypatch.setattr(strategy_refresh, "ScreenerService", _FakeScreener)
    monkeypatch.setattr(
        strategy_refresh.strategy_config,
        "list_overrides",
        lambda _data_dir: {},
    )
    monkeypatch.setattr(strategy_refresh.strategy_cache, "write_cache", lambda *_args: None)

    with pytest.raises(RuntimeError, match="策略缓存写入后校验失败"):
        strategy_refresh.refresh_strategy_cache(_FakeRepo(tmp_path), _FakeEngine())


def test_refresh_strategy_cache_rejects_missing_current_result_even_if_old_cache_has_it(
    tmp_path,
    monkeypatch,
):
    """同日旧缓存不能掩盖本轮引擎漏算的策略。"""

    class IncompleteEngine(_FakeEngine):
        def run_all(self, context, *, params_map, overrides_map, strategy_ids):
            return {
                "alpha": StrategyResult(
                    as_of=context["as_of"],
                    strategy_id="alpha",
                    rows=[],
                    total=0,
                )
            }

    strategy_cache.write_cache(
        tmp_path,
        "2026-07-29",
        {
            "alpha": {"as_of": "2026-07-29", "total": 0, "rows": []},
            "beta": {"as_of": "2026-07-29", "total": 1, "rows": [{"symbol": "OLD"}]},
        },
    )
    monkeypatch.setattr(strategy_refresh, "ScreenerService", _FakeScreener)
    monkeypatch.setattr(
        strategy_refresh.strategy_config,
        "list_overrides",
        lambda _data_dir: {},
    )

    with pytest.raises(RuntimeError, match=r"本次策略计算结果不完整.*beta"):
        strategy_refresh.refresh_strategy_cache(_FakeRepo(tmp_path), IncompleteEngine())


def test_screener_run_all_reuses_verified_refresh_service(monkeypatch):
    from app.api import screener

    calls = []
    expected_results = {
        "alpha": {
            "total": 1,
            "as_of": "2026-07-29",
            "rows": [{"symbol": "600000.SH"}],
        }
    }

    def fake_refresh(repo, engine, **kwargs):
        calls.append((repo, engine, kwargs))
        return {
            "as_of": "2026-07-29",
            "strategy_count": 1,
            "matched_rows": 1,
            "results": expected_results,
        }

    repo = SimpleNamespace(store=SimpleNamespace(data_dir="unused"))
    engine = object()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(repo=repo, strategy_engine=engine)
        )
    )
    monkeypatch.setattr(screener, "refresh_strategy_cache", fake_refresh, raising=False)
    fake_screener_service = object()
    monkeypatch.setattr(
        screener,
        "ScreenerService",
        lambda *_args, **_kwargs: fake_screener_service,
    )

    response = screener.run_all(
        request,
        {
            "as_of": "2026-07-29",
            "asset_type": "stock",
            "timeframe": "1d",
            "strategy_ids": ["alpha"],
            "summary_only": True,
        },
    )

    assert calls == [
        (
            repo,
            engine,
            {
                "as_of": date(2026, 7, 29),
                "asset_type": "stock",
                "timeframe": "1d",
                "strategy_ids": ["alpha"],
                "screener_service": fake_screener_service,
            },
        )
    ]
    assert response == {
        "as_of": "2026-07-29",
        "results": {"alpha": {"total": 1, "as_of": "2026-07-29"}},
    }
