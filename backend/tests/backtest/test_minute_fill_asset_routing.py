"""分钟K精确成交按回测资产类型路由分钟分区 (CONTRIBUTING §3.3)。

回测页可选 ETF + 「高精度」(minute_fill)。撮合层读分钟K时:
  - 全量模式 (_simulate_independent_matrix) 写死 asset_type="stock";
  - 持仓模式 (_simulate_portfolio_matrix) 按代码格式猜 (".SH" 且 "5" 开头才算 ETF),
    深市 ETF (159xxx.SZ) 被当成股票。
两条路径都去股票分钟分区找 ETF 的分钟K, 找不到就静默退回日K价成交,
「高精度」对 ETF 不生效且无任何提示。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.engine import StrategyDef

START = date(2024, 1, 1)
DAILY_OPEN = 11.0
MINUTE_VWAP = 10.5


class _MinuteRepo:
    """只在 ETF 分钟分区里有数据 (与真实仓库按资产类型分开存储一致)。"""

    def __init__(self) -> None:
        self.asset_types: list[str] = []

    def get_index_daily(self, *args, **kwargs) -> pl.DataFrame:
        return pl.DataFrame()

    def get_minute_by_dates(self, symbols, dates, asset_type="stock"):
        self.asset_types.append(asset_type)
        if asset_type != "etf":
            return pl.DataFrame()
        day = START + timedelta(days=1)
        return pl.DataFrame({
            "symbol": [symbols[0], symbols[0]],
            "datetime": [datetime.combine(day, datetime.min.time()).replace(hour=9, minute=31),
                         datetime.combine(day, datetime.min.time()).replace(hour=9, minute=32)],
            "open": [10.4, 10.6],
            "high": [10.5, 10.7],
            "low": [10.3, 10.5],
            "close": [10.4, 10.6],
            "volume": [100.0, 100.0],
            # 契约口径: amount(元) = 价 x volume(手) x 100 股 (#387)
            "amount": [10.4 * 100.0 * 100, 10.6 * 100.0 * 100],
        })


def _panel(symbol: str) -> pl.DataFrame:
    rows = []
    for i, price in enumerate([10.0, DAILY_OPEN, 12.0]):
        rows.append({
            "symbol": symbol, "name": symbol, "date": START + timedelta(days=i),
            "open": price, "high": price, "low": price, "close": price,
            "volume": 1_000, "amount": 1e6,
            "signal_limit_up": False, "signal_limit_down": False,
        })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _strategy() -> StrategyDef:
    return StrategyDef(
        meta={"id": "etf_test", "name": "etf_test", "scoring": {}, "params": [], "limit": 100,
              "asset_types": ["stock", "etf"]},
        basic_filter={},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=1,
        filter_fn=lambda df, params: pl.col("date") == START,
        filter_history_fn=None,
        lookback_days=1,
        source="custom",
        file_path=None,
    )


class _StrategyEngineStub:
    def get(self, strategy_id: str) -> StrategyDef:
        return _strategy()


@pytest.mark.parametrize(
    ("mode", "symbol"),
    [
        ("position", "159915.SZ"),
        ("full", "159915.SZ"),
        ("full", "510300.SH"),
    ],
    ids=["position-sz-etf", "full-sz-etf", "full-sh-etf"],
)
def test_etf_minute_fill_reads_etf_minute_partition(mode, symbol):
    repo = _MinuteRepo()
    engine = BacktestEngine(repo=repo)  # type: ignore[arg-type]
    panel = _panel(symbol)
    engine.load_panel_for_backtest = lambda symbols, s, e, plan, asset_type="stock": panel  # type: ignore[method-assign]
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub())

    result = service.run(StrategyBacktestConfig(
        strategy_id="etf_test",
        symbols=[symbol],
        start=START,
        end=START + timedelta(days=2),
        mode=mode,
        matching="open_t+1",
        fees_pct=0,
        slippage_bps=0,
        holding_days=1,
        max_positions=1,
        asset_type="etf",
        minute_fill=True,
    ))

    assert result.error is None
    assert repo.asset_types and set(repo.asset_types) == {"etf"}
    assert len(result.trades) == 1
    assert result.trades[0]["entry_price"] == pytest.approx(MINUTE_VWAP)  # 分钟 VWAP, 不是日K开盘 11.0
