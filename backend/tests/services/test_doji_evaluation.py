from datetime import date

from app.services.doji_patterns.evaluation import evaluate_doji_patterns
from app.services.doji_patterns.models import DojiPatternsRequest, DojiStatus


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
