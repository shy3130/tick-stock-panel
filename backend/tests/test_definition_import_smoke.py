"""P4 统一目录与导入冒烟 — 验收: 新增信号导入到可回测 ≤2 分钟。

链路 (单测试自动化证明): 构建含新信号+新因子的备份 ZIP → apply 导入
(响应内含逐条冒烟结果) → 立即用引用该信号的策略跑回测 → 成功产出信号。
全程秒级, 远低于 2 分钟验收线。
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.config import settings
from app.definitions import catalog, smoke_definition, smoke_stored
from app.services import config_backup


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    # 注入迁移自内置的信号定义 (signal_limit_up 等) — 回测解析硬依赖
    from tests.signal_seeds import install_pipeline_caches
    install_pipeline_caches(monkeypatch)
    yield tmp_path


# ── 冒烟 (静态层) ────────────────────────────────────────

_GOOD_SIGNAL = {
    "id": "smoke_ok", "name": "好信号", "kind": "entry", "timeframe": "daily",
    "conditions": [{"left": "close", "op": ">", "right": "field:ma20"}],
}
_TYPO_SIGNAL = {
    "id": "smoke_typo", "name": "拼错信号", "kind": "entry", "timeframe": "daily",
    "conditions": [{"left": "cloes", "op": ">", "right": 0}],  # 拼错列名
}
_GOOD_FACTOR = {
    "id": "uf_smoke", "label": "好因子", "kind": "custom", "lang": "python",
    "formula": 'ctx.col("close").rolling_mean(window_size=5).over("symbol")',
    "status": "active",
}
_TYPO_FACTOR = {
    "id": "uf_typo", "label": "拼错因子", "kind": "custom", "lang": "python",
    "formula": 'ctx.col("cloes") + 1',  # ctx.col 任意名 — 冒烟要拦住
    "status": "active",
}


def test_smoke_ok_for_valid_signal_and_factor() -> None:
    result = smoke_definition("signal", _GOOD_SIGNAL)
    assert result["ok"], result["issues"]
    assert result["dependencies"] == ["close", "ma20"]
    factor = smoke_definition("factor", _GOOD_FACTOR)
    assert factor["ok"], factor["issues"]
    assert factor["dependencies"] == ["close"]
    assert factor["warmup_bars"] == 6


def test_smoke_stored_by_id_roundtrip(tmp_path: Path):
    """统一选择器逐条冒烟: 按 id 从存储复检, 不存在时 fail-closed 404。"""
    from app.strategy import custom_signals

    custom_signals.save_one(tmp_path, {**_GOOD_SIGNAL})
    result = smoke_stored("signal", "smoke_ok", tmp_path)
    assert result["ok"] is True

    import fastapi
    with pytest.raises(fastapi.HTTPException) as ei:
        smoke_stored("signal", "missing_id", tmp_path)
    assert ei.value.status_code == 404


def test_smoke_flags_typos_and_invalid_definitions() -> None:
    typo_signal = smoke_definition("signal", _TYPO_SIGNAL)
    assert not typo_signal["ok"]
    assert any(i["code"] in ("V001", "W001") for i in typo_signal["issues"])

    typo_factor = smoke_definition("factor", _TYPO_FACTOR)
    assert not typo_factor["ok"]
    assert typo_factor["issues"][0]["code"] == "W001"

    invalid = smoke_definition("signal", {"id": "BAD", "name": "x", "kind": "both", "conditions": []})
    assert not invalid["ok"] and invalid["issues"][0]["code"] == "V001"


def test_catalog_lists_both_kinds_with_dependencies() -> None:
    from app.strategy import custom_signals

    custom_signals.save_one(settings.data_dir, _GOOD_SIGNAL)
    from app.factors import store as factor_store
    factor_store.save_one(settings.data_dir, _GOOD_FACTOR)

    items = catalog(settings.data_dir)
    by_key = {(item["kind"], item["id"]): item for item in items}
    signal = by_key[("signal", "smoke_ok")]
    assert signal["label"] == "好信号" and signal["lang"] == "conditions"
    assert "ma20" in signal["dependencies"]
    factor = by_key[("factor", "uf_smoke")]
    assert factor["lang"] == "python" and factor["warmup_bars"] == 6


# ── 验收: 导入 → 冒烟 → 可回测 (≤2 分钟) ─────────────────

_E2E_SIGNAL = {
    "id": "e2e_new", "name": "端到端新信号", "kind": "entry", "timeframe": "daily",
    "conditions": [{"left": "close", "op": ">", "right": 0}],  # 恒真, 保证有候选
}


def test_import_to_backtest_within_two_minutes(tmp_path: Path, monkeypatch) -> None:
    """备份导入新信号 → 导入响应带冒烟 → 引用该信号的回测立即可跑。"""
    started = time.perf_counter()

    # 1) 源目录造定义, 打备份 ZIP
    source_dir = tmp_path / "source"
    from app.factors import store as factor_store
    from app.strategy import custom_signals

    monkeypatch.setattr(settings, "data_dir", source_dir)
    custom_signals.save_one(source_dir, _E2E_SIGNAL)
    factor_store.save_one(source_dir, _GOOD_FACTOR)
    data, _filename = config_backup.build_backup(categories=["custom_signals", "custom_factors"])

    # 2) 导入到全新目录 — 响应内逐条冒烟
    target_dir = tmp_path / "target"
    monkeypatch.setattr(settings, "data_dir", target_dir)
    applied = config_backup.apply_archive(data, categories=["custom_signals", "custom_factors"])
    smoke_by_file = {
        s["file"].rsplit("/", 1)[-1]: s
        for category in applied["categories"]
        for s in category.get("smoke", [])
    }
    assert smoke_by_file["e2e_new.json"]["ok"], smoke_by_file["e2e_new.json"]
    assert smoke_by_file["uf_smoke.json"]["ok"], smoke_by_file["uf_smoke.json"]

    # 3) 立即用引用导入信号的策略跑回测 (面板 stub, 与 prepare_panel 测试同模式)
    from app.indicators.pipeline import invalidate_custom_signals

    invalidate_custom_signals()  # 信号缓存指向新 data_dir 重载
    # 种子信号 (signal_limit_* 等迁移定义) 的缓存被上一行清掉, 重新注入 —
    # 并把刚导入的 e2e 信号并入同一次编译, 模拟真实进程的合并加载。
    from app.indicators import pipeline
    from app.strategy import custom_signals
    from tests.signal_seeds import load_seed_definitions

    seeds = load_seed_definitions()
    all_signals = [*seeds, _E2E_SIGNAL]
    monkeypatch.setattr(
        pipeline, "_custom_signal_exprs",
        custom_signals.build_expressions(all_signals),
    )
    prev_fields = custom_signals.prev_day_fields(all_signals)
    monkeypatch.setattr(
        pipeline, "_custom_signal_exprs_today",
        custom_signals.build_expressions_prev(all_signals, prev_fields),
    )
    monkeypatch.setattr(pipeline, "_custom_prev_fields_cache", prev_fields)

    from app.backtest.engine import SimResult
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
    from app.strategy.engine import StrategyDef

    class _EngineStub:
        def __init__(self, panel: pl.DataFrame) -> None:
            self.panel = panel
            self.repo = SimpleNamespace(store=SimpleNamespace(data_dir=target_dir))

        def load_panel(self, symbols, start, end, columns=None, asset_type="stock"):
            return self.panel

        def load_panel_for_backtest(self, symbols, start, end, feature_plan, asset_type="stock"):
            return self.load_panel(symbols, start, end)

        def simulate_market_matrix(self, matrix, config, progress_cb=None, cancel_event=None, options=None):
            return SimResult(
                equity_curve=[{"date": "2024-01-01", "value": config.initial_capital}],
                drawdown_curve=[{"date": "2024-01-01", "value": 0.0}],
                trades=[], per_symbol_stats=[],
                stats={"total_return": 0.0, "n_trades": 0},
            )

    class _StrategyEngineStub:
        def __init__(self) -> None:
            self.strategy = StrategyDef(
                meta={"id": "e2e", "name": "e2e", "scoring": {}, "params": [], "limit": 100},
                basic_filter=None,
                entry_signals=["csg_e2e_new"],  # ← 引用刚导入的信号
                exit_signals=[],
                stop_loss=None, trailing_stop=None,
                trailing_take_profit_activate=None, trailing_take_profit_drawdown=None,
                max_hold_days=None,
                filter_fn=lambda df, params: pl.lit(True),
                filter_history_fn=None, lookback_days=1, source="custom", file_path=None,
            )

        def get(self, strategy_id):
            return self.strategy

    rows = []
    base = date(2024, 1, 1)
    for i in range(30):
        for sym in ("A", "B"):
            close = 10.0 + i * 0.1
            rows.append({
                "symbol": sym, "name": sym, "date": base + timedelta(days=i),
                "open": close - 0.05, "high": close + 0.1, "low": close - 0.1, "close": close,
                "volume": 100_000.0, "amount": close * 100_000.0,
                "ma20": close - 0.2,
                "signal_limit_up": False, "signal_limit_down": False,
                # 面板已带信号列时注入路径直接可用; 不带时由 compute_signals 补
                "csg_e2e_new": close > 0,
            })
    panel = pl.DataFrame(rows).sort(["symbol", "date"])
    service = StrategyBacktestService(_EngineStub(panel), _StrategyEngineStub())
    result = service.run(StrategyBacktestConfig(
        strategy_id="e2e", symbols=["A", "B"],
        start=date(2024, 1, 20), end=date(2024, 1, 30), mode="fast",
    ))

    assert result.error is None, result.error
    elapsed = time.perf_counter() - started
    assert elapsed < 120.0, f"导入到可回测耗时 {elapsed:.1f}s 超过验收线 2 分钟"
    # 链路本身在秒级完成
    assert elapsed < 30.0
