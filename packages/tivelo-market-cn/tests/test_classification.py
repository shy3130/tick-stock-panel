import pytest
from pydantic import ValidationError
from test_rule_history import configuration
from test_rules import at, calendar, instrument, terms
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_market_cn.classification import CNEquityClass
from tivelo_market_cn.rules import CNTradingRules


def classification(**changes):
    values = dict(venue_id="XSHG", board="main", risk_status="normal", listing_phase="regular")
    values.update(changes)
    return CNEquityClass(**values)


def test_class_binding_preserves_historical_source_and_never_reads_symbol_or_name():
    descriptor = classification()
    original = instrument(display_symbol="300001.SZ", name="*ST misleading", valid_to=at(5))
    bound = descriptor.bind(original)
    assert bound.spec_revision == descriptor.spec_revision
    assert original.spec_revision == "main-v1"
    assert bound.model_dump(exclude={"spec_revision"}) == original.model_dump(
        exclude={"spec_revision"}
    )
    assert (
        CNEquityClass.model_validate_json(canonical_bytes(descriptor)).spec_revision
        == descriptor.spec_revision
    )
    assert descriptor.bind(bound) == bound


@pytest.mark.parametrize(
    "change",
    [
        {"board": "star"},
        {"risk_status": "risk_warning"},
        {"listing_phase": "ipo_first_five"},
        {"listing_phase": "relisting_first"},
        {"listing_phase": "delisting_first"},
        {"listing_phase": "delisting"},
        {"venue_id": "XSHE"},
    ],
)
def test_each_explicit_classification_dimension_changes_spec_identity(change):
    assert classification(**change).spec_revision != classification().spec_revision


@pytest.mark.parametrize(
    "change",
    [
        {"venue_id": "XSHG", "board": "chinext"},
        {"venue_id": "XSHE", "board": "star"},
        {"board": "unknown"},
        {"risk_status": "unknown"},
        {"listing_phase": "unknown"},
    ],
)
def test_unknown_or_inconsistent_classification_is_not_normalized_to_main(change):
    with pytest.raises(ValidationError):
        classification(**change)


@pytest.mark.parametrize(
    "change",
    [
        {"venue_id": "XSHE"},
        {"asset_class": "fund"},
        {"market_id": "us.equity"},
        {"quote_currency": "USD"},
        {"instrument_type": "depositary_receipt"},
    ],
)
def test_binding_rejects_wrong_security_scope(change):
    with pytest.raises(TiveloError) as error:
        classification().bind(instrument(**change))
    assert error.value.code == ErrorCode.UNSUPPORTED_ASSET


def test_class_changes_require_the_historical_instrument_state_to_change():
    regular = classification()
    warning = classification(risk_status="risk_warning")
    normal_terms = terms(spec_revision=regular.spec_revision, revision_id="normal")
    warning_terms = terms(
        spec_revision=warning.spec_revision,
        limit_up_ratio="0.05",
        limit_down_ratio="0.05",
        revision_id="warning",
    )
    market = CNTradingRules(
        configuration(normal_terms, warning_terms, instrument_classes=(regular, warning)),
        calendar(),
    )
    before = regular.bind(instrument(valid_to=at(3)))
    after = warning.bind(
        instrument(valid_from=at(3), available_at=at(2), revision_id="warning-announced")
    )
    assert market.trading_terms(before, at=at(2)).revision_id == "normal"
    assert market.trading_terms(after, at=at(3)).revision_id == "warning"
    with pytest.raises(TiveloError) as error:
        market.trading_terms(after, at=at(2))
    assert error.value.code == ErrorCode.FUTURE_INFORMATION
    with pytest.raises(TiveloError) as error:
        market.trading_terms(before, at=at(3))
    assert error.value.code == ErrorCode.FUTURE_INFORMATION


@pytest.mark.parametrize("mode", ["old-schema", "duplicate", "missing", "wrong-venue"])
def test_configuration_validates_class_registration(mode):
    descriptor = classification()
    term = terms(spec_revision=descriptor.spec_revision)
    descriptors = (descriptor,)
    version = "1.2"
    if mode == "old-schema":
        version = "1.1"
    if mode == "duplicate":
        descriptors = (descriptor, descriptor)
    if mode == "missing":
        term = terms()
    if mode == "wrong-venue":
        term = term.model_copy(update={"venue_id": "XSHE"})
    with pytest.raises(ValidationError):
        configuration(term, schema_version=version, instrument_classes=descriptors)
