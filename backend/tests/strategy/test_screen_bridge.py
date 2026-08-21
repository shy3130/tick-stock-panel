"""F16 切片 A: screen 方案 → 策略桥 (注册/分类/回测 fail-closed)。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

from app.backtest.engine import SimResult
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.services import screener_screens
from app.services.screener_query import ScreenerSemanticError
from app.strategy.engine import StrategyDef, StrategyEngine
from app.strategy.screen_bridge import (
    SCREEN_SOURCE,
    ScreenPanelUnsupportedError,
    build_screen_strategy,
    classify_screen,
    screen_strategy_id,
    sync_screen_strategies,
)


def _screen(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "abc123def456",
        "name": "强势股",
        "conditions": [{"field": "close", "op": ">", "value": 10}],
        "order_by": None,
        "limit": 100,
    }
    base.update(overrides)
    return base


# ── build ──────────────────────────────────────────────────────────


def test_build_screen_strategy_shape():
    s = build_screen_strategy(_screen())

    assert s.meta["id"] == "screen:abc123def456"
    assert s.source == SCREEN_SOURCE
    assert s.basic_filter == {"enabled": False}
    assert s.entry_signals == [] and s.exit_signals == []
    assert s.lookback_days == 1 and s.file_path is None
    assert s.meta["params"] == []
    assert s.def_hash  # 谓词指纹非空


def test_def_hash_sensitive_to_predicate():
    a = build_screen_strategy(_screen())
    b = build_screen_strategy(_screen(conditions=[{"field": "close", "op": ">", "value": 20}]))
    same = build_screen_strategy(_screen(name="改名不影响"))

    assert a.def_hash != b.def_hash  # 谓词变 → hash 变
    assert a.def_hash == same.def_hash  # 名称变 → hash 不变 (只跟谓词)


def test_def_hash_ignores_order_by_and_limit_but_tracks_group_logic():
    a = build_screen_strategy(_screen())
    # 仅改 order_by / limit → 谓词语义不变 → hash 不变
    reordered = build_screen_strategy(
        _screen(order_by={"field": "close", "direction": "asc"}, limit=5)
    )
    assert a.def_hash == reordered.def_hash
    # group_logic 变 (谓词合并语义变) → hash 变
    grouped = build_screen_strategy(
        _screen(conditions=[
            {"field": "close", "op": ">", "value": 10, "group": "A"},
            {"field": "close", "op": "<", "value": 20, "group": "B"},
        ], group_logic="or")
    )
    grouped_and = build_screen_strategy(
        _screen(conditions=[
            {"field": "close", "op": ">", "value": 10, "group": "A"},
            {"field": "close", "op": "<", "value": 20, "group": "B"},
        ], group_logic="and")
    )
    assert grouped.def_hash != grouped_and.def_hash


def test_classify_rejects_sequence_fields():
    ok, fields = classify_screen(_screen(conditions=[{"field": "seq_consecutive_up_3", "op": "=", "value": True}]))
    assert not ok and fields == ["连续上涨3日"]
    ok, fields = classify_screen(_screen(conditions=[
        {"field": "close", "op": ">", "value": 1},
        {"field": "seq_cum_change_5d", "op": ">", "value": 5},
    ]))
    assert not ok and fields == ["近5日累计涨跌幅"]


def test_filter_fn_applies_group_or_logic():
    s = build_screen_strategy(_screen(conditions=[
        {"field": "close", "op": ">", "value": 10, "group": "A"},
        {"field": "close", "op": "<", "value": 9, "group": "B"},
    ], group_logic="or"))
    mask = _panel().select(s.filter_fn(_panel(), {}).alias("_m"))["_m"]
    # A 组: close>10 → A/C; B 组: close<9 → B; OR → 全部命中
    assert mask.to_list() == [True, True, True]
    s_and = build_screen_strategy(_screen(conditions=[
        {"field": "close", "op": ">", "value": 10, "group": "A"},
        {"field": "close", "op": "<", "value": 9, "group": "B"},
    ]))
    mask_and = _panel().select(s_and.filter_fn(_panel(), {}).alias("_m"))["_m"]
    assert mask_and.to_list() == [False, False, False]


def test_build_invalid_predicate_raises():
    import pytest

    with pytest.raises(ScreenerSemanticError):
        build_screen_strategy(_screen(conditions=[{"field": "no_such_field", "op": ">", "value": 1}]))


# ── classify ───────────────────────────────────────────────────────


def test_classify_rejects_external_join_fields():
    # 财务 (financials)
    ok, fields = classify_screen(_screen(conditions=[{"field": "roe", "op": ">", "value": 10}]))
    assert not ok and fields == ["ROE"]
    # 龙虎榜 (reference)
    ok, fields = classify_screen(_screen(conditions=[{"field": "lhb_count_30d", "op": ">=", "value": 1}]))
    assert not ok and fields == ["近30天上榜次数"]
    # 派生 deps 含 eps_annualized (不在回测面板列) → pe_approx 不支持 (label 从 registry 取, 避免与 F17 改名耦合)
    from app.services.screener_query import FIELD_REGISTRY

    ok, fields = classify_screen(_screen(conditions=[{"field": "pe_approx", "op": "<", "value": 30}]))
    assert not ok and fields == [FIELD_REGISTRY["pe_approx"].label]


def test_classify_rejects_join_only_market_cap():
    # float_shares/total_shares 仅 fresh 路径有 → 市值字段不支持
    ok, fields = classify_screen(_screen(conditions=[{"field": "float_market_cap", "op": "between", "value": [50, 500]}]))
    assert not ok and fields == ["流通市值"]


def test_classify_accepts_panel_fields():
    # 纯面板字段 + 派生 (deps 全在面板) + name 依赖 (exclude_st) + symbol 推导 (board)
    screen = _screen(conditions=[
        {"field": "close", "op": ">", "value": 10},
        {"field": "above_ma20", "op": "=", "value": True},
        {"field": "exclude_st", "op": "=", "value": True},
        {"field": "board", "op": "=", "value": "sh_main"},
    ], order_by={"field": "change_pct", "direction": "desc"})
    ok, fields = classify_screen(screen)
    assert ok and fields == []


def test_classify_ignores_order_by_field():
    # F14 契约: classify 只检查条件字段, order_by 不参与面板求值, 不作为拒绝理由
    ok, fields = classify_screen(_screen(order_by={"field": "industry", "direction": "asc"}))
    assert ok and fields == []


def test_build_ignores_unsortable_order_by():
    # order_by 不参与策略语义: unsortable 字段 (industry) 保存于方案时
    # 仍可注册为策略 (条件页排序只是展示), 不因 validate_query 的
    # unsortable_field 被整份跳过。
    s = build_screen_strategy(_screen(order_by={"field": "industry", "direction": "asc"}))
    assert s.meta["id"] == "screen:abc123def456"


def test_sync_skips_unregistrable_screen():
    # classify 判不可注册 (外部 join 字段) 的方案在 sync 时被跳过,
    # 不进引擎 (否则 run_preset/run_all 会在 filter_fn 抛 500)。
    engine = StrategyEngine.__new__(StrategyEngine)
    engine._strategies = {}
    screens = [
        _screen(id="aaa111bbb222", name="可注册", conditions=[{"field": "close", "op": ">", "value": 10}]),
        _screen(id="ccc333ddd444", name="不可注册", conditions=[{"field": "roe", "op": ">", "value": 10}]),
    ]
    from unittest import mock

    with mock.patch("app.strategy.screen_bridge.list_screens", return_value=screens):
        registered = sync_screen_strategies(engine, Path("/tmp"))
    assert registered == 1
    assert "screen:aaa111bbb222" in engine._strategies
    assert "screen:ccc333ddd444" not in engine._strategies


# ── filter_fn ──────────────────────────────────────────────────────


def _panel() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["A", "B", "C"],
        "date": [date(2026, 8, 19)] * 3,
        "close": [12.0, 8.0, 15.0],
        "ma20": [11.0, 9.0, 20.0],
        "change_pct": [0.03, -0.02, 0.05],
    })


def test_filter_fn_hits_expected_rows():
    s = build_screen_strategy(_screen(conditions=[
        {"field": "close", "op": ">", "value": 10},
        {"field": "above_ma20", "op": "=", "value": True},
    ]))
    expr = s.filter_fn(_panel(), {})
    mask = _panel().select(expr.alias("_m"))["_m"]
    assert mask.to_list() == [True, False, False]


def test_filter_fn_missing_column_raises():
    s = build_screen_strategy(_screen(conditions=[{"field": "above_ma20", "op": "=", "value": True}]))
    no_ma = _panel().drop("ma20")
    import pytest

    with pytest.raises(ScreenPanelUnsupportedError) as exc:
        s.filter_fn(no_ma, {})
    assert "ma20" in str(exc.value)


# ── sync ───────────────────────────────────────────────────────────


def _engine() -> StrategyEngine:
    return StrategyEngine(
        enriched_loader=lambda d: pl.DataFrame(),
        strategy_dirs=[],
    )


def test_sync_idempotent_and_delete_removes(tmp_path):
    screener_screens.create_screen(tmp_path, name="A", conditions=[{"field": "close", "op": ">", "value": 1}])
    engine = _engine()

    first = sync_screen_strategies(engine, tmp_path)
    again = sync_screen_strategies(engine, tmp_path)
    assert first == again == 1
    sid = f"screen:{screener_screens.list_screens(tmp_path)[0]['id']}"
    assert engine.has(sid)

    screener_screens.delete_screen(tmp_path, screener_screens.list_screens(tmp_path)[0]["id"])
    assert sync_screen_strategies(engine, tmp_path) == 0
    assert not engine.has(sid)


def test_sync_skips_invalid_screen(tmp_path, caplog):
    # 直接写一份含非法字段的存储绕过校验
    import json

    path = tmp_path / "user_data" / "screener_screens.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"screens": [
        {"id": "bad000000001", "name": "坏", "conditions": [{"field": "nope", "op": ">", "value": 1}], "order_by": None, "limit": None},
        {"id": "good00000002", "name": "好", "conditions": [{"field": "close", "op": ">", "value": 1}], "order_by": None, "limit": None},
    ]}, ensure_ascii=False), encoding="utf-8")

    engine = _engine()
    assert sync_screen_strategies(engine, tmp_path) == 1
    assert engine.has("screen:good00000002")
    assert not engine.has("screen:bad000000001")


def test_post_reload_hook_re_registers(tmp_path):
    screener_screens.create_screen(tmp_path, name="A", conditions=[{"field": "close", "op": ">", "value": 1}])
    engine = _engine()
    sync_screen_strategies(engine, tmp_path)
    sid = f"screen:{screener_screens.list_screens(tmp_path)[0]['id']}"
    engine.post_reload_hooks.append(lambda: sync_screen_strategies(engine, tmp_path))

    engine.reload()  # reload 重建 _strategies → hook 重注册

    assert engine.has(sid)
    # hook 失败仅 warning, 不抛
    engine.post_reload_hooks.append(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    engine.reload()


# ── 回测端到端 ─────────────────────────────────────────────────────


class _EngineStub:
    """回测引擎替身: 面板注入 + 撮合空转。"""

    def __init__(self, panel: pl.DataFrame) -> None:
        self.panel = panel
        self.repo = SimpleNamespace(get_index_daily=lambda *a, **k: pl.DataFrame())

    def load_panel(self, symbols, start, end, columns=None):
        return self.panel

    def simulate_portfolio(self, panel, entries, exits, config, progress_cb=None, cancel_event=None):
        return SimResult(
            equity_curve=[{"date": "2026-08-19", "value": config.initial_capital}],
            drawdown_curve=[{"date": "2026-08-19", "value": 0.0}],
            trades=[],
            per_symbol_stats=[],
            stats={"total_return": 0.0, "n_trades": 0},
        )


class _StrategyEngineStub:
    def __init__(self, strategy: StrategyDef) -> None:
        self._strategies = {strategy.meta["id"]: strategy}

    def get(self, strategy_id: str) -> StrategyDef:
        if strategy_id not in self._strategies:
            raise ValueError(f"unknown strategy: {strategy_id}")
        return self._strategies[strategy_id]


def _bt_panel() -> pl.DataFrame:
    rows = []
    start = date(2026, 7, 1)
    for i in range(40):
        rows.append({
            "symbol": "A",
            "date": start + timedelta(days=i),
            "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0 + i * 0.1,
            "volume": 100_000, "amount": 1_000_000.0,
            "signal_limit_up": False, "signal_limit_down": False,
            "ma20": 9.0,
        })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _run(service, strategy_id: str):
    start = date(2026, 7, 1)
    return service.run(StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=None,
        start=start,
        end=start + timedelta(days=30),
        matching="close_t",
        mode="position",
    ))


def test_backtest_screen_strategy_succeeds():
    s = build_screen_strategy(_screen(conditions=[
        {"field": "close", "op": ">", "value": 10},
        {"field": "above_ma20", "op": "=", "value": True},
    ]))
    service = StrategyBacktestService(
        engine=_EngineStub(_bt_panel()),
        strategy_engine=_StrategyEngineStub(s),
    )
    result = _run(service, s.meta["id"])
    assert result.error is None
    assert result.stats  # 产出统计


def test_backtest_screen_with_external_field_fails_closed():
    # 源头拒绝: 含 ROE (financials join) 的方案 build 时即抛语义错误
    # (sync 不会注册), 不可能进入回测; 回测侧预检 (classify) 对
    # 历史残留的已注册策略仍兜底拒绝。
    with pytest.raises(ScreenerSemanticError) as exc_info:
        build_screen_strategy(_screen(conditions=[
            {"field": "close", "op": ">", "value": 10},
            {"field": "roe", "op": ">", "value": 15},
        ]))
    assert "ROE" in str(exc_info.value)
    # 回测预检兜底: 直接构造带外部字段 screen_record 的策略定义 (模拟
    # 引擎重启窗口期的历史残留), run() 仍须在加载面板前拒绝。
    from app.strategy.engine import StrategyDef
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService

    legacy = StrategyDef(
        meta={"id": "screen:legacy", "name": "历史残留", "params": [], "asset_types": ["stock"]},
        basic_filter={"enabled": False},
        entry_signals=[], exit_signals=[], stop_loss=None, trailing_stop=None,
        trailing_take_profit_activate=None, trailing_take_profit_drawdown=None,
        max_hold_days=None, alerts=[], filter_fn=None, filter_history_fn=None,
        lookback_days=1, source="screen", file_path=None,
    )
    legacy.screen_record = _screen(conditions=[{"field": "roe", "op": ">", "value": 15}])
    service = StrategyBacktestService(
        engine=_EngineStub(_bt_panel()),
        strategy_engine=_StrategyEngineStub(legacy),
    )
    result = _run(service, "screen:legacy")
    assert result.error is not None
    assert "ROE" in result.error
    assert "回测不支持的字段" in result.error


def test_backtest_screen_missing_panel_column_fails_closed():
    # 运行时路径: 预检通过 (字段可算) 但面板缺基础列 → filter_fn 抛错 → 错误结果
    s = build_screen_strategy(_screen(conditions=[
        {"field": "above_ma20", "op": "=", "value": True},
    ]))
    panel = _bt_panel().drop("ma20")
    service = StrategyBacktestService(
        engine=_EngineStub(panel),
        strategy_engine=_StrategyEngineStub(s),
    )
    result = _run(service, s.meta["id"])
    assert result.error is not None
    assert "ma20" in result.error


def test_backtest_non_screen_strategy_keeps_fail_open():
    # 非 screen 策略 filter_fn 抛错 → 维持 fail-open (warning + 空 mask → 无买入信号)
    def bad_filter(df, params):
        raise RuntimeError("boom")

    s = StrategyDef(
        meta={"id": "test", "name": "test", "scoring": {}, "params": [], "limit": 100},
        basic_filter={"enabled": False},
        entry_signals=[], exit_signals=[], stop_loss=None,
        trailing_stop=None, trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None, max_hold_days=None,
        alerts=[], filter_fn=bad_filter, filter_history_fn=None,
        lookback_days=1, source="custom", file_path=None,
    )
    service = StrategyBacktestService(
        engine=_EngineStub(_bt_panel()),
        strategy_engine=_StrategyEngineStub(s),
    )
    result = _run(service, "test")
    # fail-open: 不抛异常, 空 mask → 未产生买入信号
    assert result.error == "在指定区间内未产生买入信号"


def test_screen_strategy_id_format():
    assert screen_strategy_id("abc123def456") == "screen:abc123def456"
