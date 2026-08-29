from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
from fastapi.encoders import jsonable_encoder

from app.data_providers.fquant.daily_market_research import MarketFact
from app.services.negative_exclusion_production import (
    evaluate_negative_exclusion_production,
)

SYMBOL = "000001.SZ"
COLUMNS = (
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
)


def _rows():
    start = date(2023, 1, 1)
    result = []
    for index in range(500):
        close = 100.0 - index * 0.08
        result.append(
            {
                "symbol": SYMBOL,
                "date": start + timedelta(days=index),
                "open": close + 0.05,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "raw_open": close + 0.05,
                "raw_high": close + 0.2,
                "raw_low": close - 0.2,
                "raw_close": close,
                "volume": 1_000.0,
                "amount": 1_000.0 * close,
            }
        )
    return result


class _Canonical:
    def __init__(self, rows):
        self.rows = rows
        self.days = tuple(row["date"] for row in rows)

    def has_columns(self, *columns):
        return all(column in COLUMNS for column in columns)

    def generation(self):
        return "canonical-g1"

    def manifest_sha256(self):
        return "a" * 64

    def manifest(self):
        return {"source_generations": {"markets": "markets-g1"}, "columns": COLUMNS}

    def market_days(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def daily_bars(self, symbol, start, end):
        return pl.DataFrame([row for row in self.rows if start <= row["date"] <= end])


class _Facts:
    def __init__(self, rows):
        self.rows = rows

    def generation(self):
        return "markets-g1"

    def manifest_sha256(self):
        return "b" * 64

    def limit_band_facts(self, symbol, start, end):
        return {
            row["date"]: MarketFact(
                raw_open=row["raw_open"],
                raw_high=row["raw_high"],
                raw_low=row["raw_low"],
                raw_close=row["raw_close"],
                pre_close=row["raw_close"] + 0.08,
                published_limit_up=row["raw_close"] + 10,
                published_limit_down=row["raw_close"] - 10,
                regime="main_10",
                is_st=False,
                name="测试",
            )
            for row in self.rows
            if start <= row["date"] <= end
        }


class _Universe:
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


class _EmptyUniverse(_Universe):
    def prefetch_presence_days(self, days):
        return {
            day: SimpleNamespace(
                source_day_observed=True,
                symbols=(),
                content_hash="e" * 64,
            )
            for day in days
        }


def test_production_evaluator_reports_portfolio_metrics_and_capability_gaps():
    rows = _rows()
    response = evaluate_negative_exclusion_production(
        symbols=[SYMBOL],
        start=date(2023, 1, 1),
        oos_start=date(2023, 3, 1),
        end=date(2024, 5, 1),
        canonical_reader=_Canonical(rows),
        market_facts_reader=_Facts(rows),
        universe_reader=_Universe(),
    )
    assert response["status"] == "ok", response
    assert response["coverage"]["observations"] > 30
    assert response["capabilities"]["v1"] == "unavailable_definition_unverified"
    assert response["capabilities"]["v3"] == "unavailable_no_pit_announcement_source"
    v4 = response["evaluation"]["classes"]["v4"]
    assert v4["active_days"] > 0
    assert "sharpe_delta" in v4["portfolio"]
    assert "annualized_return_delta" in v4["portfolio"]
    assert jsonable_encoder(response)["request"]["oos_start"] == "2023-03-01"


def test_all_censored_observations_return_auditable_unavailable_envelope():
    rows = _rows()
    response = evaluate_negative_exclusion_production(
        symbols=[SYMBOL],
        start=date(2023, 1, 1),
        oos_start=date(2023, 3, 1),
        end=date(2024, 5, 1),
        canonical_reader=_Canonical(rows),
        market_facts_reader=_Facts(rows),
        universe_reader=_EmptyUniverse(),
    )

    assert response["status"] == "unavailable"
    assert response["reason"] == "unavailable_no_evaluable_observations"
    assert response["coverage"]["observations"] == 0
    assert response["coverage"]["censored"]["universe_membership_unproven"] > 0
    assert response["provenance"]["canonical"]["generation"] == "canonical-g1"


def test_invalid_date_order_is_rejected():
    rows = _rows()
    try:
        evaluate_negative_exclusion_production(
            symbols=[SYMBOL],
            start=date(2024, 1, 1),
            oos_start=date(2023, 10, 1),
            end=date(2024, 1, 31),
            canonical_reader=_Canonical(rows),
            market_facts_reader=_Facts(rows),
            universe_reader=_Universe(),
        )
    except ValueError as exc:
        assert "start <= oos_start <= end" in str(exc)
    else:
        raise AssertionError("invalid date order must fail")
