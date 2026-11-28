"""高优先级 Bug 回归测试:
- Bug #4: hfq 共享矩阵切片不二次应用 BackwardAdjustment
- Bug #5: minute_replay._trigger_hhmm 正确处理北京墙钟 naive / tz-aware / 旧版 naive-UTC
- Bug #6: share_capital 缺失 announce_date 不回退 period_end (防未来函数)
- Bug #7: 策略选股排序 nulls_last=True + 矩阵升序策略不二次反转
- Bug #8: 模拟盘 /overview /positions /compare 按最近日K收盘价定版估值 (#590)
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tivelo_backtest.research.matrix import LEGACY_PRICE_SCALE

from app.api.paper import router as paper_router
from app.backtest.engine import BacktestEngine
from app.backtest.matrix import build_market_data_matrix
from app.backtest.minute_replay import _trigger_hhmm
from app.backtest.price_basis import BackwardAdjustment
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.market_time import CN_TZ
from app.share_capital import apply_historical_float_shares
from app.strategy import paper
from app.strategy.engine import StrategyDataContext, StrategyEngine


# ── Bug #4: hfq 共享矩阵不二次后复权 ──────────────────────────────────
def test_hfq_shared_matrix_not_double_adjusted(tmp_path: Path, monkeypatch):
    """market_data_override 已携带 LEGACY_PRICE_SCALE 时, prepare_matrix_optimization 不得再乘一次复权因子。"""
    import app.backtest.strategy as bt_strat

    days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    panel = pl.DataFrame({
        "symbol": ["000001.SZ"] * 3,
        "date": days,
        "open": [20.0, 22.0, 24.0],
        "high": [20.5, 22.5, 24.5],
        "low": [19.5, 21.5, 23.5],
        "close": [20.0, 22.0, 24.0],
        "volume": [1e5, 1e5, 1e5],
        "amount": [2e8, 2.2e8, 2.4e8],
        "name": ["平安银行"] * 3,
    })
    raw_market = build_market_data_matrix(panel)
    scales = np.full(raw_market.shape, 2.0, dtype=np.float32)
    market_override = replace(
        raw_market,
        fields=MappingProxyType({**raw_market.fields, LEGACY_PRICE_SCALE: scales}),
    )

    adj_called = {"count": 0}

    class _BoomAdj(BackwardAdjustment):
        def __init__(self, *args, **kwargs):
            adj_called["count"] += 1
            raise AssertionError("BackwardAdjustment should not run twice on hfq market_data_override")

    monkeypatch.setattr(bt_strat, "BackwardAdjustment", _BoomAdj)

    strat_dir = tmp_path / "strats"
    strat_dir.mkdir()
    (strat_dir / "s_hfq.py").write_text(
        '''
import numpy as np
from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix

META = {
    "id": "s_hfq",
    "name": "s_hfq",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "order_by": "close",
    "descending": True,
    "limit": 10,
}
EXECUTION_BACKEND = "matrix_native"

class _HfqMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset()

    def required_warmup_bars(self, params: dict) -> int:
        return 0

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = np.zeros(market.shape, dtype=np.uint8)
        entry[0, 0] = 1
        return make_signal_matrix(market.shape, entry=entry, score=market.close.copy())

MATRIX_STRATEGY = _HfqMatrixStrategy()
''',
        encoding="utf-8",
    )
    se = StrategyEngine(strategy_dirs=[strat_dir])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    svc = StrategyBacktestService(BacktestEngine(repo), se)

    cfg = StrategyBacktestConfig(
        strategy_id="s_hfq",
        symbols=["000001.SZ"],
        start=days[0],
        end=days[-1],
        price_basis="hfq",
    )
    prepared = svc.prepare_matrix_optimization([cfg], market_data_override=market_override)
    assert adj_called["count"] == 0
    assert float(prepared.market_data.close[0, 0]) == pytest.approx(20.0)


# ── Bug #5: 分钟回测 _trigger_hhmm 北京墙钟不偏移 +8h ──────────────────
def test_trigger_hhmm_beijing_wallclock_and_legacy_compat():
    # 标准北京墙钟 naive (CONTRIBUTING §3.3) 必须保持原时刻, 不能 +8h 变成 17:35 / 22:55
    assert _trigger_hhmm(datetime(2026, 7, 1, 9, 35)) == "09:35"
    assert _trigger_hhmm(datetime(2026, 7, 1, 14, 55)) == "14:55"
    assert _trigger_hhmm(datetime(2026, 7, 1, 11, 30)) == "11:30"
    # tz-aware UTC / Asia/Shanghai 均折算到北京时间
    assert _trigger_hhmm(datetime(2026, 7, 1, 1, 35, tzinfo=UTC)) == "09:35"
    assert _trigger_hhmm(datetime(2026, 7, 1, 9, 35, tzinfo=CN_TZ)) == "09:35"
    # 兼容旧版 naive-UTC 分区 (01:35 → 09:35)
    assert _trigger_hhmm(datetime(2026, 7, 1, 1, 35)) == "09:35"


# ── Bug #6: share_capital 缺失 announce_date 不回退 period_end ─────────
def test_share_capital_does_not_leak_period_end_without_announce_date():
    rows = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "date": [date(2025, 2, 10), date(2025, 5, 10)],
        "float_shares": [100.0, 100.0],
    })
    # 年报 period_end=2024-12-31 但缺 announce_date → 不得泄露到 2025-02-10
    shares_missing_ann = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "period_end": ["2024-12-31", "2025-03-31"],
        "announce_date": [None, "2025-04-28"],
        "float_shares": [200.0, 300.0],
    })
    out = apply_historical_float_shares(rows, shares_missing_ann, today=date(2026, 1, 1))
    assert out["float_shares"].to_list() == [100.0, 300.0]

    # 完全不含 announce_date 列时安全回退原值, 不按 period_end 生效
    shares_no_col = shares_missing_ann.drop("announce_date")
    out_no_col = apply_historical_float_shares(rows, shares_no_col, today=date(2026, 1, 1))
    assert out_no_col["float_shares"].to_list() == [100.0, 100.0]


# ── Bug #7: 选股排序 nulls_last + 矩阵升序策略不二次反转 ───────────────
def test_polars_strategy_sort_puts_null_scores_last(tmp_path: Path):
    strat_dir = tmp_path / "strats"
    strat_dir.mkdir()
    (strat_dir / "s_null.py").write_text(
        '''
import polars as pl

META = {
    "id": "s_null",
    "name": "s_null",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "order_by": "custom_metric",
    "descending": True,
    "limit": 2,
}
EXECUTION_BACKEND = "polars_expr"

def filter(df, params):
    return pl.lit(True)
''',
        encoding="utf-8",
    )
    engine = StrategyEngine(strategy_dirs=[strat_dir])
    current = pl.DataFrame({
        "symbol": ["A", "B", "C"],
        "close": [10.0, 10.0, 10.0],
        "volume": [1e6, 1e6, 1e6],
        "amount": [5e8, 5e8, 5e8],
        "custom_metric": [None, 80.0, 95.0],
    })
    ctx = StrategyDataContext(asset_type="stock", timeframe="1d", as_of=date(2026, 1, 5), current=current)
    res = engine.run("s_null", ctx, overrides={"basic_filter": {"enabled": False}})
    assert [r["symbol"] for r in res.rows] == ["C", "B"]


def test_matrix_strategy_ascending_order_not_double_reversed(tmp_path: Path):
    """descending=False 策略由 build_matrix_score 取负后, _run_matrix_strategy 应按数值最小的标的排第一。"""
    strat_dir = tmp_path / "strats"
    strat_dir.mkdir()
    (strat_dir / "s_asc.py").write_text(
        '''
import numpy as np
from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix

META = {
    "id": "s_asc",
    "name": "s_asc",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "order_by": "close",
    "descending": False,
    "limit": 2,
}
EXECUTION_BACKEND = "matrix_native"

class _AscMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset()

    def required_warmup_bars(self, params: dict) -> int:
        return 0

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = np.ones(market.shape, dtype=np.uint8)
        return make_signal_matrix(market.shape, entry=entry)

MATRIX_STRATEGY = _AscMatrixStrategy()
''',
        encoding="utf-8",
    )
    engine = StrategyEngine(strategy_dirs=[strat_dir])
    day = date(2026, 1, 5)
    history = pl.DataFrame({
        "symbol": ["LOW", "MID", "HIGH"],
        "date": [day, day, day],
        "open": [5.0, 15.0, 30.0],
        "high": [5.5, 15.5, 30.5],
        "low": [4.5, 14.5, 29.5],
        "close": [5.0, 15.0, 30.0],
        "volume": [1e6, 1e6, 1e6],
        "amount": [5e8, 5e8, 5e8],
        "name": ["低价", "中价", "高价"],
    })
    ctx = StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=day, current=history, history=history,
    )
    res = engine.run("s_asc", ctx, overrides={"basic_filter": {"enabled": False}})
    assert [r["symbol"] for r in res.rows] == ["LOW", "MID"]


# ── Bug #8 (Issue #590): 模拟盘定版估值按最近日K收盘价 ────────────────
def _write_daily_kline(data_dir: Path, symbol: str, day: date, close: float) -> None:
    part = data_dir / "kline_daily" / f"date={day.isoformat()}" / "part.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame({
        "symbol": [symbol],
        "date": [day],
        "open": [close],
        "high": [close],
        "low": [close],
        "close": [close],
        "volume": [1000.0],
        "amount": [close * 1000.0 * 100],
    })
    if part.exists():
        df = pl.concat([pl.read_parquet(part), df], how="vertical_relaxed")
    df.write_parquet(part)


def test_paper_valuation_uses_latest_close_consistent_with_nav(tmp_path: Path):
    """非盘中时段 /overview /positions /compare 均按最近日K收盘价估值, 与定版净值一致, 不退化为成本价。"""
    day_buy = date(2026, 11, 20)
    day_end = date(2026, 11, 25)
    _write_daily_kline(tmp_path, "600519.SH", day_buy, 100.0)
    _write_daily_kline(tmp_path, "600519.SH", day_end, 125.0)

    paper.create_account(
        tmp_path, initial_cash=100_000.0, account_id="acc1", name="测试账户",
        commission_pct=0.0, stamp_tax_pct=0.0, slippage_bps=0.0,
    )
    order, err = paper.create_order(
        tmp_path, "600519.SH", "buy", account_id="acc1", qty=500, order_type="close", ref_price=100.0,
    )
    assert err is None and order is not None
    paper.settle_day(tmp_path, day_buy.isoformat(), account_id="acc1")
    nav_end = paper.daily_nav(tmp_path, day_end.isoformat(), account_id="acc1")
    assert nav_end is not None
    # 买入扣除最低佣金 5 元: 现金 49,995 + 500 * 125 = 112,495
    assert nav_end["nav"] == pytest.approx(112_495.0)

    app = FastAPI()
    app.include_router(paper_router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), resolve_asset_type=lambda _: "stock")
    client = TestClient(app)

    ov = client.get("/api/paper/overview?account=acc1").json()
    assert ov["estimating"] is False
    assert ov["total"] == pytest.approx(nav_end["nav"])
    assert ov["holdings"][0]["last_price"] == pytest.approx(125.0)
    assert ov["holdings"][0]["pnl"] == pytest.approx(12_495.0)

    pos = client.get("/api/paper/positions?account=acc1").json()
    assert pos["holdings"][0]["last_price"] == pytest.approx(125.0)

    cmp_rows = client.get("/api/paper/compare").json()["accounts"]
    assert len(cmp_rows) == 1
    assert cmp_rows[0]["total"] == pytest.approx(nav_end["nav"])
    assert cmp_rows[0]["pnl_pct"] == pytest.approx(12.5)
    assert cmp_rows[0]["estimating"] is False
