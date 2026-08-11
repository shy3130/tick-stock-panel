from __future__ import annotations

import json
from argparse import Namespace
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from scripts.run_mvp import (
    build_config,
    build_payload,
    collect_data_status,
    execute,
    select_universe,
)


def _write_enriched(data_dir: Path, *, duplicate: bool = False) -> None:
    rows = [
        {"date": date(2024, 9, 24), "symbol": "000001.SZ", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1000.0},
        {"date": date(2024, 9, 24), "symbol": "000002.SZ", "open": 20.0, "high": 20.5, "low": 19.8, "close": 20.2, "volume": 2000.0},
        {"date": date(2024, 9, 25), "symbol": "000001.SZ", "open": 10.2, "high": 10.7, "low": 10.0, "close": 10.4, "volume": 1100.0},
        {"date": date(2024, 9, 25), "symbol": "000003.SZ", "open": 30.0, "high": 30.5, "low": 29.8, "close": 30.2, "volume": 3000.0},
    ]
    if duplicate:
        rows.append(dict(rows[0]))
    frame = pl.DataFrame(rows)
    for day in sorted(set(frame["date"].to_list())):
        partition = data_dir / "kline_daily_enriched" / f"date={day}"
        partition.mkdir(parents=True, exist_ok=True)
        frame.filter(pl.col("date") == day).write_parquet(partition / "part.parquet")


def _write_stock_basic(data_dir: Path) -> None:
    target = data_dir / "tushare_stock_basic"
    target.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ"],
            "name": ["平安银行", "*ST 测试", "普通股票"],
            "list_status": ["L", "L", "L"],
        }
    ).write_parquet(target / "all.parquet")


def _args(tmp_path: Path, data_dir: Path) -> Namespace:
    return Namespace(
        strategy="trend_breakout",
        start="2024-09-24",
        end="latest",
        seed=17,
        universe_size=2,
        data_dir=str(data_dir),
        output_json=str(tmp_path / "out" / "mvp.json"),
        output_html=str(tmp_path / "out" / "mvp.html"),
        validate_only=False,
    )


def test_data_status_and_universe_are_deterministic(tmp_path: Path) -> None:
    _write_enriched(tmp_path)
    status = collect_data_status(tmp_path)

    assert status == {
        "source": "data/kline_daily_enriched/**/*.parquet",
        "file_count": 2,
        "row_count": 4,
        "symbol_count": 3,
        "trading_day_count": 2,
        "min_date": "2024-09-24",
        "max_date": "2024-09-25",
        "duplicate_date_symbol_rows": 0,
        "required_null_values": 0,
        "non_finite_ohlcv_values": 0,
        "non_positive_prices": 0,
        "valid": True,
    }
    first = select_universe(
        tmp_path,
        start=date(2024, 9, 24),
        end=date(2024, 9, 25),
        size=2,
        seed=17,
    )
    second = select_universe(
        tmp_path,
        start=date(2024, 9, 24),
        end=date(2024, 9, 25),
        size=2,
        seed=17,
    )
    assert first == second


def test_full_universe_uses_end_date_and_excludes_st(tmp_path: Path) -> None:
    _write_enriched(tmp_path)
    _write_stock_basic(tmp_path)

    selected = select_universe(
        tmp_path,
        start=date(2024, 9, 24),
        end=date(2024, 9, 25),
        size=None,
        seed=17,
    )

    assert selected == ["000001.SZ", "000003.SZ"]


def test_duplicate_rows_fail_quality_gate(tmp_path: Path) -> None:
    _write_enriched(tmp_path, duplicate=True)
    status = collect_data_status(tmp_path)
    assert status["duplicate_date_symbol_rows"] == 1
    assert status["valid"] is False


def test_payload_ignores_nondeterministic_worker_fields() -> None:
    config = build_config(
        "trend_breakout",
        ["000001.SZ"],
        date(2024, 9, 24),
        date(2024, 9, 25),
    )
    data_status = {
        "min_date": "2024-09-24",
        "max_date": "2024-09-25",
        "valid": True,
    }
    first = build_payload(
        config=config,
        data_status=data_status,
        seed=7,
        requested_universe_size=1,
        raw_result={
            "run_id": "one",
            "elapsed_ms": 1,
            "stats": {"total_return": 0.1, "worker": {"pid": 1}},
            "benchmark_curve": [{"close": 100.0}, {"close": 105.0}],
        },
        validate_only=False,
    )
    second = build_payload(
        config=config,
        data_status=data_status,
        seed=7,
        requested_universe_size=1,
        raw_result={
            "run_id": "two",
            "elapsed_ms": 999,
            "stats": {"total_return": 0.1, "worker": {"pid": 2}},
            "benchmark_curve": [{"close": 100.0}, {"close": 105.0}],
        },
        validate_only=False,
    )
    assert first == second
    assert first["result"]["metrics"]["benchmark_return"] == 0.05
    assert first["result"]["metrics"]["excess"] == 0.05


def test_execute_writes_json_html_and_explicit_failure(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_enriched(data_dir)
    args = _args(tmp_path, data_dir)

    def runner(config, actual_data_dir):
        assert config.strategy_id == "trend_breakout"
        assert actual_data_dir == data_dir.resolve()
        return {
            "error": "未产生买入信号",
            "stats": {},
            "run_id": "ignored",
        }

    payload = execute(args, runner=runner)

    assert payload["result"]["status"] == "no_signal"
    assert payload["failures"] == [{"stage": "backtest", "reason": "未产生买入信号"}]
    assert json.loads(Path(args.output_json).read_text(encoding="utf-8")) == payload
    assert "TickFlow 最小回测报告" in Path(args.output_html).read_text(encoding="utf-8")


def test_build_config_rejects_non_mvp_strategy() -> None:
    with pytest.raises(ValueError, match="只开放策略"):
        build_config("pullback_to_support", ["000001.SZ"], date(2024, 1, 1), date(2024, 2, 1))
