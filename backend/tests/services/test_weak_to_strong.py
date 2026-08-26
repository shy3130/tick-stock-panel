from datetime import date

import pytest
from pydantic import ValidationError

from app.services import weak_to_strong
from app.services.weak_to_strong import (
    WeakToStrongEvaluateRequest,
    evaluate_weak_to_strong_v1,
    validate_evidence_keys,
)


def test_missing_reader_is_unavailable():
    request = WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 2), symbols=["600000.SH"])
    result = evaluate_weak_to_strong_v1(request)
    assert result.manifest.status == "unavailable"
    assert result.evaluations[0].core_status == "unavailable"


def test_duplicate_or_unknown_symbols_rejected():
    with pytest.raises(ValidationError):
        WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 2), symbols=["600000.SH", "sh600000"])
    with pytest.raises(ValidationError):
        WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 2), symbols=["600000.XX"])


def test_extra_fields_and_trading_evidence_keys_rejected():
    with pytest.raises(ValidationError):
        WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 2), symbols=["600000.SH"], target_price=1)
    with pytest.raises(ValueError):
        validate_evidence_keys(["target_price"])


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("600000.SH", "600000"),
        ("sh.600000", "600000"),
        ("SZ000001", "000001"),
        ("300001.sz", "300001"),
        ("sh.600000.SH", "600000"),
    ],
)
def test_exchange_qualifier_must_match_code_market(raw, canonical):
    assert weak_to_strong.canonicalize_symbol(raw) == canonical


@pytest.mark.parametrize("raw", ["600000.SZ", "000001.SH", "BJ600000", "sh.600000.sz"])
def test_exchange_code_mismatch_is_rejected(raw):
    with pytest.raises(ValueError):
        weak_to_strong.canonicalize_symbol(raw)


@pytest.fixture()
def isolated_reader_registry():
    original = list(weak_to_strong._READER_FACTORIES)
    weak_to_strong._READER_FACTORIES.clear()
    yield
    weak_to_strong._READER_FACTORIES[:] = original


class _FullCapabilityReader:
    def capabilities(self):
        return frozenset(weak_to_strong.REQUIRED_CAPABILITIES)


def _raising_reader_factory():
    raise RuntimeError("snapshot unavailable")


def test_reader_factory_failure_does_not_mask_later_candidate(isolated_reader_registry):
    weak_to_strong.register_reader_factory(_raising_reader_factory)
    weak_to_strong.register_reader_factory(_FullCapabilityReader)

    assert isinstance(weak_to_strong.resolve_weak_to_strong_reader(), _FullCapabilityReader)


def test_all_reader_factory_failures_return_reader_missing(
    isolated_reader_registry,
):
    weak_to_strong.register_reader_factory(_raising_reader_factory)
    request = WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 2), symbols=["600000.SH"])

    result = evaluate_weak_to_strong_v1(request)

    assert result.manifest.status == "unavailable"
    assert result.evaluations[0].status_reason == "reader_missing"
