from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.indicators.pipeline import (
    ENRICHED_COLUMNS_BY_CATEGORY,
    SIGNAL_DEPENDENCIES,
    compute_enriched_today,
    compute_indicators,
)
from app.tickflow.repository import DataStore, KlineRepository


def _historical_cache(latest: date) -> pl.DataFrame:
    rows = []
    for symbol, days in (("600001.SH", 100), ("000820.SZ", 98)):
        first = latest - timedelta(days=99)
        for offset in range(days):
            trade_date = first + timedelta(days=offset)
            close = 10.0 + offset * 0.01
            rows.append({
                "symbol": symbol,
                "date": trade_date,
                "open": close,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "raw_close": close,
                "raw_high": close + 0.1,
                "raw_low": close - 0.1,
                "volume": 1000.0 + offset,
                "amount": close * (1000.0 + offset),
            })
    return compute_indicators(pl.DataFrame(rows).sort(["symbol", "date"]))


def test_live_agg_uses_each_symbols_last_available_trading_state(tmp_path):
    latest = date(2026, 7, 29)
    repo = KlineRepository(DataStore(tmp_path))
    repo._enriched_history_cache = _historical_cache(latest)

    partition = tmp_path / "kline_daily_enriched" / f"date={latest.isoformat()}"
    partition.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600001.SH"],
        "date": [latest],
        "open": [10.99],
        "high": [11.09],
        "low": [10.89],
        "close": [10.99],
        "volume": [1099.0],
        "amount": [12078.01],
        "raw_close": [10.99],
        "raw_high": [11.09],
        "raw_low": [10.89],
        "turnover_rate": [1.0],
        "consecutive_limit_ups": pl.Series([0], dtype=pl.UInt32),
        "consecutive_limit_downs": pl.Series([0], dtype=pl.UInt32),
    }).write_parquet(partition / "part.parquet")
    resumed_date = latest - timedelta(days=2)
    resumed_partition = (
        tmp_path / "kline_daily_enriched" / f"date={resumed_date.isoformat()}"
    )
    resumed_partition.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["000820.SZ"],
        "date": [resumed_date],
        "open": [10.97],
        "high": [11.07],
        "low": [10.87],
        "close": [10.97],
        "volume": [1097.0],
        "amount": [12034.09],
        "raw_close": [10.97],
        "raw_high": [11.07],
        "raw_low": [10.87],
        "turnover_rate": [1.0],
        "consecutive_limit_ups": pl.Series([2], dtype=pl.UInt32),
        "consecutive_limit_downs": pl.Series([0], dtype=pl.UInt32),
    }).write_parquet(resumed_partition / "part.parquet")

    repo._build_live_agg(latest)

    states = repo.get_live_agg().sort("symbol")
    assert states["symbol"].to_list() == ["000820.SZ", "600001.SH"]
    resumed = states.filter(pl.col("symbol") == "000820.SZ").row(0, named=True)
    assert resumed["_prev_consec_up"] == 2


def _live_state() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600001.SH"],
        "ema5": [10.0],
        "ema10": [10.0],
        "ema20": [10.0],
        "ema30": [10.0],
        "ema60": [10.0],
        "macd_dea": [0.0],
        "kdj_k": [50.0],
        "kdj_d": [50.0],
        "atr_14": [0.2],
        "close": [10.0],
        "high": [10.1],
        "low": [9.9],
        "annual_vol_20d": [0.1],
        "_ema12": [10.0],
        "_ema26": [10.0],
        "_adj_factor": [1.0],
        "_vol_19d_pct_sum": [0.0],
        "_vol_19d_pct_sq_sum": [0.0],
        "_prev_consec_up": pl.Series([0], dtype=pl.UInt32),
        "_prev_consec_down": pl.Series([0], dtype=pl.UInt32),
        "_ma5_partial_sum": [40.0],
        "_ma10_partial_sum": [90.0],
        "_ma20_partial_sum": [190.0],
        "_ma30_partial_sum": [290.0],
        "_ma60_partial_sum": [590.0],
        "_boll_partial_sum": [190.0],
        "_boll_partial_sq_sum": [1900.0],
        "_high_59d": [10.1],
        "_low_59d": [9.9],
        "_close_5d_ago": [10.0],
        "_close_10d_ago": [10.0],
        "_close_20d_ago": [10.0],
        "_close_30d_ago": [10.0],
        "_close_60d_ago": [10.0],
        "_vol_ma5_partial_sum": [4000.0],
        "_vol_ma10_partial_sum": [9000.0],
        "_vol_ma5_prev_sum": [5000.0],
        "_kdj_8d_low": [9.9],
        "_kdj_8d_high": [10.1],
        "_window_len": [59],
        "_rsi_avg_gain_6": [0.01],
        "_rsi_avg_loss_6": [0.01],
        "_rsi_avg_gain_14": [0.01],
        "_rsi_avg_loss_14": [0.01],
        "_rsi_avg_gain_24": [0.01],
        "_rsi_avg_loss_24": [0.01],
    })


def _previous_enriched() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600001.SH"],
        "ma5": [10.0],
        "ma10": [10.0],
        "ma20": [10.0],
        "ma60": [10.0],
        "macd_dif": [0.0],
        "macd_dea": [0.0],
        "boll_upper": [10.2],
        "boll_lower": [9.8],
        "close": [10.0],
    })


def test_realtime_enriched_keeps_rows_without_history_and_limits_technical_fields():
    today = date(2026, 7, 30)
    today_rows = pl.DataFrame({
        "symbol": ["600001.SH", "000820.SZ", "001000.SZ", "600002.SH"],
        "date": [today] * 4,
        "open": [10.1, 11.0, 11.0, 0.0],
        "high": [10.2, 11.0, 11.0, 0.0],
        "low": [10.0, 11.0, 11.0, 0.0],
        "close": [10.2, 11.0, 11.0, 0.0],
        "volume": [1200.0, 3000.0, 2000.0, 0.0],
        "amount": [12240.0, 33000.0, 22000.0, 0.0],
        "prev_close": [10.0, None, 10.0, 10.0],
    })
    instruments = pl.DataFrame({
        "symbol": ["600001.SH", "000820.SZ", "001000.SZ", "600002.SH"],
        "name": ["已有历史", "复牌股票", "上市新股", "停牌股票"],
        "float_shares": [1_000_000.0] * 4,
        "limit_up": [11.0, 11.0, 100000.0, 11.0],
        "limit_down": [9.0, 9.0, 0.0, 9.0],
        "as_of": [today] * 4,
    })

    result = compute_enriched_today(
        _live_state(),
        _previous_enriched(),
        today_rows,
        instruments,
    )

    assert result["symbol"].to_list() == today_rows["symbol"].to_list()
    existing = result.filter(pl.col("symbol") == "600001.SH").row(0, named=True)
    resumed = result.filter(pl.col("symbol") == "000820.SZ").row(0, named=True)
    ipo = result.filter(pl.col("symbol") == "001000.SZ").row(0, named=True)
    halted = result.filter(pl.col("symbol") == "600002.SH").row(0, named=True)

    assert existing["ma5"] is not None
    assert resumed["raw_close"] == 11.0
    assert resumed["signal_limit_up"] is True
    assert resumed["consecutive_limit_ups"] == 1
    technical_columns = {
        column
        for category in (
            "ma", "ema", "macd", "boll", "kdj", "atr", "volume",
            "extremes", "momentum", "volatility", "rsi",
        )
        for column in ENRICHED_COLUMNS_BY_CATEGORY[category]
    } | set(SIGNAL_DEPENDENCIES)
    assert all(resumed[column] is None for column in technical_columns)
    assert ipo["signal_limit_up"] is False
    assert halted["signal_limit_up"] is not True
    assert "_has_history_state" not in result.columns


def test_repository_restores_active_rows_missing_from_latest_enriched(tmp_path):
    today = date(2026, 7, 30)
    repo = KlineRepository(DataStore(tmp_path))
    repo._live_agg_cache = _live_state()
    repo._instruments_cache = pl.DataFrame({
        "symbol": ["600001.SH", "000820.SZ", "600002.SH"],
        "name": ["已有历史", "复牌股票", "停牌股票"],
        "float_shares": [1_000_000.0] * 3,
        "limit_up": [11.0, 11.0, 11.0],
        "limit_down": [9.0, 9.0, 9.0],
        "as_of": [today] * 3,
    })
    daily_partition = tmp_path / "kline_daily" / f"date={today.isoformat()}"
    daily_partition.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600001.SH", "000820.SZ", "600002.SH"],
        "date": [today] * 3,
        "open": [10.2, 11.0, 0.0],
        "high": [10.2, 11.0, 0.0],
        "low": [10.2, 11.0, 0.0],
        "close": [10.2, 11.0, 0.0],
        "volume": [1200.0, 3000.0, 0.0],
        "amount": [12240.0, 33000.0, 0.0],
    }).write_parquet(daily_partition / "part.parquet")
    existing_today = compute_enriched_today(
        _live_state(),
        _previous_enriched(),
        pl.read_parquet(daily_partition / "part.parquet").head(1),
        repo._instruments_cache,
    )
    history = _previous_enriched().with_columns(
        pl.lit(today - timedelta(days=1)).alias("date"),
    )

    restored = repo._restore_missing_latest_rows(today, existing_today, history)

    assert restored["symbol"].to_list() == ["000820.SZ", "600001.SH"]
    resumed = restored.filter(pl.col("symbol") == "000820.SZ").row(0, named=True)
    assert resumed["signal_limit_up"] is True
    assert resumed["ma5"] is None
