"""Compatibility checks for the signal API using the actual Tivelo matcher."""
from datetime import date

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.backtest import router
from app.services.backtest import BacktestService, settings

DAYS = [date(2025, 1, day) for day in (2, 3, 6, 7, 8)]
STOCK = "600000.SH"
OTHER = "600001.SH"


def bars(symbol=STOCK, **columns):
    return pl.DataFrame({
        "symbol": [symbol] * len(DAYS),
        "date": DAYS,
        "open": [10.0] * len(DAYS),
        "high": [10.5] * len(DAYS),
        "low": [9.9] * len(DAYS),
        "close": [10.0] * len(DAYS),
        "volume": [1000.0] * len(DAYS),
        "signal_macd_golden": [True, False, False, False, False],
        "signal_macd_dead": [False] * len(DAYS),
        "signal_limit_up": [False] * len(DAYS),
        "signal_limit_down": [False] * len(DAYS),
        **columns,
    })


@pytest.fixture
def run_signal(monkeypatch, tmp_path):
    app = FastAPI()
    app.state.repo = object()
    app.include_router(router)
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    def run(panel, **params):
        monkeypatch.setattr(BacktestService, "_load_panel", lambda *args: panel)
        response = TestClient(app).post("/api/backtest/run", json={
            "symbols": [STOCK, OTHER],
            "start": DAYS[0].isoformat(),
            "end": DAYS[-1].isoformat(),
            "entries": ["macd_golden"],
            "exits": ["macd_dead"],
            "fees_pct": 0,
            "slippage_bps": 0,
            **params,
        })
        assert response.status_code == 200, response.text
        return response.json()

    return run


@pytest.mark.parametrize(("hold_days", "exit_day"), [(1, 1), (2, 2), (20, 4)])
def test_max_hold_uses_elapsed_trading_bars(run_signal, hold_days, exit_day):
    result = run_signal(bars(), max_hold_days=hold_days)
    (trade,) = result["trades"]
    assert trade["entry_date"][:10] == DAYS[0].isoformat()
    assert trade["exit_date"][:10] == DAYS[exit_day].isoformat()
    assert trade["duration"] == str(exit_day)


def test_next_open_accounts_for_missing_bars_costs_and_idle_symbols(run_signal):
    panel = bars(
        open=[10.0, 10.0, 12.0, 13.0, 13.0],
        close=[10.0, 10.0, 12.5, 13.5, 13.5],
        low=[9.9, 9.9, 11.9, 12.9, 12.9],
        high=[10.5, 10.5, 12.6, 13.6, 13.6],
        signal_macd_dead=[False, False, True, False, False],
    ).filter(pl.col("date") != DAYS[1])
    panel = pl.concat([panel, bars(OTHER, signal_macd_golden=[False] * len(DAYS))])
    result = run_signal(panel, matching="open_t+1", fees_pct=0.002, slippage_bps=10)
    (trade,) = result["trades"]
    assert (trade["entry_date"][:10], trade["exit_date"][:10]) == (
        DAYS[2].isoformat(), DAYS[3].isoformat(),
    )
    assert (trade["entry_price"], trade["exit_price"]) == (12.0, 13.0)
    cost, proceeds = 12 * 1.003, 13 * 0.997
    assert trade["pnl_pct"] == pytest.approx(proceeds / cost - 1, abs=1e-6)
    initial_capital = 1_000_000
    shares = (initial_capital // (100 * cost)) * 100
    total_return = shares * (proceeds - cost) / initial_capital
    assert result["per_symbol_stats"] == [
        {"symbol": STOCK, "total_return": pytest.approx(total_return)},
        {"symbol": OTHER, "total_return": 0},
    ]
    values = [point["value"] for point in result["equity_curve"]]
    assert values == pytest.approx([
        100, 100, 100 * (1 + shares * (12.5 - cost) / (2 * initial_capital)),
        100 * (1 + total_return / 2), 100 * (1 + total_return / 2),
    ])
    assert result["stats"]["Total Return [%]"] == pytest.approx(100 * total_return / 2)


def test_stop_loss_uses_intraday_low_from_first_sellable_day(run_signal):
    panel = bars(low=[9.0, 9.0, 9.9, 9.9, 9.9])
    result = run_signal(panel, stop_loss_pct=-0.05)
    (trade,) = result["trades"]
    assert trade["exit_date"][:10] == DAYS[1].isoformat()
    assert trade["exit_price"] == pytest.approx(9.5)
    assert trade["pnl_pct"] == pytest.approx(-0.05)


def test_limit_up_buy_and_limit_down_exit_follow_matching_rules(run_signal):
    panel = bars(
        signal_macd_golden=[True, True, False, False, False],
        signal_macd_dead=[False, False, True, False, False],
        signal_limit_up=[True, False, False, False, False],
        signal_limit_down=[False, False, True, False, False],
    )
    result = run_signal(panel)
    (trade,) = result["trades"]
    assert trade["entry_date"][:10] == DAYS[1].isoformat()
    assert trade["exit_date"][:10] == DAYS[3].isoformat()


def test_empty_results_keep_existing_error_payload(run_signal):
    for panel, error in ((pl.DataFrame(), "no data"), (bars(), "no buy signals")):
        result = run_signal(panel, entries=[])
        assert result["stats"] == {"error": error}
        assert result["equity_curve"] == result["trades"] == result["per_symbol_stats"] == []
