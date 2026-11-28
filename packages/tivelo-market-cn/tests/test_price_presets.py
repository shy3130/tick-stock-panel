import json
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib import metadata

import pytest
from pydantic import ValidationError
from test_fee_presets import compile as compile_fees
from test_quantity_presets import classification
from test_rules import calendar, instrument, terms
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.market import ReferencePrice

from tivelo_market_cn.adapter import CNMarketProfile, create_rules
from tivelo_market_cn.price_presets import (
    PricePreset,
    PricePresetContents,
    compile_price_terms,
    load_price_preset,
    resolve_price_rules,
)
from tivelo_market_cn.rules import RuleConfiguration


def at(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def base(state=None, **changes):
    state = state or classification()
    return terms(
        **dict(
            venue_id=state.venue_id,
            spec_revision=state.spec_revision,
            valid_from=at("2024-01-01T00:00:00+08:00"),
            valid_to=at("2027-01-01T00:00:00+08:00"),
            available_at=at("2023-12-01T00:00:00+08:00"),
        )
        | changes
    )


def archived_price_profile():
    state = classification(risk_status="risk_warning")
    prices = compile_price_terms(load_price_preset(version="2026.1.0"), state, (base(state),))
    fees = compile_fees(start="2024-01-01")
    return CNMarketProfile(
        profile_format="tivelo-cn-profile-v3",
        calendar=calendar().version,
        fee_compilation=fees,
        price_compilations=(prices,),
        configuration=RuleConfiguration(
            schema_version="1.2",
            profile_id="synthetic-quantity-with-official-prices-and-fees",
            profile_version="1",
            currency="CNY",
            terms=prices.terms,
            instrument_classes=(state,),
            required_fee_ids=fees.required_fee_ids,
            fee_rules=fees.fee_rules,
        ),
    )


@pytest.mark.parametrize(
    "venue,board,ratio",
    [
        ("XSHG", "main", "0.1"),
        ("XSHE", "main", "0.1"),
        ("XSHG", "star", "0.2"),
        ("XSHE", "chinext", "0.2"),
    ],
)
@pytest.mark.parametrize("day", ["2024-01-02", "2025-07-01", "2026-07-06"])
def test_standard_prices_and_daily_limits(venue, board, ratio, day):
    selected = resolve_price_rules(
        load_price_preset(version="2026.1.0"),
        classification(venue, board),
        at=at(day + "T10:00:00+08:00"),
    )
    assert selected.scope == "daily_price_limits_and_tick"
    assert selected.price_tick == Decimal("0.01")
    assert selected.limit_up_ratio == selected.limit_down_ratio == Decimal(ratio)


@pytest.mark.parametrize("venue", ["XSHG", "XSHE"])
@pytest.mark.parametrize(
    "moment,ratio",
    [
        ("2025-06-28T10:00:00+08:00", "0.05"),
        ("2026-04-25T10:00:00+08:00", "0.05"),
        ("2026-07-05T23:59:59+08:00", "0.05"),
        ("2026-07-06T00:00:00+08:00", "0.1"),
    ],
)
def test_warning_change_uses_effective_date_not_draft_or_announcement(venue, moment, ratio):
    selected = resolve_price_rules(
        load_price_preset(version="2026.1.0"),
        classification(venue, risk_status="risk_warning"),
        at=at(moment),
    )
    assert selected.limit_up_ratio == selected.limit_down_ratio == Decimal(ratio)


@pytest.mark.parametrize(
    "venue,board,phase,ratio",
    [
        ("XSHG", "main", "delisting", "0.1"),
        ("XSHE", "main", "delisting", "0.1"),
        ("XSHG", "star", "regular", "0.2"),
        ("XSHE", "chinext", "regular", "0.2"),
        ("XSHG", "star", "delisting", "0.2"),
        ("XSHE", "chinext", "delisting", "0.2"),
        ("XSHG", "main", "ipo_first_five", None),
        ("XSHE", "chinext", "ipo_first_five", None),
        ("XSHG", "star", "delisting_first", None),
        ("XSHE", "main", "delisting_first", None),
        ("XSHG", "main", "relisting_first", None),
        ("XSHE", "chinext", "relisting_first", None),
    ],
)
def test_special_phases_do_not_inherit_main_board_warning_limit(venue, board, phase, ratio):
    selected = resolve_price_rules(
        load_price_preset(version="2026.1.0"),
        classification(venue, board, risk_status="risk_warning", listing_phase=phase),
        at=at("2025-01-03T10:00:00+08:00"),
    )
    assert selected.price_limit_mode == ("unlimited" if ratio is None else "relative_reference")
    assert (
        selected.limit_up_ratio
        == selected.limit_down_ratio
        == (None if ratio is None else Decimal(ratio))
    )


def test_unknown_coverage_and_class_are_not_treated_as_unlimited():
    preset = load_price_preset(version="2026.1.0")
    for moment in ("2023-12-31T23:59:59+08:00", "2027-01-01T00:00:00+08:00"):
        with pytest.raises(TiveloError) as error:
            resolve_price_rules(preset, classification(), at=at(moment))
        assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
    with pytest.raises(TiveloError) as error:
        resolve_price_rules(
            preset,
            classification(board="star", listing_phase="relisting_first"),
            at=at("2025-01-03T10:00:00+08:00"),
        )
    assert error.value.code == ErrorCode.UNSUPPORTED_ASSET


def test_compilation_preserves_nonprice_terms_and_both_histories():
    state = classification(risk_status="risk_warning")
    first = base(state, price_tick="0.1", limit_up_ratio="0.4", limit_down_ratio="0.4")
    second = first.model_copy(
        update={
            "valid_from": at("2025-01-01T00:00:00+08:00"),
            "available_at": at("2025-01-02T00:00:00+08:00"),
            "revision_id": "quantity-next",
            "buy_maximum": Decimal("10000"),
        }
    )
    compiled = compile_price_terms(load_price_preset(version="2026.1.0"), state, (first, second))
    assert compiled.inputs.base_terms == (first, second)
    assert len(compiled.terms) == 3
    assert {t.price_tick for t in compiled.terms} == {Decimal("0.01")}
    assert {t.buy_maximum for t in compiled.terms} == {Decimal("10000"), Decimal("1000000")}
    assert all(t.sell_lag_sessions == 1 and t.buy_minimum == 100 for t in compiled.terms)
    assert {t.valid_from for t in compiled.terms} == {
        first.valid_from,
        second.valid_from,
        at("2026-07-06T00:00:00+08:00"),
    }
    assert {t.limit_up_ratio for t in compiled.terms} == {Decimal("0.05"), Decimal("0.1")}


def test_price_epoch_does_not_reverse_base_history_priority():
    profile = archived_price_profile()
    state = profile.configuration.instrument_classes[0]
    first = base(state)
    second = first.model_copy(
        update={
            "valid_from": at("2025-01-01T00:00:00+08:00"),
            "available_at": at("2025-01-02T00:00:00+08:00"),
            "revision_id": "newer-epoch",
            "buy_maximum": Decimal("10000"),
        }
    )
    late_old = first.model_copy(
        update={
            "available_at": at("2026-07-10T00:00:00+08:00"),
            "revision_id": "late-old-epoch",
            "buy_maximum": Decimal("500000"),
        }
    )
    compiled = compile_price_terms(
        load_price_preset(version="2026.1.0"), state, (first, second, late_old)
    )
    updated = CNMarketProfile(
        profile_format="tivelo-cn-profile-v3",
        calendar=profile.calendar,
        configuration=profile.configuration.model_copy(update={"terms": compiled.terms}),
        fee_compilation=profile.fee_compilation,
        price_compilations=(compiled,),
    )
    rules = create_rules(configuration_json=updated.model_dump_json())
    security = state.bind(instrument())
    for instant in (
        "2026-07-05T10:00:00+08:00",
        "2026-07-06T10:00:00+08:00",
        "2026-07-11T10:00:00+08:00",
    ):
        assert rules.trading_terms(security, at=at(instant)).buy_maximum == 10000


def test_unavailable_new_epoch_does_not_revive_expired_old_prices():
    original = load_price_preset(version="2026.1.0")
    sources = tuple(
        s.model_copy(update={"published_on": date(2026, 7, 7)})
        if s.source_id == "sse-notice-2026"
        else s
        for s in original.contents.sources
    )
    preset = PricePreset.create(original.contents.model_copy(update={"sources": sources}))
    state = classification(risk_status="risk_warning")
    moment = at("2026-07-06T10:00:00+08:00")
    with pytest.raises(TiveloError) as error:
        resolve_price_rules(preset, state, at=moment)
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE
    compiled = compile_price_terms(preset, state, (base(state),))
    profile = archived_price_profile()
    # A v1 derived configuration is used only to test the generic temporal
    # selector with this intentionally modified source, not the pinned loader.
    rules = create_rules(
        configuration_json=CNMarketProfile(
            configuration=profile.configuration.model_copy(update={"terms": compiled.terms}),
            calendar=profile.calendar,
        ).model_dump_json()
    )
    security = state.bind(instrument())
    with pytest.raises(TiveloError) as error:
        rules.trading_terms(security, at=moment)
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


@pytest.mark.parametrize(
    "known,expiry",
    [
        ("2026-06-01", "2027-01-01"),
        ("2026-07-10", "2027-01-01"),
        ("2026-06-01", "2026-07-05"),
        ("2026-07-10", "2026-07-08"),
    ],
)
def test_price_composition_preserves_base_selection_and_expiry(known, expiry):
    profile = archived_price_profile()
    state = profile.configuration.instrument_classes[0]
    original = base(state)
    newer = original.model_copy(
        update={
            "valid_from": at("2026-07-04T00:00:00+08:00"),
            "available_at": at(known + "T00:00:00+08:00"),
            "valid_to": at(expiry + "T00:00:00+08:00"),
            "revision_id": "newer",
            "buy_maximum": Decimal("10000"),
        }
    )
    bases = (original, newer)
    compiled = compile_price_terms(load_price_preset(version="2026.1.0"), state, bases)
    variants = []
    for history in (bases, compiled.terms):
        variants.append(
            create_rules(
                configuration_json=CNMarketProfile(
                    configuration=profile.configuration.model_copy(update={"terms": history}),
                    calendar=profile.calendar,
                ).model_dump_json()
            )
        )
    security = state.bind(instrument())
    for day in (3, 4, 5, 6, 7, 8, 10, 11):
        outcomes = []
        for rules in variants:
            try:
                outcomes.append(
                    rules.trading_terms(
                        security, at=at(f"2026-07-{day:02d}T10:00:00+08:00")
                    ).buy_maximum
                )
            except TiveloError as error:
                outcomes.append(error.code)
        assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize(
    "changes",
    [
        {"valid_to": None},
        {"spec_revision": "wrong"},
        {"venue_id": "XSHE"},
        {"instrument_type": "preferred_stock"},
    ],
)
def test_compilation_rejects_unbounded_or_mismatched_base(changes):
    with pytest.raises(ValidationError):
        compile_price_terms(
            load_price_preset(version="2026.1.0"), classification(), (base(**changes),)
        )


def test_compilation_does_not_clip_missing_coverage_or_accept_forged_models():
    preset = load_price_preset(version="2026.1.0")
    for changes in (
        {"valid_from": at("2023-01-01T00:00:00+08:00")},
        {"valid_to": at("2027-01-02T00:00:00+08:00")},
    ):
        with pytest.raises(TiveloError) as error:
            compile_price_terms(preset, classification(), (base(**changes),))
        assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
    with pytest.raises(ValidationError):
        compile_price_terms(
            preset, classification(), (base().model_copy(update={"buy_minimum": Decimal(-1)}),)
        )


@pytest.mark.parametrize(
    "moment,reference,lower,upper",
    [
        ("2025-01-03T10:00:00+08:00", "10", "9.50", "10.50"),
        ("2026-07-06T10:00:00+08:00", "10.05", "9.05", "11.06"),
        ("2026-07-06T10:00:00+08:00", "0.01", "0.01", "0.02"),
    ],
)
def test_installed_factory_uses_historical_raw_price_limits(moment, reference, lower, upper):
    profile = archived_price_profile()
    (entry,) = metadata.entry_points(
        group="tivelo.market_rules.v1", name="tivelo.market.cn.cash-equity"
    )
    rules = entry.load()(configuration_json=profile.model_dump_json())
    state = profile.configuration.instrument_classes[0]
    security = state.bind(instrument())
    instant = at(moment)
    price = ReferencePrice(
        instrument_id=security.instrument_id,
        trading_date=date.fromisoformat(moment[:10]),
        value=reference,
        kind="exchange_ex_rights",
        available_at=instant,
        source_id="synthetic-exchange-reference",
        revision_id="ref1",
    )
    bounds = rules.price_bounds(security, price, at=instant)
    assert bounds.lower == Decimal(lower) and bounds.upper == Decimal(upper)
    assert bounds.reference_revision == "ref1"
    assert bounds.rule_revision == rules.trading_terms(security, at=instant).revision_id


def test_v3_roundtrip_and_legacy_absent_field_encoding():
    profile = archived_price_profile()
    assert CNMarketProfile.model_validate_json(canonical_bytes(profile)) == profile
    for version in ("tivelo-cn-profile-v1", "tivelo-cn-profile-v2"):
        payload = profile.model_dump(mode="json")
        payload["profile_format"] = version
        payload.pop("price_compilations")
        if version.endswith("v1"):
            payload.pop("fee_compilation")
        legacy = CNMarketProfile.model_validate(payload)
        assert "price_compilations" not in legacy.model_dump()
        assert (
            canonical_bytes(legacy)
            == json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        )


@pytest.mark.parametrize(
    "part",
    [
        "base",
        "classification",
        "compiled",
        "configured",
        "pin",
        "selected",
        "missing",
        "old_format",
        "duplicate",
    ],
)
def test_profile_rejects_price_input_or_execution_mismatch(part):
    raw = archived_price_profile().model_dump(mode="json")
    compiled = raw["price_compilations"][0]
    if part == "base":
        compiled["inputs"]["base_terms"][0]["sell_lag_sessions"] = 0
    elif part == "classification":
        compiled["inputs"]["classification"]["risk_status"] = "normal"
    elif part == "compiled":
        compiled["terms"][0]["limit_up_ratio"] = "0.1"
    elif part == "configured":
        raw["configuration"]["terms"][0]["price_tick"] = "0.1"
    elif part == "pin":
        compiled["inputs"]["preset_version_id"] = "a" * 64
    elif part == "selected":
        compiled["selected_rule_ids"] = ["invented"]
    elif part == "missing":
        raw.pop("price_compilations")
    elif part == "old_format":
        raw["profile_format"] = "tivelo-cn-profile-v2"
    else:
        raw["price_compilations"].append(compiled)
    with pytest.raises((ValidationError, TiveloError)):
        CNMarketProfile.model_validate(raw)


def test_offline_pins_and_damaged_resources(monkeypatch, tmp_path):
    import socket

    from tivelo_market_cn import price_presets

    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    original = load_price_preset(version="2026.1.0")
    assert len(original.contents.sources) == 9 and len(original.contents.schedules) == 8
    assert load_price_preset(version="2026.1.0", expected_sha256=original.version_id) == original
    for version, digest in (("latest", None), ("../2026.1.0", None), ("2026.1.0", "a" * 64)):
        with pytest.raises(TiveloError):
            load_price_preset(version=version, expected_sha256=digest)
    directory = tmp_path / "price_presets"
    directory.mkdir()
    target = directory / "2026.1.0.json"
    monkeypatch.setattr(price_presets.resources, "files", lambda name: tmp_path)
    with pytest.raises(TiveloError):
        load_price_preset(version="2026.1.0")
    for payload in (
        "{",
        PricePreset.create(
            original.contents.model_copy(update={"coverage_end": date(2026, 12, 30)})
        ).model_dump_json(),
    ):
        target.write_text(payload)
        with pytest.raises(TiveloError) as error:
            load_price_preset(version="2026.1.0")
        assert error.value.code == ErrorCode.ARTIFACT_CORRUPT


def test_duplicate_epochs_and_missing_sources_are_rejected():
    original = load_price_preset(version="2026.1.0").contents
    for changes in (
        {"sources": original.sources[1:]},
        {
            "schedules": (
                *original.schedules,
                original.schedules[0].model_copy(update={"rule_id": "duplicate"}),
            )
        },
    ):
        with pytest.raises(ValidationError):
            PricePresetContents.model_validate_json(
                original.model_copy(update=changes).model_dump_json()
            )
