from datetime import UTC, datetime
from decimal import Decimal, localcontext

import pytest
from pydantic import TypeAdapter, ValidationError

from tivelo_contracts.base import ExactDecimal, RelativePath, canonical_bytes
from tivelo_contracts.data import Bar, CorporateAction, Coverage, KnownDate


@pytest.mark.parametrize("value", [datetime(2025, 1, 2, 7), "2025-01-02T07:00:00", 1735801200])
def test_d02_naive_or_numeric_timestamp_is_never_inferred(make_bar, value):
    with pytest.raises(ValidationError, match="timezone"):
        make_bar(available_at=value)


def test_d02_explicit_beijing_offset_preserves_absolute_time(make_bar):
    bar = make_bar(available_at="2025-01-02T15:00:00+08:00")
    assert bar.available_at == datetime(2025, 1, 2, 7, tzinfo=UTC)
    assert bar.model_dump(mode="json")["available_at"] == "2025-01-02T07:00:00Z"


@pytest.mark.parametrize("value", [1735801200, datetime(2025, 1, 2, tzinfo=UTC), "20250102"])
def test_trading_date_is_never_inferred_from_a_timestamp(make_bar, value):
    with pytest.raises(ValidationError, match="calendar dates"):
        make_bar(trading_date=value)


def test_submicrosecond_time_is_rejected_instead_of_silently_truncated(make_bar):
    with pytest.raises(ValidationError, match="precision"):
        make_bar(available_at="2025-01-02T07:00:00.0000001Z")


def test_d03_dst_and_night_session_keep_trading_date(make_bar):
    # Synthetic NY overnight interval crosses the repeated hour on DST fall-back.
    bar = make_bar(
        bar_start="2025-11-02T01:30:00-04:00",
        bar_end="2025-11-02T01:30:00-05:00",
        available_at="2025-11-02T01:30:00-05:00",
        ingested_at="2025-11-02T06:31:00Z",
        trading_date="2025-11-03",
        session_id="synthetic.night",
    )
    assert (bar.bar_end - bar.bar_start).total_seconds() == 3600
    assert str(bar.trading_date) == "2025-11-03"


def test_d03_spring_gap_has_no_synthetic_wall_clock_hour(make_bar):
    bar = make_bar(
        bar_start="2025-03-09T01:59:00-05:00",
        bar_end="2025-03-09T03:00:00-04:00",
        available_at="2025-03-09T03:00:00-04:00",
        ingested_at="2025-03-09T07:01:00Z",
        trading_date="2025-03-10",
        session_id="synthetic.night",
    )
    assert (bar.bar_end - bar.bar_start).total_seconds() == 60
    assert str(bar.trading_date) == "2025-03-10"
    with pytest.raises(ValidationError, match="timezone"):
        make_bar(bar_start="2025-03-09T02:30:00")


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), 1.1, True, "NaN", "Infinity", "1e100000", "1e-100000"]
)
def test_d04_financial_facts_reject_approximate_or_unrepresentable_values(value):
    with pytest.raises(ValidationError):
        TypeAdapter(ExactDecimal).validate_python(value)


def test_d04_decimal128_boundary_is_not_rounded_by_decimal_context():
    exact = "99999999999999999999999999.999999999999"
    with localcontext() as context:
        context.prec = 6
        adapter = TypeAdapter(ExactDecimal)
        value = adapter.validate_python(exact)
        assert value == Decimal(exact)
        assert adapter.dump_json(value) == f'"{exact}"'.encode()
        assert adapter.validate_python("0.100000000000000000") == Decimal("0.1")


@pytest.mark.parametrize(
    "changes",
    [
        {"low": "11"},
        {"high": "9"},
        {"volume": "-1"},
        {"available_at": "2025-01-02T06:59:00Z"},
        {"bar_end": "2025-01-02T01:30:00Z"},
        {"schema_version": "3.0"},
        {"unknown_mandatory_field": "x"},
    ],
)
def test_invalid_bars_fail_at_contract_boundary(make_bar, changes):
    with pytest.raises(ValidationError):
        make_bar(**changes)


def test_round_trip_is_immutable_and_price_basis_remains_explicit(make_bar):
    bar = make_bar(price_basis="qfq")
    restored = Bar.model_validate_json(canonical_bytes(bar))
    assert restored == bar
    assert restored.price_basis == "qfq"
    with pytest.raises(ValidationError, match="frozen"):
        restored.price_basis = "raw"


@pytest.mark.parametrize(
    "state,value", [("unknown", "2025-01-01"), ("known", None), ("not_yet", "2025-01-01")]
)
def test_unknown_and_not_yet_are_not_encoded_as_known_dates(state, value):
    with pytest.raises(ValidationError):
        KnownDate(state=state, value=value)


@pytest.mark.parametrize(
    "values",
    [
        dict(status="complete", requested_units=2, completed_units=1, row_count=10),
        dict(status="complete", requested_units=2, completed_units=2, row_count=0),
        dict(status="partial", requested_units=2, completed_units=1, row_count=10),
        dict(status="empty", requested_units=2, completed_units=0, row_count=0),
        dict(status="error", requested_units=2, completed_units=0, row_count=3),
    ],
)
def test_d05_incomplete_coverage_cannot_claim_success(values):
    with pytest.raises(ValidationError):
        Coverage(**values)


def test_d05_empty_with_explanation_differs_from_failure():
    empty = Coverage(
        status="empty",
        requested_units=1,
        completed_units=1,
        row_count=0,
        issues=({"reason": "suspended", "detail": "No trades in the requested session"},),
    )
    failed = Coverage(
        status="error",
        requested_units=1,
        completed_units=0,
        row_count=0,
        issues=({"reason": "source_error", "detail": "Provider unavailable"},),
    )
    assert empty.status != failed.status


@pytest.mark.parametrize(
    "path", ["../facts", "a/../b", "/absolute", "C:/data", "a\\b", "a//b", "a/./b", "x\x00y"]
)
def test_artifacts_cannot_escape_their_snapshot(path):
    with pytest.raises(ValidationError):
        TypeAdapter(RelativePath).validate_python(path)


def test_cash_event_does_not_invent_payment_time_or_shares():
    action = CorporateAction(
        action_id="dividend-1",
        instrument_id="security-1",
        action_type="cash_dividend",
        effective_at="2025-01-02T00:00:00Z",
        available_at="2024-12-20T00:00:00Z",
        revision_id="r1",
        source_id="synthetic",
        cash_per_unit="0.1",
        currency="CNY",
    )
    assert action.payment_at is None
    assert action.quantity_ratio is None


def test_instrument_rule_boundary_does_not_guess_asset_from_symbol(make_instrument):
    instrument = make_instrument(display_symbol="12345", venue_id="OTHER", market_id="synthetic")
    assert instrument.instrument_id == "security-1"
    assert instrument.asset_class == "equity"
    with pytest.raises(ValidationError, match="analytical"):
        make_instrument(asset_class="index")
