from decimal import Decimal
from importlib import metadata

import pytest
from pydantic import ValidationError
from test_rules import at, calendar, fee, instrument, terms
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_market_cn.adapter import CNMarketProfile
from tivelo_market_cn.rules import CNTradingRules, RuleConfiguration


def configuration(*versions, schema_version="1.2", fees=None, **extra):
    return RuleConfiguration(
        schema_version=schema_version,
        profile_id="cn.synthetic.history",
        profile_version="1",
        currency="CNY",
        terms=versions or (terms(),),
        required_fee_ids=("commission",),
        fee_rules=fees or (fee(),),
        **extra,
    )


def history(*versions):
    return CNTradingRules(configuration(*versions), calendar())


def epochs():
    return (
        terms(limit_up_ratio="0.05", limit_down_ratio="0.05", revision_id="old"),
        terms(valid_from=at(3, 0), available_at=at(2, 12), revision_id="new"),
    )


@pytest.mark.parametrize(
    "day,hour,expected",
    [(1, 1, "old"), (2, 1, "old"), (2, 13, "old"), (3, 0, "new"), (6, 1, "new")],
)
def test_announcement_and_effective_time_are_independent(day, hour, expected):
    market = history(*epochs())
    selected = market.trading_terms(instrument(), at=at(day, hour))
    assert selected.revision_id == expected
    assert selected.limit_up_ratio == Decimal("0.05" if expected == "old" else "0.1")


def test_unpublished_epoch_cannot_change_past_rules():
    old, new = epochs()
    new = new.model_copy(update={"available_at": at(5, 0)})
    assert history(old, new).trading_terms(instrument(), at=at(4)).revision_id == "old"
    assert history(old, new).trading_terms(instrument(), at=at(6)).revision_id == "new"


def test_future_end_is_visible_only_after_its_revision_is_known():
    old, new = epochs()
    revised = old.model_copy(
        update={"available_at": at(2, 12), "valid_to": at(3, 0), "revision_id": "old-end-announced"}
    )
    market = history(old, revised, new)
    assert market.trading_terms(instrument(), at=at(2)).valid_to is None
    assert market.trading_terms(instrument(), at=at(2, 13)).valid_to == at(3, 0)
    assert market.trading_terms(instrument(), at=at(3)).revision_id == "new"


def test_late_correction_of_old_epoch_does_not_supersede_new_epoch():
    old, new = epochs()
    corrected_old = old.model_copy(
        update={
            "available_at": at(4),
            "revision_id": "old-correction",
            "buy_maximum": Decimal(500000),
        }
    )
    market = history(new, corrected_old, old)
    assert market.trading_terms(instrument(), at=at(6)).revision_id == "new"


@pytest.mark.parametrize("correction", [False, True])
def test_expired_latest_epoch_never_resurrects_an_older_rule(correction):
    old, new = epochs()
    expired = new.model_copy(
        update={
            "valid_to": at(4, 0),
            "revision_id": "expired",
            "available_at": at(4, 0) if correction else new.available_at,
        }
    )
    market = history(old, new, expired) if correction else history(old, expired)
    assert market.trading_terms(instrument(), at=at(3)).revision_id == (
        "new" if correction else "expired"
    )
    with pytest.raises(TiveloError) as error:
        market.trading_terms(instrument(), at=at(6))
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


def test_latest_known_replacement_has_no_fallback_during_a_gap():
    old = terms(valid_to=at(3), revision_id="old")
    future = terms(valid_from=at(5), available_at=at(2), revision_id="future")
    with pytest.raises(TiveloError) as error:
        history(old, future).trading_terms(instrument(), at=at(4))
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


@pytest.mark.parametrize(
    "change",
    [
        {"revision_id": "duplicate-time"},
        {"valid_from": at(2), "available_at": at(2)},
        {
            "valid_from": at(2),
            "available_at": at(2),
            "revision_id": "venue-change",
            "venue_id": "XSHE",
        },
        {
            "valid_from": at(2),
            "available_at": at(2),
            "revision_id": "currency-change",
            "currency": "USD",
        },
    ],
)
def test_conflicting_or_reused_version_identity_is_rejected(change):
    original = terms()
    with pytest.raises(ValidationError):
        configuration(original, original.model_copy(update=change))


@pytest.mark.parametrize("old_version", ["1.0", "1.1"])
def test_temporal_rules_require_a_new_configuration_version(old_version):
    with pytest.raises(ValidationError):
        configuration(*epochs(), schema_version=old_version)


def test_installed_adapter_retains_the_rule_history_and_identity():
    original = history(*epochs())
    profile = CNMarketProfile(
        configuration=original.configuration, calendar=original.calendar.version
    )
    (entry,) = metadata.entry_points(
        group="tivelo.market_rules.v1", name="tivelo.market.cn.cash-equity"
    )
    restored = entry.load()(configuration_json=profile.model_dump_json())
    assert restored.reference == original.reference
    assert canonical_bytes(restored.configuration) == canonical_bytes(original.configuration)
    assert restored.trading_terms(instrument(), at=at(2)).revision_id == "old"
    assert restored.trading_terms(instrument(), at=at(3)).revision_id == "new"


def test_old_single_term_configuration_keeps_its_original_wire_shape():
    original = configuration(terms(), schema_version="1.0")
    assert b"instrument_classes" not in canonical_bytes(original)
    restored = RuleConfiguration.model_validate_json(original.model_dump_json())
    assert canonical_bytes(restored) == canonical_bytes(original)
    assert CNTradingRules(restored, calendar()).trading_terms(instrument(), at=at(2)) == terms()


def test_fees_follow_their_own_known_and_effective_epochs():
    first = fee(revision_id="fee-old")
    ended = fee(revision_id="fee-end-announced", available_at=at(2, 12), valid_to=at(3, 0))
    second = fee(revision_id="fee-new", valid_from=at(3, 0), available_at=at(2, 12), rate="0.0003")
    market = CNTradingRules(configuration(fees=(first, ended, second)), calendar())
    for moment, expected, revision in (
        (at(2), "20", "fee-old"),
        (at(2, 13), "20", "fee-end-announced"),
        (at(3), "30", "fee-new"),
    ):
        quote = market.fee_breakdown(instrument(), "buy", Decimal(10000), Decimal(10), at=moment)
        assert quote.total == Decimal(expected)
        assert quote.components[0].rule_revision == revision


def test_late_fee_correction_cannot_replace_the_newer_effective_fee():
    old = fee(revision_id="old")
    correction = fee(revision_id="old-correction", available_at=at(4), rate="0.001")
    new = fee(revision_id="new", valid_from=at(3), available_at=at(2), rate="0.0003")
    market = CNTradingRules(configuration(fees=(old, new, correction)), calendar())
    assert market.fees(instrument(), "buy", Decimal(10000), Decimal(10), at=at(6)) == 30


def test_expired_fee_is_missing_and_never_resurrects_previous_rate():
    old = fee(revision_id="old")
    expired = fee(revision_id="expired", valid_from=at(3), valid_to=at(4), available_at=at(2))
    market = CNTradingRules(configuration(fees=(old, expired)), calendar())
    with pytest.raises(TiveloError) as error:
        market.fees(instrument(), "buy", Decimal(10000), Decimal(10), at=at(6))
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


@pytest.mark.parametrize("old_version", ["1.0", "1.1"])
def test_previous_fee_period_and_revision_identity_semantics_remain_compatible(old_version):
    # Historical v1 configurations allowed a provider to reuse its fee revision label.
    old = fee(valid_to=at(3))
    new = fee(valid_from=at(3), available_at=at(2), rate="0.0003")
    config = configuration(schema_version=old_version, fees=(old, new))
    market = CNTradingRules(config, calendar())
    assert market.fees(instrument(), "buy", Decimal(10000), Decimal(10), at=at(2)) == 20
    assert market.fees(instrument(), "buy", Decimal(10000), Decimal(10), at=at(3)) == 30


@pytest.mark.parametrize(
    "new", [fee(revision_id="same-time"), fee(valid_from=at(3), available_at=at(2))]
)
def test_new_fee_history_rejects_ambiguous_or_reused_identity(new):
    with pytest.raises(ValidationError):
        configuration(fees=(fee(), new))
