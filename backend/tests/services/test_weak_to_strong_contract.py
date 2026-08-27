from datetime import date, datetime, time

from app.services import weak_to_strong
from app.services.weak_to_strong import WeakToStrongEvaluateRequest, evaluate_weak_to_strong_v1


class _MinimumReader:
    def __init__(self, *, available=True, exact=12.05):
        self.available = available
        self.exact = exact

    def capabilities(self):
        return frozenset(weak_to_strong.MINIMUM_CAPABILITIES)

    def run_manifest(self):
        return {
            "generation": "composite-test",
            "sha256": "a" * 64,
            "components": {
                "markets": {
                    "generation": "markets-test",
                    "manifest_sha256": "b" * 64,
                    "first_available_at": "2026-01-09T09:25:00+08:00",
                }
            },
        }

    def daily_bars(self, symbol, start, end):
        return [
            {"trade_date": date(2026, 1, 7), "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 10.0},
            {"trade_date": date(2026, 1, 8), "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 30.0},
            {"trade_date": date(2026, 1, 9), "open": 12.05, "high": 12.05, "low": 12.05, "close": 12.05, "volume": 25.0},
            {"trade_date": date(2026, 1, 12), "open": 12.5, "high": 12.5, "low": 12.5, "close": 12.5, "volume": 20.0},
        ]

    def suspended_dates(self, symbol, start, end):
        return []

    def minute_bars(self, symbol, trade_date):
        return [{"timestamp": datetime(2026, 1, 9, 9, 31), "open": 12.05, "high": 12.05, "low": 12.05, "close": 12.05, "volume": 1.0}]

    def auction_snapshot(self, symbol, trade_date):
        return None

    def ticks(self, symbol, trade_date):
        return []

    def order_book_snapshots(self, symbol, trade_date):
        return []

    def pit_snapshot(self, symbol, as_of):
        if not self.available or as_of not in {date(2026, 1, 8), date(2026, 1, 9)}:
            return None
        exact = 11.0 if as_of == date(2026, 1, 8) else self.exact
        return {
            "effective_at": datetime.combine(as_of, time(9, 25)),
            "available_at": datetime.combine(as_of, time(9, 24)),
            "limit_up_pct": 0.1,
            "limit_down_pct": 0.1,
            "is_st": False,
            "float_shares": None,
            "limit_up_price": exact,
        }


def test_minimum_reader_reports_full_delta_and_exact_ztj():
    result = evaluate_weak_to_strong_v1(
        WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 9), symbols=["600000.SH"]),
        reader=_MinimumReader(),
    )
    assert result.manifest.status == "available"
    assert result.manifest.missing_capabilities == [
        "auction_evidence_reader",
        "sortable_tick_reader",
        "historical_order_book_reader",
        "pit_float_shares_records",
    ]
    assert result.evaluations[0].evidence["limit_price_source"] == "pinned_ztj"
    assert result.evaluations[0].evidence["limit_up_price"] == 12.05
    assert "missing_sortable_tick" in result.evaluations[0].censoring


def test_0925_effective_and_available_gates_are_both_required():
    reader = _MinimumReader()
    reader.pit_snapshot = lambda symbol, as_of: {
        "effective_at": datetime.combine(as_of, time(9, 26)),
        "available_at": datetime.combine(as_of, time(9, 24)),
        "limit_up_pct": 0.1,
        "limit_down_pct": 0.1,
        "is_st": False,
        "float_shares": None,
        "limit_up_price": 12.05,
    } if as_of in {date(2026, 1, 8), date(2026, 1, 9)} else None
    result = evaluate_weak_to_strong_v1(
        WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 9), symbols=["600000.SH"]),
        reader=reader,
    )
    assert result.evaluations[0].status_reason == "pit_incomplete"
    assert "pit_effective_after_cutoff" in result.evaluations[0].censoring
