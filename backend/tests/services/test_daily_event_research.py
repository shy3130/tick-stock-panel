from datetime import date, timedelta

import polars as pl

import app.services.daily_event_research.evaluation as evaluation
from app.data_providers.fquant.daily_market_research import MarketFact
from app.services.daily_event_research import (
    CensorReason,
    DailyEventRequest,
    DailyEventStatus,
    DailyEventVerdict,
    Detection,
    DetectionEvidence,
    UnavailabilityReason,
    evaluate_daily_events,
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


def request(**overrides):
    values = {
        "symbols": [SYMBOL],
        "start": date(2024, 7, 1),
        "oos_start": date(2024, 8, 1),
        "end": date(2024, 9, 1),
        "variant": "ma_24_72",
    }
    values.update(overrides)
    return DailyEventRequest(**values)


class Reader:
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
            "generation": self.generation(),
            "columns": COLUMNS,
            "source_generations": {"markets": "markets-v1"},
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
                pre_close=max(row["raw_close"] - 0.1, 0.01),
                published_limit_up=row["raw_close"] + 10.0,
                published_limit_down=max(row["raw_close"] - 10.0, 0.01),
                regime="main_board_10pct",
                is_st=False,
                name="测试",
            )
        return result


def rows(count=330):
    start = date(2024, 1, 1)
    result = []
    for index in range(count):
        close = 10.0 + index * 0.05
        result.append(
            {
                "symbol": SYMBOL,
                "date": start + timedelta(days=index),
                "open": close,
                "high": close + 0.02,
                "low": close - 0.02,
                "close": close,
                "raw_open": close,
                "raw_high": close + 0.02,
                "raw_low": close - 0.02,
                "raw_close": close,
                "volume": 100.0,
                "amount": 1000.0,
            }
        )
    return result


class StubDetector:
    def __init__(self, _config):
        pass

    def detect(self, symbol, _bars, calendar):
        signal = next(day for day in calendar if day >= date(2024, 7, 5))
        return (
            Detection(
                detector_id="dugu_trend",
                variant="ma_24_72",
                symbol=symbol,
                signal_date=signal,
                evidence=DetectionEvidence(
                    qualified=True,
                    values={"t1": True, "t2": True, "t3": True},
                ),
            ),
        )


def test_empty_reader_is_explicitly_unavailable():
    response = evaluate_daily_events(request(), Reader([]), Facts([]))
    assert response.status is DailyEventStatus.UNAVAILABLE
    assert response.unavailable_reason is UnavailabilityReason.CANONICAL_READER
    assert response.promoted is False


def test_missing_market_facts_is_explicitly_unavailable():
    response = evaluate_daily_events(request(), Reader(rows()))
    assert response.unavailable_reason is UnavailabilityReason.MARKET_FACTS


def test_request_boundary_and_json_shape():
    value = request()
    assert value.model_validate_json(value.model_dump_json()) == value


def test_t_plus_one_execution_cost_and_oos_boundary_are_frozen(monkeypatch):
    monkeypatch.setattr(evaluation, "DuguTrendDetector", StubDetector)
    source_rows = rows()
    response = evaluate_daily_events(
        request(
            start=date(2024, 7, 1),
            oos_start=date(2024, 7, 5),
            end=date(2024, 9, 1),
            horizon_days=5,
            cost_bps=100,
        ),
        Reader(source_rows),
        Facts(source_rows),
    )
    assert response.status is DailyEventStatus.OK
    event = response.events[0]
    assert event.entry_date == event.signal_date + timedelta(days=1)
    assert event.exit_date == event.entry_date + timedelta(days=4)
    expected = (event.exit_price / event.entry_price) * 0.99**2 - 1.0
    assert event.cost_adjusted_forward_return == expected
    assert event.oos is True
    assert response.verdicts is not None
    assert response.verdicts.verdict is DailyEventVerdict.UNAVAILABLE


def test_one_price_limit_up_entry_is_censored(monkeypatch):
    monkeypatch.setattr(evaluation, "DuguTrendDetector", StubDetector)
    source_rows = rows()
    signal_index = next(
        index for index, row in enumerate(source_rows) if row["date"] == date(2024, 7, 5)
    )
    entry = source_rows[signal_index + 1]
    entry.update(
        {
            "raw_open": 20.0,
            "raw_high": 20.0,
            "raw_low": 20.0,
            "raw_close": 20.0,
        }
    )

    class LimitFacts(Facts):
        def limit_band_facts(self, symbol, start, end):
            facts = super().limit_band_facts(symbol, start, end)
            original = facts[entry["date"]]
            facts[entry["date"]] = MarketFact(
                **{
                    **original.__dict__,
                    "published_limit_up": 20.0,
                }
            )
            return facts

    response = evaluate_daily_events(
        request(),
        Reader(source_rows),
        LimitFacts(source_rows),
    )
    assert response.status is DailyEventStatus.OK
    assert response.events == []
    assert response.censored[0].reason is CensorReason.ENTRY_LIMIT_UP_BLOCKED
