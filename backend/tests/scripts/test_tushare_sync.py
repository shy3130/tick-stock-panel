from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import polars as pl

from scripts.tushare_sync import (
    _normalise_daily,
    _normalise_index_daily,
    _partition_path,
    sync_tushare,
)


def _payload(fields: list[str], rows: list[list[object]]) -> dict:
    return {"fields": fields, "items": rows}


def _daily_frame(trading_day: date, symbol: str = "000001.SZ") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol],
            "date": [trading_day],
            "open": [10.0],
            "high": [10.5],
            "low": [9.8],
            "close": [10.2],
            "volume": [1_000.0],
            "amount": [10_200.0],
        }
    )


def _write_daily(data_dir: Path, trading_day: date) -> Path:
    target = _partition_path(data_dir, "kline_daily", trading_day)
    target.parent.mkdir(parents=True, exist_ok=True)
    _daily_frame(trading_day).write_parquet(target)
    return target


class _FakeClient:
    def __init__(self, responses: dict[tuple[str, str], dict]):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def post(self, api_name, params, *, fields):
        self.calls.append((api_name, dict(params)))
        key_value = (
            params.get("trade_date")
            or params.get("list_status")
            or params.get("ts_code")
            or ""
        )
        return self.responses.get((api_name, str(key_value)), {"fields": [], "items": []})


def test_normalise_daily_converts_tushare_units_and_deduplicates():
    payload = _payload(
        ["ts_code", "open", "high", "low", "close", "vol", "amount"],
        [
            ["000001.SZ", 10.0, 10.5, 9.8, 10.2, 12.0, 123.0],
            ["000001.SZ", 10.1, 10.6, 9.9, 10.3, 13.0, 124.0],
        ],
    )

    result = _normalise_daily(payload, date(2024, 9, 24))

    assert result.height == 1
    assert result["volume"].item() == 1_300.0
    assert result["amount"].item() == 124_000.0
    assert result["date"].item() == date(2024, 9, 24)


def test_normalise_index_daily_keeps_only_requested_symbol():
    payload = _payload(
        ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"],
        [
            ["000300.SH", "20240924", 10.0, 10.5, 9.8, 10.2, 12.0, 123.0],
            ["000905.SH", "20240924", 20.0, 20.5, 19.8, 20.2, 22.0, 223.0],
        ],
    )

    result = _normalise_index_daily(payload, "000300.SH")

    assert result.height == 1
    assert result["symbol"].item() == "000300.SH"


def test_sync_skips_existing_valid_daily_without_rewriting(tmp_path):
    trading_day = date(2024, 9, 24)
    target = _write_daily(tmp_path, trading_day)
    before_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    adj_path = tmp_path / "adj_factor" / "all.parquet"
    adj_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["000001.SZ"],
            "trade_date": [trading_day],
            "ex_factor": [1.0],
        }
    ).write_parquet(adj_path)
    basic_path = (
        tmp_path
        / "tushare_daily_basic"
        / f"date={trading_day.isoformat()}"
        / "part.parquet"
    )
    basic_path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["000001.SZ"], "trade_date": [trading_day]}).write_parquet(
        basic_path
    )
    client = _FakeClient(
        {
            ("trade_cal", ""): _payload(["cal_date"], [["20240924"]]),
        }
    )

    summary = sync_tushare(
        client=client,
        data_dir=tmp_path,
        start=trading_day,
        end=trading_day,
        index_start=trading_day,
        index_symbols=(),
        run_enrichment=False,
    )

    assert summary["daily_days_written"] == 0
    assert summary["existing_daily_partitions_skipped"] == 1
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before_hash
    assert not any(name == "daily" for name, _ in client.calls)


def test_sync_adds_missing_day_and_merges_adj_factor_with_overlap(tmp_path):
    previous_day = date(2024, 9, 23)
    trading_day = date(2024, 9, 24)
    adj_path = tmp_path / "adj_factor" / "all.parquet"
    adj_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["000001.SZ"],
            "trade_date": [previous_day],
            "ex_factor": [1.0],
        }
    ).write_parquet(adj_path)
    client = _FakeClient(
        {
            ("trade_cal", ""): _payload(
                ["cal_date"],
                [["20240923"], ["20240924"]],
            ),
            ("daily", "20240924"): _payload(
                ["ts_code", "open", "high", "low", "close", "vol", "amount"],
                [["000001.SZ", 10.0, 10.5, 9.8, 10.2, 12.0, 123.0]],
            ),
            ("adj_factor", "20240923"): _payload(
                ["ts_code", "adj_factor"],
                [["000001.SZ", 2.0]],
            ),
            ("adj_factor", "20240924"): _payload(
                ["ts_code", "adj_factor"],
                [["000001.SZ", 2.2]],
            ),
        }
    )

    summary = sync_tushare(
        client=client,
        data_dir=tmp_path,
        start=trading_day,
        end=trading_day,
        index_start=trading_day,
        index_symbols=(),
        include_daily_basic=False,
        run_enrichment=False,
    )

    assert summary["daily_days_written"] == 1
    assert _partition_path(tmp_path, "kline_daily", trading_day).exists()
    factors = pl.read_parquet(adj_path).sort(["symbol", "trade_date"])
    current = factors.filter(pl.col("trade_date") == trading_day)
    assert current.height == 1
    assert abs(current["ex_factor"].item() - 1.1) < 1e-12
    assert factors.filter(pl.col("trade_date") == previous_day).height == 1


def test_sync_records_empty_open_day_instead_of_aborting(tmp_path):
    trading_day = date(2026, 7, 28)
    client = _FakeClient(
        {
            ("trade_cal", ""): _payload(["cal_date"], [["20260728"]]),
            ("daily", "20260728"): _payload(
                ["ts_code", "open", "high", "low", "close", "vol", "amount"],
                [],
            ),
            ("adj_factor", "20260728"): _payload(
                ["ts_code", "adj_factor"],
                [],
            ),
        }
    )

    summary = sync_tushare(
        client=client,
        data_dir=tmp_path,
        start=trading_day,
        end=trading_day,
        index_start=trading_day,
        index_symbols=(),
        include_daily_basic=False,
        run_enrichment=False,
    )

    assert summary["status"] == "complete_with_failures"
    assert summary["daily_failures"][0]["date"] == "2026-07-28"
    assert summary["adj_factor_failures"][0]["date"] == "2026-07-28"
    assert not _partition_path(tmp_path, "kline_daily", trading_day).exists()
