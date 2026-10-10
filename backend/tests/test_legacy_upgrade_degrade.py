"""旧数据目录升级的宽容降级 — 去内置化迁移配套。

内置策略移除后, 老环境的用户数据 (data/strategies/ 里的自定义文件、浏览器
策略池) 仍引用旧世界: 文件 import ``app.strategy.builtin.factor_rank_research``
断链逐条 toast; 策略池残留内置 ID 让 /run_all 整体 404。此处回归:
  1. builtin.factor_rank_research 兼容垫片透明转发到 research 模块;
  2. /run_all 混合未知 ID → 跳过并回 skipped_unknown; 全部未知才 404。
"""
from __future__ import annotations

import types
from datetime import date

import pytest
from fastapi import HTTPException

from app.api import screener as screener_api
from app.services.screener import ScreenerResult


class _Engine:
    """只有一个已知策略的最小引擎 (has/get/run_all)。"""

    def has(self, strategy_id):
        return strategy_id == "kept_strategy"

    def get(self, strategy_id):
        if not self.has(strategy_id):
            raise ValueError(f"unknown strategy: {strategy_id}")
        return types.SimpleNamespace(meta={"id": strategy_id})

    def run_all(self, context, *, params_map=None, overrides_map=None, strategy_ids=None, parallel=True):
        return {
            sid: ScreenerResult(as_of=context.as_of, strategy=sid)
            for sid in strategy_ids or []
        }


class _Svc:
    def __init__(self, repo, asset_type="stock"):
        pass

    def latest_date(self):
        return date(2026, 7, 15)

    def build_strategy_context(self, engine, as_of, strategy_ids, *, timeframe="1d", params_map=None, overrides_map=None):
        return types.SimpleNamespace(as_of=as_of)


def test_builtin_factor_rank_research_shim_forwards_to_research_module():
    import app.strategy.builtin.factor_rank_research as legacy
    import app.strategy.research.factor_rank_research as current

    assert legacy is current
    # 旧策略文件的真实用法: from ... import 常量/类
    from app.strategy.builtin.factor_rank_research import META  # noqa: F401


def test_run_all_skips_unknown_ids_and_reports_them(monkeypatch, tmp_path):
    """池里混着已删除的内置 ID: 跳过继续跑, 响应带回 skipped_unknown。"""
    engine = _Engine()
    repo = types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path))
    state = types.SimpleNamespace(repo=repo, strategy_engine=engine)
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=state))

    monkeypatch.setattr(screener_api, "ScreenerService", _Svc)
    monkeypatch.setattr(screener_api, "_load_ext_value_maps", lambda *a, **k: {})
    monkeypatch.setattr(screener_api.strategy_cache, "write_cache", lambda *a: None)
    monkeypatch.setattr(screener_api, "_update_cache_strategy", lambda *a: None)
    # 渐进式队列不参与本测试: 直接走同步首返路径
    monkeypatch.setattr(
        screener_api.strategy_run_queue, "order_strategy_ids", lambda ids, _t: list(ids)
    )

    body = {
        "strategy_ids": ["kept_strategy", "builtin_gone_a", "builtin_gone_b"],
        "as_of": "2026-07-15",
    }
    result = screener_api.run_all(request, body)

    assert "kept_strategy" in result["results"]
    assert set(result["skipped_unknown"]) == {"builtin_gone_a", "builtin_gone_b"}


def test_run_all_all_unknown_still_404(monkeypatch, tmp_path):
    engine = _Engine()
    repo = types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path))
    state = types.SimpleNamespace(repo=repo, strategy_engine=engine)
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=state))

    monkeypatch.setattr(screener_api, "ScreenerService", _Svc)

    with pytest.raises(HTTPException) as exc:
        screener_api.run_all(request, {"strategy_ids": ["gone_only"], "as_of": "2026-07-15"})
    assert exc.value.status_code == 404
