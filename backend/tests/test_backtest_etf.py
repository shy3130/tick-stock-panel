import types
from datetime import date

import polars as pl

from app.services.backtest import BacktestConfig
from app.backtest.engine import BacktestEngine, PanelCache
from app.backtest.factor import FactorConfig
from app.backtest.strategy import StrategyBacktestConfig


def test_configs_default_to_stock():
    assert BacktestConfig(symbols=[], start=date(2026, 1, 1), end=date(2026, 1, 2)).asset_type == "stock"
    assert FactorConfig(factor_name="x", symbols=None, start=date(2026, 1, 1), end=date(2026, 1, 2)).asset_type == "stock"
    assert StrategyBacktestConfig(strategy_id="x", symbols=None, start=date(2026, 1, 1), end=date(2026, 1, 2)).asset_type == "stock"


def test_panel_cache_key_isolates_asset_type():
    args = (["510300"], date(2026, 1, 1), date(2026, 1, 2), None)
    k_stock = PanelCache._make_key(*args, "stock")
    k_etf = PanelCache._make_key(*args, "etf")
    assert k_stock != k_etf
    assert k_etf.startswith("etf:")
    assert k_stock.startswith("stock:")


def test_engine_loads_from_etf_dir(monkeypatch, tmp_path):
    """asset_type='etf' 时, load_panel 应扫 ETF enriched 目录, 不走 stock 缓存。"""
    captured = {}

    def fake_scan(path, *a, **k):
        captured["path"] = str(path)
        return pl.LazyFrame({
            "symbol": pl.Series("symbol", [], dtype=pl.Utf8),
            "date": pl.Series("date", [], dtype=pl.Date),
            "open": pl.Series("open", [], dtype=pl.Float64),
            "high": pl.Series("high", [], dtype=pl.Float64),
            "low": pl.Series("low", [], dtype=pl.Float64),
            "close": pl.Series("close", [], dtype=pl.Float64),
            "volume": pl.Series("volume", [], dtype=pl.Float64),
        })

    monkeypatch.setattr("app.backtest.engine.pl.scan_parquet", fake_scan)

    # get_enriched_range 返回 None: 即便被调也不命中缓存; etf 分支本就不该调它
    repo = types.SimpleNamespace(
        store=types.SimpleNamespace(data_dir=tmp_path),
        get_enriched_range=lambda *a, **k: None,
    )
    eng = BacktestEngine(repo)
    eng._load_panel_inner(["510300"], date(2026, 1, 1), date(2026, 1, 2), None, "etf")
    assert "kline_etf_enriched" in captured["path"]


def test_engine_stock_uses_daily_enriched_dir(monkeypatch, tmp_path):
    captured = {}

    def fake_scan(path, *a, **k):
        captured["path"] = str(path)
        return pl.LazyFrame({
            "symbol": pl.Series("symbol", [], dtype=pl.Utf8),
            "date": pl.Series("date", [], dtype=pl.Date),
            "open": pl.Series("open", [], dtype=pl.Float64),
            "high": pl.Series("high", [], dtype=pl.Float64),
            "low": pl.Series("low", [], dtype=pl.Float64),
            "close": pl.Series("close", [], dtype=pl.Float64),
            "volume": pl.Series("volume", [], dtype=pl.Float64),
        })

    monkeypatch.setattr("app.backtest.engine.pl.scan_parquet", fake_scan)
    repo = types.SimpleNamespace(
        store=types.SimpleNamespace(data_dir=tmp_path),
        get_enriched_range=lambda *a, **k: None,
    )
    eng = BacktestEngine(repo)
    eng._load_panel_inner(["600519"], date(2026, 1, 1), date(2026, 1, 2), None, "stock")
    assert "kline_daily_enriched" in captured["path"]


def test_job_key_includes_asset_type_and_is_consistent():
    """stream 与 cancel 必须用同一 job_key: asset_type 进 key 且相同入参产出相同 key。"""
    from app.api.backtest import _make_job_key

    args = ("s1", None, None, None, "open_t+1", None, None,
            0.0002, 5.0, 10, 1.0, 1_000_000.0, "equal", None, None,
            "position", 5, None, None)
    k_stock = _make_job_key(*args, asset_type="stock")
    k_etf = _make_job_key(*args, asset_type="etf")
    assert k_stock != k_etf
    # 相同参数(含 asset_type)必须产出相同 key —— stream 端与 cancel 端对齐的前提
    assert _make_job_key(*args, asset_type="etf") == k_etf
