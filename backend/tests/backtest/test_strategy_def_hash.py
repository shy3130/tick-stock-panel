"""F13 策略定义指纹 (strategy_def_hash) 测试。

口径:
- 文件型策略 (builtin/custom/ai 目录 .py): 文件内容 sha256 前 12 位
- composite 策略 (含寻优临时组合): 规范化 JSON (sorted keys) sha256 前 12 位,
  子项声明顺序与键序无关
- csg_ 自定义信号定义 JSON: 与 composite 共用 canonical_def_hash 同法
- 回测 run() 把 StrategyDef.def_hash 写入 stats (候选执行模式同样写),
  随 Run 持久化供前端做版本比对
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.backtest.engine import SimResult
from app.backtest.optimizer import make_combo_strategy
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.engine import (
    CompositeChild,
    CompositeSpec,
    StrategyDef,
    StrategyEngine,
    canonical_def_hash,
)


# ── 文件型策略: 引擎加载 ────────────────────────────────


def _engine_with_dir(d: Path) -> StrategyEngine:
    return StrategyEngine(
        enriched_loader=lambda as_of: pl.DataFrame(),
        strategy_dirs=[d],
    )


def test_file_strategy_hash_stable(tmp_path: Path) -> None:
    """文件型策略 hash = 文件内容 sha256 前 12 位, reload 后保持稳定。"""
    d = tmp_path / "custom"
    d.mkdir()
    f = d / "alpha.py"
    f.write_text('META = {"id": "alpha", "name": "alpha"}\n', encoding="utf-8")

    engine = _engine_with_dir(d)
    s = engine.get("alpha")
    expected = hashlib.sha256(f.read_bytes()).hexdigest()[:12]

    assert s.def_hash == expected
    assert len(s.def_hash) == 12
    # 热重载后指纹不变 (同内容)
    engine.reload()
    assert engine.get("alpha").def_hash == expected


def test_file_content_change_changes_hash(tmp_path: Path) -> None:
    """文件内容变化 → 指纹变化; 恢复内容 → 指纹恢复。"""
    d = tmp_path / "custom"
    d.mkdir()
    f = d / "alpha.py"
    original = 'META = {"id": "alpha", "name": "alpha"}\n'
    f.write_text(original, encoding="utf-8")

    engine = _engine_with_dir(d)
    before = engine.get("alpha").def_hash

    f.write_text('META = {"id": "alpha", "name": "alpha", "description": "v2"}\n', encoding="utf-8")
    engine.reload()
    changed = engine.get("alpha").def_hash
    assert changed != before

    f.write_text(original, encoding="utf-8")
    engine.reload()
    assert engine.get("alpha").def_hash == before


# ── composite / 规范化 JSON ─────────────────────────────


def _composite_def(children: tuple[CompositeChild, ...]) -> StrategyDef:
    return StrategyDef(
        meta={"id": "combo", "name": "combo"},
        basic_filter={},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        alerts=[],
        filter_fn=None,
        filter_history_fn=None,
        lookback_days=1,
        source="composite",
        file_path=None,
        execution_backend="composite",
        composite=CompositeSpec(children=children),
    )


def test_composite_hash_order_independent() -> None:
    """composite 子项声明顺序与 JSON 键序均不影响指纹; 权重变化才影响。"""
    low_first = _composite_def((CompositeChild("s_low", 0.3), CompositeChild("s_high", 0.7)))
    high_first = _composite_def((CompositeChild("s_high", 0.7), CompositeChild("s_low", 0.3)))

    expected = canonical_def_hash({
        "children": [
            {"strategy_id": "s_high", "weight": 0.7},
            {"strategy_id": "s_low", "weight": 0.3},
        ],
    })
    assert low_first.def_hash == high_first.def_hash == expected

    reweighted = _composite_def((CompositeChild("s_low", 0.3), CompositeChild("s_high", 0.5)))
    assert reweighted.def_hash != expected


def test_canonical_hash_json_key_order_independent() -> None:
    """csg_ 自定义信号定义 JSON 同法: 键序无关, 内容变化才变。"""
    a = canonical_def_hash({
        "id": "gold_volume",
        "name": "放量黄金",
        "conditions": [{"left": "volume", "op": ">", "right": "100000"}],
        "enabled": True,
    })
    b = canonical_def_hash({
        "enabled": True,
        "conditions": [{"right": "100000", "op": ">", "left": "volume"}],
        "name": "放量黄金",
        "id": "gold_volume",
    })
    assert a == b

    c = canonical_def_hash({
        "id": "gold_volume",
        "name": "放量黄金",
        "conditions": [{"left": "volume", "op": ">", "right": "200000"}],
        "enabled": True,
    })
    assert c != a


# ── 寻优临时组合 ────────────────────────────────────────


def test_ephemeral_combo_hash_covered() -> None:
    """临时组合 (combo:) 同样获得定义指纹: 同子项同权重 → 同指纹。"""
    combo_a = make_combo_strategy("combo:aaaa11112222", ("child_x", "child_y"), merge_mode="union", label="A")
    combo_b = make_combo_strategy("combo:bbbb33334444", ("child_y", "child_x"), merge_mode="union", label="B")

    assert combo_a.def_hash and len(combo_a.def_hash) == 12
    # 子项集合一致 (声明顺序不同) → 定义相同 → 指纹一致
    assert combo_a.def_hash == combo_b.def_hash

    combo_c = make_combo_strategy("combo:cccc55556666", ("child_x", "child_z"), merge_mode="union", label="C")
    assert combo_c.def_hash != combo_a.def_hash


# ── 回测 run stats 携带指纹 ─────────────────────────────


def _panel() -> pl.DataFrame:
    rows = []
    for i in range(4):
        rows.append({
            "symbol": "A", "name": "A",
            "date": date(2024, 1, 2) + timedelta(days=i),
            "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
            "volume": 100_000, "amount": 1000.0,
            "signal_limit_up": False, "signal_limit_down": False,
        })
    return pl.DataFrame(rows).sort(["symbol", "date"])


class _Repo:
    """空行情 repo: 基准降级为 warning, 不影响 run 完成。"""

    def get_index_daily(self, symbol, start, end, columns=None):
        return pl.DataFrame()


class _SimEngine:
    """load_panel + 双模式撮合桩; full 模式标记候选执行口径。"""

    def __init__(self) -> None:
        self.repo = _Repo()
        self.panel = _panel()

    def load_panel(self, symbols, start, end) -> pl.DataFrame:
        return self.panel

    def _result(self, stats: dict) -> SimResult:
        dates = self.panel["date"].to_list()
        last = len(dates) - 1
        return SimResult(
            equity_curve=[
                {"date": str(d)[:10], "value": 1_000_000 * (1.1 if i == last else 1.0)}
                for i, d in enumerate(dates)
            ],
            drawdown_curve=[],
            trades=[],
            per_symbol_stats=[],
            stats=stats,
        )

    def simulate_portfolio(self, panel, entries, exits, config, progress_cb=None, cancel_event=None):
        return self._result({"total_return": 0.1, "n_trades": 1})

    def simulate_independent_candidates(self, panel, entries, exits, config, progress_cb=None, cancel_event=None):
        return self._result({"full_kind": "candidate_execution", "total_return": 0.1})


class _StrategyEngineStub:
    def __init__(self, strategy: StrategyDef) -> None:
        self.strategy = strategy

    def get(self, strategy_id: str) -> StrategyDef:
        return self.strategy


def _stub_strategy(def_hash: str) -> StrategyDef:
    return StrategyDef(
        meta={"id": "test", "name": "test", "scoring": {}, "params": [], "limit": 100},
        basic_filter={"enabled": False},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        alerts=[],
        filter_fn=lambda df, params: pl.lit(True),
        filter_history_fn=None,
        lookback_days=1,
        source="custom",
        file_path=None,
        def_hash=def_hash,
    )


def _service() -> StrategyBacktestService:
    return StrategyBacktestService(
        engine=_SimEngine(),
        strategy_engine=_StrategyEngineStub(_stub_strategy("abc123def456")),
    )


def _cfg(**kwargs) -> StrategyBacktestConfig:
    defaults = dict(
        strategy_id="test", symbols=None,
        start=date(2024, 1, 1), end=date(2024, 1, 31),
        matching="close_t", mode="position",
    )
    defaults.update(kwargs)
    return StrategyBacktestConfig(**defaults)


def test_run_stats_carry_def_hash() -> None:
    """仓位模拟模式: run stats 携带 strategy_def_hash。"""
    result = _service().run(_cfg())
    assert result.error is None
    assert result.stats["strategy_def_hash"] == "abc123def456"


def test_candidate_execution_run_stats_carry_def_hash() -> None:
    """候选执行模式 (full) 同样写入 —— 定义指纹不是时序指标。"""
    result = _service().run(_cfg(mode="full"))
    assert result.error is None
    assert result.stats["full_kind"] == "candidate_execution"
    assert result.stats["strategy_def_hash"] == "abc123def456"
