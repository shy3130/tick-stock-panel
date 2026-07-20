from __future__ import annotations

import gc
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import polars as pl
import pytest

from app.backtest import matrix as matrix_module
from app.backtest.matrix import (
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    RealtimeMarketDataMatrix,
    apply_time_masks,
    build_market_data_matrix,
    build_matrix_score,
    load_market_data_matrix_from_parquet,
    make_signal_matrix,
    matrix_feature,
    slice_signal_matrix,
    validate_signal_matrix,
)
from app.backtest.strategy import build_matrix_cache_profile
from app.indicators.pipeline import (
    compute_indicators,
    compute_limit_signals,
)
from app.indicators.pipeline import (
    compute_signals as compute_indicator_signals,
)
from app.strategy.engine import StrategyDataContext, StrategyEngine

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_common_matrix_features_match_polars_indicator_pipeline():
    rows = []
    start = date(2024, 1, 1)
    for offset in range(100):
        close = 10.0 + offset * 0.03 + np.sin(offset / 4.0) * 0.8
        rows.append({
            "symbol": "000001.SZ",
            "date": start + timedelta(days=offset),
            "open": close - 0.1,
            "high": close + 0.3 + (offset % 3) * 0.02,
            "low": close - 0.25,
            "close": close,
            "volume": 1000.0 + (offset % 7) * 130.0,
        })
    panel = pl.DataFrame(rows)
    features = {
        "prev_close", "change_pct", "change_amount", "amplitude",
        "ma5", "ma20", "ma60", "boll_upper", "boll_lower",
        "high_60d", "low_60d", "momentum_60d", "vol_ratio_5d",
        "annual_vol_20d", "rsi_14",
    }
    enriched = compute_indicators(panel, needed=features)
    market = build_market_data_matrix(panel)

    for name in sorted(features):
        expected = enriched.sort(["date", "symbol"])[name].to_numpy()
        actual = matrix_feature(market, name)[:, 0]
        np.testing.assert_allclose(actual, expected, rtol=2e-5, atol=2e-5, equal_nan=True)

    expected_bias = (
        enriched.sort(["date", "symbol"])["close"].to_numpy()
        / enriched.sort(["date", "symbol"])["ma20"].to_numpy()
        - 1.0
    )
    np.testing.assert_allclose(
        matrix_feature(market, "ma20_bias")[:, 0],
        expected_bias,
        rtol=2e-5,
        atol=2e-5,
        equal_nan=True,
    )


def _panel_with_missing_asset_bar() -> pl.DataFrame:
    rows = []
    start = date(2024, 1, 1)
    for offset in range(110):
        for asset_id, symbol in enumerate(("000001.SZ", "600000.SH")):
            if symbol == "000001.SZ" and offset == 43:
                continue
            if asset_id == 0:
                close = (
                    20.0 - offset * 0.15
                    if offset < 35
                    else 14.75 + (offset - 35) * 0.25
                )
            else:
                close = 18.0 + offset * 0.025 + np.sin(offset / 5.0) * 0.7
            rows.append({
                "symbol": symbol,
                "date": start + timedelta(days=offset),
                "open": close - 0.1,
                "high": close + 0.3,
                "low": close - 0.25,
                "close": close,
                "volume": 1000.0 + asset_id * 200.0 + (offset % 7) * 130.0,
            })
    return pl.DataFrame(rows)


def test_matrix_features_skip_missing_asset_bars_like_polars_groups():
    panel = _panel_with_missing_asset_bar()
    features = {
        "prev_close", "change_pct", "change_amount", "amplitude",
        "ma5", "ma20", "ma60", "boll_upper", "boll_lower",
        "high_60d", "low_60d", "momentum_60d", "vol_ratio_5d",
        "annual_vol_20d", "rsi_14",
    }
    enriched = compute_indicators(panel, needed=features)
    market = build_market_data_matrix(panel)
    time_id_by_date = {
        label[:10]: time_id
        for time_id, label in enumerate(market.timestamp_labels)
    }

    for symbol in market.symbols:
        expected_rows = enriched.filter(pl.col("symbol") == symbol).sort("date")
        time_ids = np.array(
            [time_id_by_date[str(value)] for value in expected_rows["date"]],
            dtype=np.intp,
        )
        asset_id = market.symbols.index(symbol)
        for name in sorted(features):
            expected = expected_rows[name].to_numpy()
            actual = matrix_feature(market, name)[time_ids, asset_id]
            np.testing.assert_allclose(
                actual,
                expected,
                rtol=2e-5,
                atol=2e-5,
                equal_nan=True,
                err_msg=f"{symbol} {name}",
            )

    missing_time_id = time_id_by_date["2024-02-13"]
    missing_asset_id = market.symbols.index("000001.SZ")
    for name in features:
        assert np.isnan(matrix_feature(market, name)[missing_time_id, missing_asset_id])


def test_matrix_pipeline_builds_and_reuses_one_compact_valid_bar_index():
    panel = _panel_with_missing_asset_bar()
    market = build_market_data_matrix(panel)
    base_bytes = market.nbytes
    strategy = StrategyEngine._load_file(
        REPO_ROOT / "backend" / "app" / "strategy" / "builtin" / "ma_golden_cross.py"
    ).matrix_strategy

    with patch.object(
        matrix_module,
        "_build_valid_bar_index",
        wraps=matrix_module._build_valid_bar_index,
    ) as build_index:
        MatrixStrategyPipeline().run(
            strategy,
            market,
            {
                "require_ma_golden": True,
                "use_volume_filter": False,
                "require_above_ma60": False,
            },
            MatrixPipelineConfig(
                basic_filter={"enabled": False},
                scoring={},
                order_by=None,
                descending=True,
            ),
        )

    index = market.valid_bars
    assert build_index.call_count == 1
    assert index.rows.size == panel.height
    assert index.offsets.tolist() == [0, 109, 219]
    assert market.valid_bars is index
    assert market.nbytes == base_bytes + index.nbytes


@pytest.mark.parametrize(
    ("strategy_file", "entry_column", "exit_column", "params"),
    [
        (
            "ma_golden_cross.py",
            "signal_ma_golden_5_20",
            "signal_ma_dead_5_20",
            {
                "require_ma_golden": True,
                "use_volume_filter": False,
                "require_above_ma60": False,
            },
        ),
        (
            "macd_golden.py",
            "signal_macd_golden",
            "signal_macd_dead",
            {"require_macd_golden": True, "use_volume_filter": False},
        ),
    ],
)
def test_matrix_crossovers_skip_missing_asset_bars_like_polars_signals(
    strategy_file: str,
    entry_column: str,
    exit_column: str,
    params: dict,
):
    panel = _panel_with_missing_asset_bar()
    indicator_names = (
        {"ma5", "ma20"}
        if strategy_file == "ma_golden_cross.py"
        else {"macd_dif", "macd_dea"}
    )
    enriched = compute_indicators(panel, needed=indicator_names)
    expected = compute_indicator_signals(
        enriched,
        needed={entry_column, exit_column},
    )
    market = build_market_data_matrix(panel)
    strategy_def = StrategyEngine._load_file(
        REPO_ROOT / "backend" / "app" / "strategy" / "builtin" / strategy_file
    )
    actual = strategy_def.matrix_strategy.compute_signals(market, params)
    time_id_by_date = {
        label[:10]: time_id
        for time_id, label in enumerate(market.timestamp_labels)
    }

    for symbol in market.symbols:
        expected_rows = expected.filter(pl.col("symbol") == symbol).sort("date")
        time_ids = np.array(
            [time_id_by_date[str(value)] for value in expected_rows["date"]],
            dtype=np.intp,
        )
        asset_id = market.symbols.index(symbol)
        expected_entry = (
            expected_rows[entry_column].fill_null(False).cast(pl.UInt8).to_numpy()
        )
        expected_exit = (
            expected_rows[exit_column].fill_null(False).cast(pl.UInt8).to_numpy()
        )
        np.testing.assert_array_equal(actual.entry[time_ids, asset_id], expected_entry)
        np.testing.assert_array_equal(actual.exit[time_ids, asset_id], expected_exit)

    missing_time_id = time_id_by_date["2024-02-13"]
    missing_asset_id = market.symbols.index("000001.SZ")
    assert actual.entry[missing_time_id, missing_asset_id] == 0
    assert actual.exit[missing_time_id, missing_asset_id] == 0
    assert actual.entry.any() or actual.exit.any()


def test_builtin_matrix_strategies_use_their_declared_formula_modules():
    strategy_dir = REPO_ROOT / "backend" / "app" / "strategy" / "builtin"
    strategy_files = sorted(
        path for path in strategy_dir.glob("*.py") if path.name != "__init__.py"
    )

    assert len(strategy_files) == 18
    for strategy_path in strategy_files:
        strategy = StrategyEngine._load_file(strategy_path)
        assert strategy.execution_backend == "matrix_native"
        assert strategy.matrix_strategy is not None
        assert strategy.matrix_strategy.__class__.__module__ == strategy_path.stem
        assert strategy.filter_fn is None
        assert strategy.filter_history_fn is None


def test_market_matrix_derives_live_raw_close_when_requested():
    panel = pl.DataFrame({
        "symbol": ["000001.SZ", "600000.SH"],
        "date": [date(2024, 1, 1)] * 2,
        "open": [10.0, 20.0],
        "high": [10.2, 20.2],
        "low": [9.8, 19.8],
        "close": [10.1, 20.1],
        "volume": [1_000.0, 2_000.0],
    })
    market = build_market_data_matrix(panel, field_columns={"raw_close"})

    np.testing.assert_array_equal(market.field("raw_close"), market.close)

    live = RealtimeMarketDataMatrix(
        panel,
        field_columns={"raw_close"},
    )
    live.update(panel.with_columns(pl.lit(date(2024, 1, 2)).alias("date")))
    snapshot = live.snapshot()
    np.testing.assert_array_equal(snapshot.field("raw_close")[-1], snapshot.close[-1])


def test_direct_parquet_matrix_matches_panel_builder_and_reuses_mmap(tmp_path):
    market_root = tmp_path / "kline_daily_enriched"
    days = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
    rows = []
    closes = {
        "000001.SZ": (10.0, 11.0, 12.0),
        "000002.SZ": (10.0, 10.5, None),
        "300001.SZ": (10.0, 12.0, 12.2),
    }
    for symbol, values in closes.items():
        for current, close in zip(days, values, strict=True):
            if close is None:
                continue
            rows.append({
                "symbol": symbol,
                "date": current,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1_000.0,
                "amount": close * 100_000.0,
                "raw_close": close,
                "raw_high": close,
                "raw_low": close,
                "turnover_rate": 1.5,
            })
    panel = pl.DataFrame(rows).sort(["symbol", "date"])
    for current in days:
        partition = market_root / f"date={current.isoformat()}"
        partition.mkdir(parents=True)
        panel.filter(pl.col("date") == current).write_parquet(partition / "part.parquet")

    instruments = pl.DataFrame({
        "symbol": ["000001.SZ", "000002.SZ", "300001.SZ"],
        "name": ["普通", "ST测试", "创业板"],
        "total_shares": [1_000_000.0, 2_000_000.0, 3_000_000.0],
        "float_shares": [800_000.0, 1_500_000.0, 2_000_000.0],
        "limit_up": [12.0, 11.0, 13.0],
        "limit_down": [10.0, 9.0, 10.0],
    })
    enriched = compute_limit_signals(
        panel,
        instruments,
        needed={"signal_limit_up", "signal_limit_down"},
    ).join(
        instruments.select("symbol", "name", "total_shares", "float_shares"),
        on="symbol",
        how="left",
    )
    field_columns = {
        "amount",
        "raw_close",
        "raw_high",
        "raw_low",
        "turnover_rate",
        "total_shares",
        "float_shares",
    }
    expected = build_market_data_matrix(enriched, field_columns=field_columns)
    cache_root = tmp_path / "matrix_cache"
    actual = load_market_data_matrix_from_parquet(
        market_root,
        days[0],
        days[-1],
        field_columns=field_columns,
        instruments=instruments,
        cache_root=cache_root,
    )
    assert actual.cache_status == "built"

    assert actual.timestamp_labels == expected.timestamp_labels
    assert actual.symbols == expected.symbols
    assert actual.names == expected.names
    for name in (
        "timestamps",
        "session_ids",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "tradable",
        "limit_up_locked",
        "limit_down_locked",
    ):
        np.testing.assert_array_equal(getattr(actual, name), getattr(expected, name))
    for name in field_columns:
        np.testing.assert_array_equal(actual.field(name), expected.field(name))

    cached = load_market_data_matrix_from_parquet(
        market_root,
        days[0],
        days[-1],
        field_columns=field_columns,
        instruments=instruments,
        cache_root=cache_root,
    )
    assert cached.cache_status == "exact"
    assert isinstance(cached.close, np.memmap)
    assert not cached.close.flags.writeable
    np.testing.assert_array_equal(cached.close, actual.close)

    latest_path = market_root / f"date={days[-1].isoformat()}" / "part.parquet"
    latest = pl.read_parquet(latest_path).with_columns(
        pl.when(pl.col("symbol") == "000001.SZ")
        .then(12.5)
        .otherwise(pl.col("close"))
        .alias("close")
    )
    latest.write_parquet(latest_path)
    refreshed = load_market_data_matrix_from_parquet(
        market_root,
        days[0],
        days[-1],
        field_columns=field_columns,
        instruments=instruments,
        cache_root=cache_root,
    )
    target_asset = refreshed.symbols.index("000001.SZ")
    assert refreshed.close[-1, target_asset] == pytest.approx(12.5)
    assert len(list(cache_root.glob("v*-*"))) == 2


def test_covering_matrix_cache_reuses_wider_dates_and_fields(tmp_path):
    market_root = tmp_path / "kline_daily_enriched"
    days = [date(2024, 1, 2) + timedelta(days=offset) for offset in range(4)]
    rows = []
    for current in days:
        partition = market_root / f"date={current.isoformat()}"
        partition.mkdir(parents=True)
        for asset_id, symbol in enumerate(("000001.SZ", "600000.SH")):
            close = 10.0 + asset_id + (current - days[0]).days
            rows.append({
                "symbol": symbol,
                "date": current,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1_000.0,
                "amount": close * 100_000.0,
                "raw_close": close,
            })
        pl.DataFrame([row for row in rows if row["date"] == current]).write_parquet(
            partition / "part.parquet"
        )
    instruments = pl.DataFrame({
        "symbol": ["000001.SZ", "600000.SH"],
        "name": ["A", "B"],
    })
    cache_root = tmp_path / "matrix_cache"
    broad = load_market_data_matrix_from_parquet(
        market_root,
        days[0],
        days[-1],
        field_columns={"amount", "raw_close"},
        instruments=instruments,
        cache_root=cache_root,
    )
    narrow = load_market_data_matrix_from_parquet(
        market_root,
        days[1],
        days[2],
        field_columns={"raw_close"},
        instruments=instruments,
        cache_root=cache_root,
    )

    assert broad.cache_status == "built"
    assert narrow.cache_status == "covering"
    assert narrow.cache_path == broad.cache_path
    assert narrow.timestamp_labels == tuple(value.isoformat() for value in days[1:3])
    assert set(narrow.fields) == {"raw_close"}
    assert isinstance(narrow.close, np.memmap)
    assert not narrow.close.flags.writeable
    assert len(list(cache_root.glob("v*-*"))) == 1


def test_covering_cache_ignores_outside_slice_change_but_invalidates_inside(tmp_path):
    market_root = tmp_path / "kline_daily_enriched"
    days = [date(2024, 2, 1) + timedelta(days=offset) for offset in range(4)]
    for offset, current in enumerate(days):
        partition = market_root / f"date={current.isoformat()}"
        partition.mkdir(parents=True)
        pl.DataFrame({
            "symbol": ["000001.SZ"],
            "date": [current],
            "open": [10.0 + offset],
            "high": [10.0 + offset],
            "low": [10.0 + offset],
            "close": [10.0 + offset],
            "volume": [1_000.0],
        }).write_parquet(partition / "part.parquet")
    instruments = pl.DataFrame({"symbol": ["000001.SZ"], "name": ["A"]})
    cache_root = tmp_path / "matrix_cache"
    load_market_data_matrix_from_parquet(
        market_root,
        days[0],
        days[-1],
        field_columns=set(),
        instruments=instruments,
        cache_root=cache_root,
    )

    outside_path = market_root / f"date={days[-1].isoformat()}" / "part.parquet"
    pl.read_parquet(outside_path).with_columns(pl.lit(99.0).alias("close")).write_parquet(
        outside_path
    )
    outside = load_market_data_matrix_from_parquet(
        market_root,
        days[1],
        days[2],
        field_columns=set(),
        instruments=instruments,
        cache_root=cache_root,
    )
    assert outside.cache_status == "covering"

    inside_path = market_root / f"date={days[2].isoformat()}" / "part.parquet"
    pl.read_parquet(inside_path).with_columns(pl.lit(77.0).alias("close")).write_parquet(
        inside_path
    )
    inside = load_market_data_matrix_from_parquet(
        market_root,
        days[1],
        days[2],
        field_columns=set(),
        instruments=instruments,
        cache_root=cache_root,
    )
    assert inside.cache_status == "built"
    assert inside.close[-1, 0] == pytest.approx(77.0)


def test_matrix_cache_can_be_disabled(tmp_path):
    market_root = tmp_path / "kline_daily_enriched"
    current = date(2024, 3, 1)
    partition = market_root / f"date={current.isoformat()}"
    partition.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [current],
        "open": [10.0],
        "high": [10.0],
        "low": [10.0],
        "close": [10.0],
        "volume": [1_000.0],
    }).write_parquet(partition / "part.parquet")

    market = load_market_data_matrix_from_parquet(
        market_root,
        current,
        current,
        field_columns=set(),
        cache_root=None,
    )
    assert market.cache_status == "disabled"
    assert not isinstance(market.close, np.memmap)


def test_matrix_cache_prunes_by_bytes_and_leaves_no_staging_directory(tmp_path):
    market_root = tmp_path / "kline_daily_enriched"
    current = date(2024, 3, 4)
    partition = market_root / f"date={current.isoformat()}"
    partition.mkdir(parents=True)
    path = partition / "part.parquet"

    def write_close(value: float) -> None:
        pl.DataFrame({
            "symbol": ["000001.SZ"],
            "date": [current],
            "open": [value],
            "high": [value],
            "low": [value],
            "close": [value],
            "volume": [1_000.0],
        }).write_parquet(path)

    cache_root = tmp_path / "matrix_cache"
    write_close(10.0)
    first = load_market_data_matrix_from_parquet(
        market_root,
        current,
        current,
        field_columns=set(),
        cache_root=cache_root,
        cache_max_bytes=1,
    )
    write_close(11.0)
    second = load_market_data_matrix_from_parquet(
        market_root,
        current,
        current,
        field_columns=set(),
        cache_root=cache_root,
        cache_max_bytes=1,
    )

    assert first.cache_path != second.cache_path
    del first
    gc.collect()
    assert second.close[0, 0] == pytest.approx(11.0)
    assert len(list(cache_root.glob("v4-*"))) == 1
    assert list(cache_root.glob(".*.tmp")) == []
    assert len(list(cache_root.glob(".axes-v1-*.json"))) == 1


def test_managed_source_generation_skips_file_walk_and_invalidates_explicitly(tmp_path):
    market_root = tmp_path / "kline_daily_enriched"
    current = date(2024, 3, 5)
    partition = market_root / f"date={current.isoformat()}"
    partition.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [current],
        "open": [10.0],
        "high": [10.0],
        "low": [10.0],
        "close": [10.0],
        "volume": [1_000.0],
    }).write_parquet(partition / "part.parquet")
    cache_root = tmp_path / "matrix_cache"

    first = load_market_data_matrix_from_parquet(
        market_root,
        current,
        current,
        field_columns=set(),
        cache_root=cache_root,
        source_generation="generation-a",
    )
    repeated = load_market_data_matrix_from_parquet(
        market_root,
        current,
        current,
        field_columns=set(),
        cache_root=cache_root,
        source_generation="generation-a",
    )
    changed = load_market_data_matrix_from_parquet(
        market_root,
        current,
        current,
        field_columns=set(),
        cache_root=cache_root,
        source_generation="generation-b",
    )

    assert first.cache_status == "built"
    assert repeated.cache_status == "exact"
    assert repeated.cache_path == first.cache_path
    assert changed.cache_status == "built"
    assert changed.cache_path != first.cache_path
    del first, repeated
    gc.collect()
    assert len(list(cache_root.glob("v4-*"))) == 1


def test_registered_builtin_matrix_strategies_share_one_cache_profile():
    engine = StrategyEngine(
        strategy_dirs=[REPO_ROOT / "backend" / "app" / "strategy" / "builtin"]
    )
    profile = build_matrix_cache_profile(engine, "stock")
    strategies = engine.strategy_definitions()

    assert len(strategies) == 18
    assert all(strategy.execution_backend == "matrix_native" for strategy in strategies)
    assert profile.warmup_bars > 0
    assert profile.forward_bars == max(int(strategy.max_hold_days or 0) for strategy in strategies)
    assert {"open", "high", "low", "close", "volume"}.issubset(profile.field_columns)


def test_chunked_matrix_score_matches_previous_full_matrix_formula():
    row_count = 4
    asset_count = 600
    symbols = [f"{asset_id:06d}.SZ" for asset_id in range(asset_count)]
    rows = []
    rng = np.random.default_rng(20260715)
    for time_id in range(row_count):
        for asset_id, symbol in enumerate(symbols):
            rows.append({
                "symbol": symbol,
                "date": date(2024, 1, 1) + timedelta(days=time_id),
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": 1_000.0,
                "feature_a": float(rng.normal()),
                "feature_b": float((asset_id % 7) - 3),
            })
    market = build_market_data_matrix(
        pl.DataFrame(rows),
        field_columns={"feature_a", "feature_b"},
    )
    universe = rng.random(market.shape) > 0.2
    weights = {"feature_a": 0.75, "feature_b": 0.25}

    expected = np.zeros(market.shape, dtype=np.float32)
    all_finite = universe.copy()
    total_weight = sum(weights.values())
    for name, weight in weights.items():
        values = market.field(name)
        finite = universe & np.isfinite(values)
        all_finite &= np.isfinite(values)
        row_min = np.min(np.where(finite, values, np.inf), axis=1)
        row_max = np.max(np.where(finite, values, -np.inf), axis=1)
        row_range = row_max - row_min
        normalized = np.zeros(market.shape, dtype=np.float32)
        varying = finite & np.isfinite(row_range[:, None]) & (row_range[:, None] > 0)
        equal = finite & ~varying
        np.divide(
            values - row_min[:, None],
            row_range[:, None],
            out=normalized,
            where=varying,
        )
        normalized[equal] = np.float32(0.5)
        expected += normalized * np.float32(weight / total_weight)
    expected *= np.float32(100.0)
    expected[~universe | ~all_finite] = 0.0

    actual = build_matrix_score(
        market,
        universe,
        weights,
        "score",
        True,
        fallback=np.zeros(market.shape, dtype=np.float32),
    )
    np.testing.assert_array_equal(actual, expected)


def test_signal_slice_is_zero_copy_and_masking_only_allocates_final_flags():
    entry = np.ones((5, 2), dtype=np.uint8)
    codes = np.arange(10, dtype=np.int16).reshape(5, 2)
    score = np.arange(10, dtype=np.float32).reshape(5, 2)
    signals = make_signal_matrix(
        (5, 2),
        entry=entry,
        exit=entry,
        score=score,
        entry_signal_code=codes,
        exit_signal_code=codes,
    )
    sliced = slice_signal_matrix(signals, 1, 4)
    assert np.shares_memory(sliced.entry, signals.entry)
    assert np.shares_memory(sliced.score, signals.score)

    masked = apply_time_masks(
        sliced,
        np.array([True, False, True]),
        np.array([False, True, True]),
    )
    assert np.shares_memory(masked.score, signals.score)
    assert masked.entry.tolist() == [[1, 1], [0, 0], [1, 1]]
    assert masked.exit.tolist() == [[0, 0], [1, 1], [1, 1]]
    assert masked.entry_signal_code[1].tolist() == [-1, -1]
    assert masked.exit_signal_code[0].tolist() == [-1, -1]
    assert not masked.entry.flags.writeable
    assert signals.entry.tolist() == [[1, 1]] * 5


def test_matrix_pipeline_applies_basic_filter_and_candidate_scoring():
    panel = pl.DataFrame({
        "symbol": ["000001.SZ", "600000.SH"],
        "name": ["A", "B"],
        "date": [date(2024, 1, 1)] * 2,
        "open": [10.0, 20.0],
        "high": [10.0, 20.0],
        "low": [10.0, 20.0],
        "close": [10.0, 20.0],
        "volume": [100.0, 100.0],
        "amount": [50.0, 500.0],
    })
    market = build_market_data_matrix(panel, field_columns={"amount"})

    class AllEntries:
        def required_fields(self):
            return frozenset({"close"})

        def required_warmup_bars(self, params):
            return 1

        def compute_signals(self, market, params):
            return make_signal_matrix(market.shape, entry=np.ones(market.shape, dtype=np.uint8))

    signals = MatrixStrategyPipeline().run(
        AllEntries(),
        market,
        {},
        MatrixPipelineConfig(
            basic_filter={"enabled": True, "amount_min": 100.0},
            scoring={"close": 1.0},
            order_by="score",
            descending=True,
        ),
    )

    assert signals.entry.tolist() == [[0, 1]]
    assert signals.score.tolist() == [[0.0, 50.0]]


def test_signal_matrix_validation_rejects_mutable_strategy_output():
    signals = make_signal_matrix((2, 1))
    mutable_entry = signals.entry.copy()
    invalid = type(signals)(
        entry=mutable_entry,
        exit=signals.exit,
        score=signals.score,
        entry_signal_code=signals.entry_signal_code,
        exit_signal_code=signals.exit_signal_code,
    )
    with pytest.raises(ValueError, match="read-only"):
        validate_signal_matrix(invalid, (2, 1))


def test_matrix_pipeline_applies_asset_pool_before_cross_sectional_scoring():
    panel = pl.DataFrame({
        "symbol": ["000001.SZ", "600000.SH"],
        "name": ["A", "B"],
        "date": [date(2024, 1, 1)] * 2,
        "open": [10.0, 20.0],
        "high": [10.0, 20.0],
        "low": [10.0, 20.0],
        "close": [10.0, 20.0],
        "volume": [100.0, 100.0],
    })
    market = build_market_data_matrix(panel)

    class AllEntries:
        def required_fields(self):
            return frozenset({"close"})

        def required_warmup_bars(self, params):
            return 1

        def compute_signals(self, market, params):
            return make_signal_matrix(market.shape, entry=np.ones(market.shape, dtype=np.uint8))

    signals = MatrixStrategyPipeline().run(
        AllEntries(),
        market,
        {},
        MatrixPipelineConfig(
            basic_filter={"enabled": False},
            scoring={"close": 1.0},
            order_by="score",
            descending=True,
            asset_mask=np.array([False, True]),
        ),
    )

    assert signals.entry.tolist() == [[0, 1]]
    assert signals.score.tolist() == [[0.0, 50.0]]


def test_realtime_market_matrix_overwrites_last_row_and_appends_new_bar():
    panel = pl.DataFrame({
        "symbol": ["000001.SZ", "600000.SH"] * 2,
        "name": ["A", "B"] * 2,
        "date": [date(2024, 1, 1)] * 2 + [date(2024, 1, 2)] * 2,
        "open": [10.0, 20.0, 10.5, 20.5],
        "high": [10.2, 20.2, 10.7, 20.7],
        "low": [9.8, 19.8, 10.3, 20.3],
        "close": [10.1, 20.1, 10.6, 20.6],
        "volume": [1_000.0, 2_000.0, 1_100.0, 2_100.0],
        "amount": [10_100.0, 40_200.0, 11_660.0, 43_260.0],
    })
    buffer = RealtimeMarketDataMatrix(panel, field_columns={"amount"})

    same_bar = panel.filter(pl.col("date") == date(2024, 1, 2)).with_columns(
        (pl.col("close") + 5.0).alias("close")
    )
    buffer.update(same_bar)
    snapshot = buffer.snapshot()
    assert snapshot.shape == (2, 2)
    assert snapshot.close[-1].tolist() == pytest.approx(same_bar.sort("symbol")["close"].to_list())
    assert buffer.build_count == 1
    assert buffer.update_count == 1
    assert not snapshot.close.flags.writeable

    next_bar = same_bar.with_columns(pl.lit(date(2024, 1, 3)).alias("date"))
    buffer.update(next_bar)
    assert buffer.snapshot().shape == (3, 2)
    assert buffer.build_count == 1
    assert buffer.update_count == 2


def test_strategy_engine_runs_matrix_strategy_without_legacy_filter():
    start = date(2024, 1, 1)
    rows = []
    for offset in range(65):
        values = (
            ("000001.SZ", 10.0 + offset * 0.05),
            ("600000.SH", 20.0 + offset * 0.03),
        )
        for symbol, close in values:
            rows.append({
                "symbol": symbol,
                "name": symbol,
                "date": start + timedelta(days=offset),
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1_000_000.0,
                "amount": 100_000_000.0,
                "total_shares": 1_000_000_000.0,
                "float_shares": 800_000_000.0,
            })
    history = pl.DataFrame(rows)
    target = start + timedelta(days=64)
    engine = StrategyEngine(
        strategy_dirs=[REPO_ROOT / "backend" / "app" / "strategy" / "builtin"],
    )
    strategy = engine.get("macd_golden")
    assert strategy.filter_fn is None

    result = engine.run(
        "macd_golden",
        StrategyDataContext(
            asset_type="stock",
            timeframe="1d",
            as_of=target,
            current=history.filter(pl.col("date") == target),
            history=history,
        ),
        pool=["600000.SH"],
        params={"require_macd_golden": False, "use_volume_filter": False},
        overrides={"basic_filter": {"enabled": False}},
    )

    assert [row["symbol"] for row in result.rows] == ["600000.SH"]
    assert result.total == 1


def test_strategy_engine_run_all_builds_one_shared_matrix():
    start = date(2024, 1, 1)
    rows = []
    for offset in range(65):
        close = 10.0 + offset * 0.05
        rows.append({
            "symbol": "000001.SZ",
            "name": "A",
            "date": start + timedelta(days=offset),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000.0,
            "amount": 100_000_000.0,
            "total_shares": 1_000_000_000.0,
            "float_shares": 800_000_000.0,
        })
    history = pl.DataFrame(rows)
    target = start + timedelta(days=64)
    engine = StrategyEngine(
        strategy_dirs=[REPO_ROOT / "backend" / "app" / "strategy" / "builtin"],
    )
    original = engine.get("macd_golden")
    engine._strategies["macd_copy"] = replace(
        original,
        meta={**original.meta, "id": "macd_copy"},
    )
    params = {"require_macd_golden": False, "use_volume_filter": False}
    overrides = {"basic_filter": {"enabled": False}}

    with patch(
        "app.backtest.matrix.build_market_data_matrix",
        wraps=matrix_module.build_market_data_matrix,
    ) as build:
        results = engine.run_all(
            StrategyDataContext(
                asset_type="stock",
                timeframe="1d",
                as_of=target,
                current=history.filter(pl.col("date") == target),
                history=history,
            ),
            strategy_ids=["macd_golden", "macd_copy"],
            params_map={"macd_golden": params, "macd_copy": params},
            overrides_map={"macd_golden": overrides, "macd_copy": overrides},
        )

    assert build.call_count == 1
    assert results["macd_golden"].total == 1
    assert results["macd_copy"].total == 1
