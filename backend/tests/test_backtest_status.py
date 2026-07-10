from __future__ import annotations

from app.api import backtest as backtest_api


def test_status_reports_legacy_signal_backtest_availability(monkeypatch):
    monkeypatch.setattr(backtest_api, "is_available", lambda: False)

    assert backtest_api.status() == {
        "available": True,
        "strategy_available": True,
        "factor_available": True,
        "signal_available": False,
    }
