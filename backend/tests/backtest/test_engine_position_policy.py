"""自定义仓位策略引擎级 — 权重生效 / 预算缩放 / 运行期回退 / 双路径一致。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.position_sizing import PositionSizingResult


def _panel(
    symbols: list[str],
    days: int = 3,
    price: float = 10.0,
    overrides: dict[tuple[str, int], dict] | None = None,
) -> pl.DataFrame:
    overrides = overrides or {}
    start = date(2024, 1, 1)
    rows = []
    for sym in symbols:
        for i in range(days):
            patch = overrides.get((sym, i), {})
            rows.append({
                "symbol": sym,
                "name": sym,
                "date": start + timedelta(days=i),
                "open": patch.get("open", price),
                "high": patch.get("high", price),
                "low": patch.get("low", price),
                "close": patch.get("close", price),
                "volume": patch.get("volume", 100_000),
                "score": patch.get("score", {"A": 4, "B": 3, "C": 2, "D": 1}.get(sym, 0)),
                "signal_limit_up": False,
                "signal_limit_down": False,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _mask(panel: pl.DataFrame, marks: set[tuple[str, int]]) -> pl.Series:
    values = []
    base = date(2024, 1, 1)
    for row in panel.select(["symbol", "date"]).iter_rows(named=True):
        day = (row["date"] - base).days
        values.append((row["symbol"], day) in marks)
    return pl.Series(values, dtype=pl.Boolean)


def _engine() -> BacktestEngine:
    return BacktestEngine(repo=None)  # simulate_portfolio 不访问 repo


def _config(compute, *, params: dict | None = None, **kw) -> MatcherConfig:
    return MatcherConfig(
        matching="open_t+1",
        fees_pct=0,
        slippage_bps=0,
        max_positions=2,
        max_exposure_pct=1.0,
        initial_capital=100_000,
        position_sizing="policy",
        position_policy_compute=compute,
        position_policy_params=params or {},
        **kw,
    )


def test_policy_weights_split_capital_by_ratio() -> None:
    captured: dict = {}

    def compute(ctx):
        captured["syms"] = [c.symbol for c in ctx.selected]
        return PositionSizingResult(weights=[0.75, 0.25], budget_scale=1.0)

    panel = _panel(["A", "B"], days=3)
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0), ("B", 0)}), _mask(panel, set()), _config(compute)
    )
    assert captured["syms"] == ["A", "B"]  # 候选按分数降序 (A=4 > B=3)
    pcts = {t.symbol: t.position_pct for t in result.trades}
    # 权重 0.75 被下游单仓上限钳制到 0.5 (target_value = 权益×敞口/最大持仓数)
    # — 安全钳制语义的顺带验证; B 的 0.25 未触上限按权重生效。
    assert pcts["A"] == pytest.approx(0.5, abs=1e-3)
    assert pcts["B"] == pytest.approx(0.25, abs=1e-3)


def test_policy_budget_scale_shrinks_total_budget() -> None:
    def compute(ctx):
        n = len(ctx.selected)
        return PositionSizingResult(weights=[1.0 / n] * n, budget_scale=0.5)

    panel = _panel(["A", "B"], days=3)
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0), ("B", 0)}), _mask(panel, set()), _config(compute)
    )
    pcts = {t.symbol: t.position_pct for t in result.trades}
    assert pcts["A"] == pytest.approx(0.25, abs=1e-3)
    assert pcts["B"] == pytest.approx(0.25, abs=1e-3)


def test_policy_runtime_error_falls_back_equal_and_counts() -> None:
    def compute(ctx):
        raise RuntimeError("user bug")

    panel = _panel(["A", "B"], days=3)
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0), ("B", 0)}), _mask(panel, set()), _config(compute)
    )
    pcts = {t.symbol: t.position_pct for t in result.trades}
    assert pcts["A"] == pytest.approx(0.5, abs=1e-3)
    assert pcts["B"] == pytest.approx(0.5, abs=1e-3)
    assert result.stats["execution"]["position_policy_fallback"] >= 1


def test_policy_ctx_contents_are_causal() -> None:
    captured: dict = {}

    def compute(ctx):
        captured["ctx"] = ctx
        n = len(ctx.selected)
        return PositionSizingResult(weights=[1.0 / n] * n)

    panel = _panel(["A", "B"], days=5)
    _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 2), ("B", 2)}), _mask(panel, set()), _config(compute)
    )
    ctx = captured["ctx"]
    assert ctx.api_version == 1
    assert [c.symbol for c in ctx.selected] == ["A", "B"]
    assert ctx.portfolio.equity == pytest.approx(100_000.0)
    assert ctx.portfolio.drawdown == pytest.approx(0.0)  # 首日 peak=初始资金
    assert ctx.closed_trades == ()  # 首日尚无平仓
    # 窗口截止到信号日 (day2): day0-day2 三根收盘 → 2 个收益率; 成交日 (day3)
    # 的 K 线属于未来数据, 不得出现 (收益全 0 因价格平坦)。
    assert set(ctx.recent_returns) == {"A", "B"}
    assert len(ctx.recent_returns["A"]) == 2


def test_matrix_and_legacy_parity_with_policy() -> None:
    def vol_inverse(ctx):
        inv = []
        for cand in ctx.selected:
            rets = list(ctx.recent_returns.get(cand.symbol) or [0.01])
            vol = (sum(x * x for x in rets) / max(len(rets), 1)) ** 0.5
            inv.append(1.0 / max(vol, 1e-9))
        total = sum(inv)
        return PositionSizingResult(
            weights=[v / total for v in inv], budget_scale=0.8
        )

    start = date(2024, 1, 1)
    paths = {
        "A": [10.0, 11.0, 12.0, 11.5, 12.2],   # 高波动
        "B": [10.0, 10.2, 10.1, 10.3, 10.2],   # 低波动
    }
    rows = []
    for sym, closes in paths.items():
        for day, close in enumerate(closes):
            rows.append({
                "symbol": sym, "name": sym, "date": start + timedelta(days=day),
                "open": close, "high": close * 1.01, "low": close * 0.99,
                "close": close, "volume": 100_000,
                "score": {"A": 4, "B": 3}[sym],
                "signal_limit_up": False, "signal_limit_down": False,
            })
    panel = pl.DataFrame(rows).sort(["symbol", "date"])
    entries = _mask(panel, {("A", 0), ("B", 0)})
    exits = _mask(panel, set())
    config = _config(vol_inverse)
    engine = _engine()

    matrix_result = engine.simulate_portfolio(panel, entries, exits, config)
    legacy_result = engine.simulate_portfolio_legacy(panel, entries, exits, config)

    # 波动率倒数: B (低波动) 权重更高 — 与等权不同, parity 才有意义
    pcts = {t.symbol: t.position_pct for t in matrix_result.trades}
    assert pcts["B"] > pcts["A"]
    assert [asdict(t) for t in matrix_result.trades] == [asdict(t) for t in legacy_result.trades]
    assert matrix_result.equity_curve == legacy_result.equity_curve
    assert matrix_result.drawdown_curve == legacy_result.drawdown_curve
    assert matrix_result.stats["execution"] == legacy_result.stats["execution"]


def test_missing_policy_compute_behaves_as_equal() -> None:
    """position_sizing='policy' 但未注入 compute (配置直连引擎) → 等权兜底。"""
    panel = _panel(["A", "B"], days=3)
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0), ("B", 0)}), _mask(panel, set()), _config(None)
    )
    pcts = {t.symbol: t.position_pct for t in result.trades}
    assert pcts["A"] == pytest.approx(0.5, abs=1e-3)
    assert pcts["B"] == pytest.approx(0.5, abs=1e-3)
