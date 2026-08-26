from datetime import date

import pytest
from pydantic import ValidationError

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
