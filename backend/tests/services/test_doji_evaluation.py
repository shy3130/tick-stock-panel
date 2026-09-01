from datetime import date

import polars as pl

from app.services.doji_patterns.evaluation import evaluate_doji_patterns
from app.services.doji_patterns.models import DojiPatternsRequest, DojiStatus
from app.services.hold_firm_patterns.models import REQUIRED_CANONICAL_COLUMNS


class BrokenCanonical:
    def has_columns(self, *_):
        return False

    def market_days(self, *_):
        return ()

    def generation(self):
        return "g"

    def manifest_sha256(self):
        return "a" * 64

    def manifest(self):
        return {}


class EmptyCanonical:
    def has_columns(self, *columns):
        return all(column in REQUIRED_CANONICAL_COLUMNS for column in columns)

    def market_days(self, start, end):
        return [date(2025, 1, 2)]

    def generation(self):
        return "canonical-g"

    def manifest_sha256(self):
        return "a" * 64

    def manifest(self):
        return {
            "source_generations": {"canonical": "canonical-g"},
            "columns": list(REQUIRED_CANONICAL_COLUMNS),
        }

    def daily_bars(self, symbol, start, end):
        return pl.DataFrame(schema={column: pl.Float64 for column in REQUIRED_CANONICAL_COLUMNS})


class EmptyFacts:
    def generation(self):
        return "markets-g"

    def manifest_sha256(self):
        return "b" * 64

    def limit_band_facts(self, symbol, start, end):
        return {}


class EmptyPresence:
    def prefetch_presence_days(self, days):
        return {}

    def source_manifest(self):
        return {
            "schema_version": 2,
            "artifact": "universe_presence",
            "generation": "20250101T000000Z-0123456789abcdef",
            "rule_version": "presence_v1",
            "retrospective": True,
            "status_filter": "daily_market_row_present_exact_day",
            "source": {
                "artifact": "fstore_snapshot",
                "generation": "20250101T000000",
                "manifest_sha256": "c" * 64,
            },
        }


def test_request_is_strict_and_defaults_are_frozen():
    r = DojiPatternsRequest(symbols=["600000.SH"], start=date(2025, 1, 1), end=date(2025, 12, 31))
    assert r.oos_start == date(2025, 7, 1) and r.cost_bps == 10 and r.theta_body_ratio == 0.1
    try:
        DojiPatternsRequest(
            symbols=["600000.SH"], start=date(2025, 1, 1), end=date(2025, 12, 31), extra=True
        )
    except Exception:
        pass
    else:
        raise AssertionError("extra fields must be rejected")


def test_canonical_failure_is_order_level_unavailable():
    r = DojiPatternsRequest(symbols=["600000.SH"], start=date(2025, 1, 1), end=date(2025, 12, 31))
    out = evaluate_doji_patterns(r, BrokenCanonical(), object(), object())
    assert out.status is DojiStatus.UNAVAILABLE and not out.factors


def test_empty_but_valid_inputs_return_ok_zero_parent_results():
    request = DojiPatternsRequest(
        symbols=["600000.SH"],
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
    )

    result = evaluate_doji_patterns(
        request,
        EmptyCanonical(),
        EmptyFacts(),
        EmptyPresence(),
    )

    assert result.status is DojiStatus.OK
    assert len(result.factors) == 5
    assert all(factor.parent_events == 0 for factor in result.factors)
