from app.services.screener import (
    PRESET_STRATEGIES,
    strategy_supports_asset,
)


def test_all_presets_have_asset_types():
    for sid, strat in PRESET_STRATEGIES.items():
        assert "asset_types" in strat, f"{sid} 缺 asset_types"
        assert "stock" in strat["asset_types"], f"{sid} 必须支持 stock"


def test_limit_up_strategies_are_stock_only():
    for sid in ("broken_board_recovery", "consecutive_limit_ups"):
        assert PRESET_STRATEGIES[sid]["asset_types"] == ["stock"]


def test_pure_technical_strategies_support_etf():
    for sid in (
        "trend_breakout", "ma_golden_cross", "macd_golden",
        "volume_price_surge", "low_volatility_leader", "oversold_bounce",
        "boll_breakout", "bullish_alignment", "pullback_to_support",
        "n_day_low_reversal",
    ):
        assert "etf" in PRESET_STRATEGIES[sid]["asset_types"], sid


def test_strategy_supports_asset_defaults_to_stock():
    assert strategy_supports_asset({}, "stock") is True
    assert strategy_supports_asset({}, "etf") is False
    assert strategy_supports_asset({"asset_types": ["stock", "etf"]}, "etf") is True


import types
from datetime import date

import polars as pl

from app.services.screener import ScreenerService


class _FakeRepo:
    """最小 repo 桩：只实现 screener 用到的 _asset 取数接口。"""

    def __init__(self, data_dir, enriched=None, instruments=None, latest=None):
        self.store = types.SimpleNamespace(data_dir=data_dir)
        self._enriched = enriched if enriched is not None else pl.DataFrame()
        self._instruments = instruments if instruments is not None else pl.DataFrame()
        self._latest = latest

    def get_enriched_latest_asset(self, asset_type):
        return self._enriched, self._latest

    def get_instruments_asset(self, asset_type):
        return self._instruments

    def get_enriched_history(self, target_date, lookback_days):
        return None  # stock 缓存；ETF 分支不应调用它


def test_service_defaults_to_stock_dir(tmp_path):
    svc = ScreenerService(_FakeRepo(tmp_path))
    assert svc.asset_type == "stock"
    assert svc._enriched_dirname == "kline_daily_enriched"


def test_service_etf_uses_etf_dir(tmp_path):
    svc = ScreenerService(_FakeRepo(tmp_path), asset_type="etf")
    assert svc.asset_type == "etf"
    assert svc._enriched_dirname == "kline_etf_enriched"


def test_etf_run_preset_empty_data_degrades(tmp_path):
    """ETF enriched 为空时，run_preset 返回空结果而非抛错。"""
    svc = ScreenerService(_FakeRepo(tmp_path), asset_type="etf")
    result = svc.run_preset("trend_breakout", as_of=date(2026, 1, 2))
    assert result.total == 0
    assert result.rows == []


def test_etf_run_preset_filters_rows(tmp_path):
    """给一份含技术列的 ETF enriched，趋势突破策略能选出命中行。"""
    enriched = pl.DataFrame({
        "symbol": ["510300", "159915"],
        "name": ["沪深300ETF", "创业板ETF"],
        "date": [date(2026, 1, 2), date(2026, 1, 2)],
        "close": [4.0, 2.0],
        "open": [3.9, 2.1],
        "ma60": [3.5, 2.5],
        "signal_n_day_high": [True, False],
        "vol_ratio_5d": [2.5, 0.5],
        "momentum_60d": [0.2, -0.1],
    })
    repo = _FakeRepo(tmp_path, enriched=enriched, latest=date(2026, 1, 2))
    svc = ScreenerService(repo, asset_type="etf")
    result = svc.run_preset("trend_breakout", as_of=date(2026, 1, 2))
    assert result.total == 1
    assert result.rows[0]["symbol"] == "510300"


def test_strategies_filtered_for_etf():
    etf_ids = [sid for sid, s in PRESET_STRATEGIES.items()
               if strategy_supports_asset(s, "etf")]
    assert "trend_breakout" in etf_ids
    assert "consecutive_limit_ups" not in etf_ids
    assert len(etf_ids) == 10


def test_run_preset_stock_only_strategy_on_etf_returns_empty(tmp_path):
    """对 ETF 跑股票专有策略（连板）应返回空结果，而非误命中或抛错。"""
    enriched = pl.DataFrame({
        "symbol": ["510300"],
        "date": [date(2026, 1, 2)],
        "close": [4.0],
    })
    repo = _FakeRepo(tmp_path, enriched=enriched, latest=date(2026, 1, 2))
    svc = ScreenerService(repo, asset_type="etf")
    result = svc.run_preset("consecutive_limit_ups", as_of=date(2026, 1, 2))
    assert result.total == 0
