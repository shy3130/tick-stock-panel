"""旧数据目录升级的宽容降级与一次性自动迁移 — 去内置化配套。

内置策略移除后, 老环境的用户数据仍引用旧世界。此处回归:
  1. builtin.factor_rank_research 兼容垫片透明转发到 research 模块;
  2. /run_all 混合未知 ID → 跳过并回 skipped_unknown; 全部未知才 404;
  3. 信号种子 seed_if_empty: 空目录一次写入 20 个默认定义, 标记防重,
     用户已有定义/再次启动不重复种入;
  4. 失效策略文件归档: 仅「内置残留」类错误归档, 语法错误不动。
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


# ── 信号种子一次性迁移 ─────────────────────────────────────
def test_seed_if_empty_writes_once_and_marks(tmp_path):
    from app.strategy import custom_signals

    n = custom_signals.seed_if_empty(tmp_path)
    assert n == 20
    files = list((tmp_path / "user_data" / "custom_signals").glob("*.json"))
    assert len(files) == 20
    # 标记存在 → 二次调用不再种入 (即使清空目录也不复活)
    for f in files:
        f.unlink()
    assert custom_signals.seed_if_empty(tmp_path) == 0
    assert not list((tmp_path / "user_data" / "custom_signals").glob("*.json"))


def test_seed_if_empty_merges_beside_user_signals(tmp_path):
    """升级用户已有自定义信号时仍种入缺失的默认信号 (不覆盖用户文件)。

    旧逻辑"目录非空即整体跳过"曾令升级用户丢失内置涨跌停信号:
    看板跌停恒 0、连板梯队无数据。
    """
    from app.strategy import custom_signals

    d = tmp_path / "user_data" / "custom_signals"
    d.mkdir(parents=True)
    (d / "my_own.json").write_text("{}", encoding="utf-8")

    assert custom_signals.seed_if_empty(tmp_path) == 20
    names = {f.name for f in d.glob("*.json")}
    assert "my_own.json" in names
    assert (d / "limit_up.json").exists()
    assert (d / "limit_down.json").exists()
    assert (d / "my_own.json").read_text(encoding="utf-8") == "{}"
    # 补种完成后标记生效, 二次调用零写入
    assert custom_signals.seed_if_empty(tmp_path) == 0


def test_seed_if_empty_remediates_legacy_skip_marker(tmp_path):
    """旧逻辑写的 skipped 标记视为未种子化, 升级后自动补种。"""
    from app.strategy import custom_signals

    d = tmp_path / "user_data" / "custom_signals"
    d.mkdir(parents=True)
    (d / "my_own.json").write_text("{}", encoding="utf-8")
    (d / ".seeded_v1").write_text("skipped: user signals present\n", encoding="utf-8")

    assert custom_signals.seed_if_empty(tmp_path) == 20
    assert (d / "limit_up.json").exists()
    assert "skipped" not in (d / ".seeded_v1").read_text(encoding="utf-8")
    # 补种后标记为正常 seeded, 不再重复
    assert custom_signals.seed_if_empty(tmp_path) == 0


def test_seed_if_empty_does_not_overwrite_existing_seed_ids(tmp_path):
    """同 id 文件已存在 (用户改过种子) 时不覆盖, 只补缺失项。"""
    import json

    from app.strategy import custom_signals
    from app.strategy.signal_seeds import SEED_SIGNALS

    d = tmp_path / "user_data" / "custom_signals"
    d.mkdir(parents=True)
    sentinel = {"id": "limit_up", "name": "我改过的涨停", "enabled": False}
    (d / "limit_up.json").write_text(
        json.dumps(sentinel, ensure_ascii=False), encoding="utf-8"
    )

    n = custom_signals.seed_if_empty(tmp_path)
    assert n == len(SEED_SIGNALS) - 1
    assert json.loads((d / "limit_up.json").read_text(encoding="utf-8"))["name"] == "我改过的涨停"


# ── 失效策略文件归档 ───────────────────────────────────────
def test_archive_only_legacy_unloadable_files(tmp_path):
    from app.strategy.legacy_migrate import archive_unloadable_strategies

    custom = tmp_path / "data" / "strategies" / "custom"
    custom.mkdir(parents=True)
    broken_composite = custom / "combo_old.py"
    broken_composite.write_text("# composite", encoding="utf-8")
    syntax_err = custom / "editing_wip.py"
    syntax_err.write_text("# wip", encoding="utf-8")

    archived = archive_unloadable_strategies([
        {"file": str(broken_composite), "error": "composite strategy combo_old 引用的子策略 'boll_breakout' 不存在"},
        {"file": str(syntax_err), "error": "SyntaxError: unexpected EOF"},
        {"file": str(custom / "missing.py"), "error": "composite strategy x 引用的子策略 'y' 不存在"},
    ])

    assert archived == ["combo_old.py"]
    assert not broken_composite.exists()
    assert syntax_err.exists()  # 普通失败不归档
    archive_dir = custom / "_archive_unloadable"
    assert (archive_dir / "combo_old.py").exists()
