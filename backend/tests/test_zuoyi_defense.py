from datetime import date, timedelta

import polars as pl

from app.services.zuoyi_defense import ARMS, evaluate_zuoyi_defense


class FakeReader:
    def __init__(self, rows, missing=()):
        self.rows = rows
        self.missing = set(missing)
    def has_columns(self, *columns):
        return not (set(columns) & self.missing)
    def manifest(self):
        return {"source_generations": {"markets": "m1"}}
    def generation(self): return "c1"
    def manifest_sha256(self): return "c" * 64
    def market_days(self, start, end): return [r["date"] for r in self.rows if start <= r["date"] <= end]
    def daily_bars(self, symbol, start, end):
        return pl.DataFrame([r for r in self.rows if r["date"] >= start and r["date"] <= end])


class FakeFacts:
    def __init__(self, rows=None): self.rows = rows or {}
    def get(self, symbol, day): return self.rows.get((symbol, day), {"published_limit_up": 999999, "suspended": False})
    def manifest_sha256(self): return "m" * 64


def rows(n=130):
    d = date(2024, 1, 1)
    return [{"symbol": "SZ.000001", "date": d + timedelta(days=i), "open": 10 + i * .01, "high": 10.2 + i * .01, "low": 9.8 + i * .01, "close": 10 + i * .01, "raw_open": 10 + i * .01, "raw_high": 10.2 + i * .01, "raw_low": 9.8 + i * .01, "raw_close": 10 + i * .01} for i in range(n)]


def test_unavailable_without_markets_pin():
    r = evaluate_zuoyi_defense(FakeReader(rows()), start=date(2024,1,1), end=date(2024,4,1), symbols=["SZ.000001"], oos_start=date(2024,2,1))
    assert r["status"] == "unavailable"
    assert r["code"] == "UNAVAILABLE_MARKETS_PIN"
    assert r["arms"] == [] and r["events"] == [] and r["segments"] == [] and r["censored"] == []


def test_insufficient_oos_is_top_level_unavailable():
    r = evaluate_zuoyi_defense(FakeReader(rows()), start=date(2024, 1, 1), end=date(2024, 4, 1), symbols=["SZ.000001"], oos_start=date(2024, 2, 1), market_facts_reader=FakeFacts())
    assert r["status"] == "unavailable"
    assert r["code"] == "UNAVAILABLE_INVALID_PROVENANCE"


def test_sufficient_paired_oos_has_six_arms():
    d = date(2020, 1, 1)
    dense = []
    for i in range(3000):
        close = 80.0 if i % 70 == 0 else 100.0 + i * 0.01
        dense.append({"symbol": "SZ.000001", "date": d + timedelta(days=i), "open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "raw_open": close, "raw_high": close + 0.2, "raw_low": close - 0.2, "raw_close": close})
    result = evaluate_zuoyi_defense(FakeReader(dense), start=d + timedelta(days=100), end=d + timedelta(days=2700), symbols=["SZ.000001"], oos_start=d + timedelta(days=1100), market_facts_reader=FakeFacts())
    assert result["status"] == "ok"
    assert [arm["arm"] for arm in result["arms"]] == list(ARMS)
    ids = [segment["entry_id"] for segment in result["arms"][0]["segments"]]
    assert all([segment["entry_id"] for segment in arm["segments"]] == ids for arm in result["arms"])


def test_required_column_fail_closed():
    r = evaluate_zuoyi_defense(FakeReader(rows(), missing=("raw_open",)), start=date(2024,1,1), end=date(2024,4,1), symbols=["SZ.000001"], oos_start=date(2024,2,1), market_facts_reader=FakeFacts())
    assert r["status"] == "unavailable"
    assert r["code"] == "UNAVAILABLE_REQUIRED_COLUMN"

def test_missing_pinned_markets_is_whole_order_unavailable():
    class SparseFacts(FakeFacts):
        def get(self, symbol, day):
            return None if day.day == 3 else super().get(symbol, day)

    result = evaluate_zuoyi_defense(
        FakeReader(rows()),
        start=date(2024, 1, 1),
        end=date(2024, 4, 1),
        symbols=["SZ.000001"],
        oos_start=date(2024, 2, 1),
        market_facts_reader=SparseFacts(),
    )
    assert result["status"] == "unavailable"
    assert result["code"] == "UNAVAILABLE_MARKETS_PIN"
