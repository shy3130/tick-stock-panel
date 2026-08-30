from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl

from app.data_providers.fquant.daily_market_research import MarketFact
from app.services.daily_event_research.production import (
    evaluate_escape_risk_production,
    evaluate_pre_surge_production,
)

COLUMNS = [
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "volume",
    "amount",
]
SYMBOL = "000001.SZ"
BENCHMARK = "000300.SH"


def make_rows():
    rows = []
    start = date(2023, 1, 1)
    for symbol, slope in ((SYMBOL, 0.02), (BENCHMARK, 0.01)):
        for index in range(500):
            close = 10.0 + slope * index
            rows.append(
                {
                    "symbol": symbol,
                    "date": start + timedelta(days=index),
                    "open": close,
                    "high": close + 0.1,
                    "low": close - 0.1,
                    "close": close,
                    "raw_open": close,
                    "raw_high": close + 0.1,
                    "raw_low": close - 0.1,
                    "raw_close": close,
                    "volume": 100.0 + index,
                    "amount": (100.0 + index) * close,
                }
            )
    return rows


class Canonical:
    def __init__(self, rows):
        self.rows = rows
        self.days = sorted({row["date"] for row in rows})

    def has_columns(self, *columns):
        return all(column in COLUMNS for column in columns)

    def generation(self):
        return "20240801T000000Z-test"

    def manifest_sha256(self):
        return "a" * 64

    def manifest(self):
        return {
            "source_generations": {"markets": "markets-v1"},
            "columns": COLUMNS,
        }

    def market_days(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def daily_bars(self, symbol, start, end):
        return pl.DataFrame(
            [row for row in self.rows if row["symbol"] == symbol and start <= row["date"] <= end]
        )


class Facts:
    def __init__(self, rows):
        self.rows = rows

    def generation(self):
        return "markets-v1"

    def manifest_sha256(self):
        return "b" * 64

    def limit_band_facts(self, symbol, start, end):
        result = {}
        for row in self.rows:
            if row["symbol"] != symbol or not start <= row["date"] <= end:
                continue
            result[row["date"]] = MarketFact(
                raw_open=row["raw_open"],
                raw_high=row["raw_high"],
                raw_low=row["raw_low"],
                raw_close=row["raw_close"],
                pre_close=max(row["raw_close"] - 0.02, 0.01),
                published_limit_up=row["raw_close"] + 10.0,
                published_limit_down=max(row["raw_close"] - 10.0, 0.01),
                regime="main_10",
                is_st=False,
                name="测试",
            )
        return result


class Universe:
    def prefetch_presence_days(self, days):
        return {
            day: SimpleNamespace(
                source_day_observed=True,
                symbols=(SYMBOL,),
                content_hash="c" * 64,
            )
            for day in days
        }

    def source_manifest(self):
        return {
            "schema_version": 2,
            "artifact": "universe_presence",
            "rule_version": "presence_v1",
            "retrospective": True,
            "status_filter": "daily_market_row_present_exact_day",
            "generation": "20240801T000000Z-1234567890abcdef",
            "source": {
                "artifact": "fstore_snapshot",
                "generation": "20240801T000000",
                "manifest_sha256": "d" * 64,
            },
        }


def test_pre_surge_production_binds_canonical_markets_and_universe():
    rows = make_rows()
    response = evaluate_pre_surge_production(
        symbols=[SYMBOL],
        start=date(2023, 6, 1),
        oos_start=date(2023, 10, 1),
        end=date(2024, 1, 31),
        canonical_reader=Canonical(rows),
        market_facts_reader=Facts(rows),
        universe_reader=Universe(),
        benchmark_symbol=BENCHMARK,
    )
    assert response["status"] == "ok", response
    assert set(response["identity"]) == {"canonical", "market_facts", "universe"}
    assert response["coverage"]["pit_universe_ineligible"] == 0
    assert response["promoted"] is False


def test_escape_production_censors_intraday_when_reader_is_missing():
    rows = make_rows()
    response = evaluate_escape_risk_production(
        symbols=[SYMBOL],
        start=date(2023, 6, 1),
        end=date(2024, 1, 31),
        canonical_reader=Canonical(rows),
    )
    assert response["status"] == "ok"
    assert response["capabilities"]["daily"] == {
        "s1": "available",
        "s8": "available",
        "s9": "available",
    }
    assert set(response["capabilities"]["intraday"]["signals"].values()) == {"available"}
    assert response["capabilities"]["intraday"]["runtime_status"] == "unavailable_reader"
    minute_reports = {
        item["signal_id"]: item for item in response["report"]["signals"]
    }
    assert all(
        "censor_intraday_data_missing" in minute_reports[signal]["censor_codes"]
        for signal in ("s2", "s3", "s4", "s5", "s6", "s7", "s10")
    )
