from datetime import UTC, date, datetime
from decimal import Decimal, Inexact, localcontext
from importlib import metadata

import pytest
from pydantic import ValidationError
from test_rules import calendar, instrument, terms
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_market_cn.adapter import CNMarketProfile
from tivelo_market_cn.calendar import CalendarVersion, CNCalendar
from tivelo_market_cn.fee_presets import (
    BrokerFeePolicy,
    FeePreset,
    FeePresetContents,
    FeeRounding,
    compile_fee_rules,
    load_fee_preset,
)
from tivelo_market_cn.rules import CNTradingRules, RuleConfiguration


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def policy(*, includes=(), rounding="half_up", **changes):
    values = dict(
        policy_id="synthetic.account",
        policy_version="1",
        account_scope="domestic_cash_standard",
        aggregation="per_fill_research",
        commission_rate="0.0002",
        commission_minimum="5",
        commission_includes=includes,
        rounding=tuple(
            FeeRounding(fee_id=key, quantum="0.01", rounding=rounding)
            for key in sorted(
                {"commission", "handling", "regulatory", "stamp", "transfer"} - set(includes)
            )
        ),
        source_reference="synthetic-policy-not-a-broker-quote",
        valid_from=at("2022-01-01"),
        available_at=at("2022-01-01"),
        revision_id="synthetic-policy-1",
        source_id="synthetic",
    )
    values.update(changes)
    return BrokerFeePolicy(**values)


def compile(*, preset=None, account=None, venue="XSHG", start="2022-07-01", end="2026-12-31"):
    return compile_fee_rules(
        preset or load_fee_preset(version="2026.1.0"),
        account or policy(),
        venue_id=venue,
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
    )


def market(compiled):
    # Only fee estimation uses this synthetic calendar; actual execution with a
    # compiled fee profile is covered in the cross-package account fixture.
    venue = compiled.inputs.venue_id
    original = calendar().version.contents
    calendar_version = CalendarVersion.create(original.model_copy(update={"venue_id": venue}))
    start = at("2022-01-01")
    profile = RuleConfiguration(
        schema_version="1.2",
        profile_id="synthetic.quantity-with-source-backed-fees",
        profile_version="1",
        currency="CNY",
        terms=(terms(venue_id=venue, valid_from=start, available_at=start),),
        required_fee_ids=compiled.required_fee_ids,
        fee_rules=compiled.fee_rules,
    )
    security = instrument(
        venue_id=venue,
        valid_from=start,
        available_at=start,
        listing_date=dict(state="known", value="2022-01-01"),
    )
    return CNTradingRules(profile, CNCalendar(calendar_version)), security


def quote(compiled, day, side="sell", quantity="1000", price="10"):
    rules, security = market(compiled)
    return rules.fee_breakdown(security, side, Decimal(quantity), Decimal(price), at=at(day))


@pytest.mark.parametrize("venue", ["XSHG", "XSHE"])
@pytest.mark.parametrize(
    "day,buy,sell,handling,stamp",
    [
        ("2022-07-01", "5.79", "15.79", "0.0000487", "0.001"),
        ("2023-08-20", "5.79", "15.79", "0.0000487", "0.001"),
        ("2023-08-27T15:59:59", "5.79", "15.79", "0.0000487", "0.001"),
        ("2023-08-27T16:00:00", "5.64", "10.64", "0.0000341", "0.0005"),
        ("2026-12-31T07:00:00", "5.64", "10.64", "0.0000341", "0.0005"),
    ],
)
def test_sourced_rates_change_on_chinese_effective_date(venue, day, buy, sell, handling, stamp):
    compiled = compile(venue=venue)
    purchased = quote(compiled, day, "buy")
    sold = quote(compiled, day)
    assert purchased.total == Decimal(buy)
    assert sold.total == Decimal(sell)
    components = {item.fee_id: item for item in sold.components}
    assert components["handling"].rate == Decimal(handling)
    assert components["stamp"].rate == Decimal(stamp)
    assert components["transfer"].rate == Decimal("0.00001")
    assert components["regulatory"].rate == Decimal("0.00002")
    assert components["commission"].minimum_applied
    assert "stamp" not in {item.fee_id for item in purchased.components}
    assert all(
        item.source_reference.startswith("cn.fees.research-per-fill:") for item in sold.components
    )


def test_large_notional_uses_commission_rate_instead_of_minimum():
    sold = quote(compile(), "2025-01-03", quantity="10000")
    assert sold.total == Decimal("76.41")
    commission = next(item for item in sold.components if item.fee_id == "commission")
    assert commission.amount == 20 and not commission.minimum_applied


@pytest.mark.parametrize(
    "includes,buy,sell",
    [
        (("handling", "regulatory"), "5.10", "10.10"),
        (("handling", "regulatory", "transfer"), "5", "10"),
    ],
)
def test_inclusive_commission_omits_separate_charges(includes, buy, sell):
    compiled = compile(account=policy(includes=includes))
    assert quote(compiled, "2025-01-03", "buy").total == Decimal(buy)
    sold = quote(compiled, "2025-01-03")
    assert sold.total == Decimal(sell)
    assert not set(includes) & {item.fee_id for item in sold.components}
    commission = next(item for item in compiled.fee_rules if item.fee_id == "commission")
    assert commission.included_fee_ids == includes
    assert any("handling" in item for item in compiled.selected_rate_ids)


@pytest.mark.parametrize(
    "mode,expected", [("half_up", "0.01"), ("half_even", "0"), ("up", "0.04"), ("down", "0")]
)
def test_billing_rounding_is_explicit_per_component_and_context_independent(mode, expected):
    compiled = compile(account=policy(rounding=mode, commission_rate="0", commission_minimum="0"))
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        sold = quote(compiled, "2025-01-03", quantity="1")
    assert sold.total == Decimal(expected)


@pytest.mark.parametrize("day", ["2024-12-31T15:59:59", "2025-01-07T16:00:00"])
def test_compiled_range_cannot_be_used_before_or_after_its_bounds(day):
    compiled = compile(start="2025-01-01", end="2025-01-07")
    with pytest.raises(TiveloError) as error:
        quote(compiled, day)
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


@pytest.mark.parametrize(
    "start,end",
    [("2022-06-30", "2023-01-01"), ("2025-01-01", "2027-01-01"), ("2025-01-02", "2025-01-01")],
)
def test_outside_or_reversed_catalog_ranges_reject(start, end):
    with pytest.raises(TiveloError) as error:
        compile(start=start, end=end)
    assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT


@pytest.mark.parametrize(
    "change", [dict(valid_from=at("2024-01-02")), dict(valid_to=at("2024-01-02"))]
)
def test_one_broker_policy_must_cover_the_entire_requested_range(change):
    with pytest.raises(TiveloError) as error:
        compile(account=policy(**change), start="2024-01-01", end="2024-01-03")
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


def test_future_broker_policy_never_becomes_a_known_fee_early():
    compiled = compile(
        account=policy(available_at=at("2025-01-03")), start="2025-01-01", end="2025-01-07"
    )
    with pytest.raises(TiveloError) as error:
        quote(compiled, "2025-01-02")
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE
    assert quote(compiled, "2025-01-03").total == Decimal("10.64")


def test_future_rate_source_is_not_applied_before_it_is_known():
    preset = load_fee_preset(version="2026.1.0")
    contents = preset.contents.model_copy(
        update={
            "sources": tuple(
                item.model_copy(update={"published_on": date(2023, 8, 29)})
                if item.source_id == "stamp-cut-2023"
                else item
                for item in preset.contents.sources
            )
        }
    )
    late = FeePreset.create(contents)
    compiled = compile(preset=late, start="2023-08-01", end="2023-08-31")
    assert quote(compiled, "2023-08-28").total == Decimal("15.64")
    assert quote(compiled, "2023-08-30").total == Decimal("10.64")
    with pytest.raises(TiveloError, match="not yet known"):
        compile(preset=late, start="2023-08-28", end="2023-08-31")


def test_included_fee_still_requires_a_sourced_rate():
    preset = load_fee_preset(version="2026.1.0")
    incomplete = FeePreset.create(
        preset.contents.model_copy(
            update={
                "rates": tuple(item for item in preset.contents.rates if item.fee_id != "handling")
            }
        )
    )
    with pytest.raises(TiveloError, match="No initial official rate"):
        compile(preset=incomplete, account=policy(includes=("handling",)))


@pytest.mark.parametrize(
    "change",
    [
        dict(aggregation="per_order"),
        dict(aggregation="per_day"),
        dict(account_scope="stock_connect"),
        dict(commission_includes=("stamp",)),
        dict(commission_includes=("handling", "handling")),
        dict(rounding=()),
        dict(commission_minimum="5.005"),
        dict(commission_rate=0.0002),
    ],
)
def test_unsupported_or_ambiguous_billing_is_not_silently_normalized(change):
    with pytest.raises(ValidationError):
        policy(**change)


def test_compilation_revalidates_unchecked_nested_copies():
    account = policy().model_copy(update={"aggregation": "per_day"})
    with pytest.raises(ValidationError):
        compile(account=account)
    preset = load_fee_preset(version="2026.1.0")
    corrupt = preset.model_copy(update={"version_id": "a" * 64})
    with pytest.raises(ValidationError):
        compile(preset=corrupt)


@pytest.mark.parametrize("archive_inputs", [False, True])
def test_installed_factory_consumes_existing_fee_contract_and_locks_policy(archive_inputs):
    first = compile(start="2025-01-01", end="2025-01-07")
    second = compile(account=policy(commission_rate="0.0003"), start="2025-01-01", end="2025-01-07")
    assert first.version_id != second.version_id
    assert first.fee_rules[0].source_reference != second.fee_rules[0].source_reference
    market_rules, security = market(first)
    profile = CNMarketProfile(
        configuration=market_rules.configuration,
        calendar=market_rules.calendar.version,
        **(
            {"profile_format": "tivelo-cn-profile-v2", "fee_compilation": first}
            if archive_inputs
            else {}
        ),
    )
    (entry,) = metadata.entry_points(
        group="tivelo.market_rules.v1", name="tivelo.market.cn.cash-equity"
    )
    loaded = entry.load()(configuration_json=profile.model_dump_json())
    assert loaded.reference == market_rules.reference
    assert canonical_bytes(loaded.configuration) == canonical_bytes(market_rules.configuration)
    assert loaded.fees(
        security, "sell", Decimal(1000), Decimal(10), at=at("2025-01-03")
    ) == Decimal("10.64")


def archived_profile():
    compiled = compile(start="2025-01-01", end="2025-01-07")
    rules, _ = market(compiled)
    return CNMarketProfile(
        profile_format="tivelo-cn-profile-v2",
        fee_compilation=compiled,
        configuration=rules.configuration,
        calendar=rules.calendar.version,
    )


def test_archived_profile_retains_inputs_and_preserves_legacy_encoding():
    original = archived_profile()
    restored = CNMarketProfile.model_validate_json(original.model_dump_json())
    assert restored == original
    assert (
        restored.fee_compilation.inputs.policy.source_reference
        == "synthetic-policy-not-a-broker-quote"
    )
    assert restored.fee_compilation.inputs.preset_version == "2026.1.0"
    old = CNMarketProfile(configuration=original.configuration, calendar=original.calendar)
    assert set(old.model_dump()) == {"profile_format", "configuration", "calendar"}
    with pytest.raises(ValidationError, match="requires profile format v2"):
        CNMarketProfile.model_validate_json(
            original.model_copy(update={"profile_format": "tivelo-cn-profile-v1"}).model_dump_json()
        )
    with pytest.raises(ValidationError, match="requires the original fee compilation"):
        CNMarketProfile.model_validate_json(
            original.model_copy(update={"fee_compilation": None}).model_dump_json()
        )


@pytest.mark.parametrize(
    "part", ["policy", "compiled_charge", "configured_charge", "provenance", "venue", "asset"]
)
def test_archived_fee_inputs_cannot_disagree_with_executable_rules(part):
    original = archived_profile()
    raw = original.model_dump(mode="json")
    if part == "policy":
        raw["fee_compilation"]["inputs"]["policy"]["commission_rate"] = "0.0003"
    elif part == "compiled_charge":
        raw["fee_compilation"]["fee_rules"][0]["rate"] = "0.0003"
    elif part == "configured_charge":
        raw["configuration"]["fee_rules"][0]["rate"] = "0.0003"
    elif part == "provenance":
        raw["fee_compilation"]["selected_rate_ids"] = []
    elif part == "venue":
        raw["configuration"]["terms"][0]["venue_id"] = "XSHE"
    else:
        raw["configuration"]["terms"][0]["instrument_type"] = "preferred_stock"
    with pytest.raises(ValidationError):
        CNMarketProfile.model_validate(raw)


def test_offline_loader_requires_explicit_version_and_pinned_content(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    preset = load_fee_preset(version="2026.1.0")
    assert len(preset.contents.sources) == 12 and len(preset.contents.rates) == 9
    assert load_fee_preset(version="2026.1.0", expected_sha256=preset.version_id) == preset
    assert (
        next(
            item
            for item in preset.contents.rates
            if item.rate_id == "szse.regulatory.observed-2019"
        ).date_basis
        == "documented_in_force"
    )
    for version, digest, code in [
        ("latest", None, ErrorCode.UNSUPPORTED_CAPABILITY),
        ("../2026.1.0", None, ErrorCode.UNSUPPORTED_CAPABILITY),
        ("2026.1.0", "a" * 64, ErrorCode.ARTIFACT_CORRUPT),
    ]:
        with pytest.raises(TiveloError) as error:
            load_fee_preset(version=version, expected_sha256=digest)
        assert error.value.code == code


def test_damaged_or_resealed_installed_catalog_is_rejected(tmp_path, monkeypatch):
    from tivelo_market_cn import fee_presets

    directory = tmp_path / "fee_presets"
    directory.mkdir()
    target = directory / "2026.1.0.json"
    original = load_fee_preset(version="2026.1.0")
    monkeypatch.setattr(fee_presets.resources, "files", lambda name: tmp_path)
    for payload in [
        b"{",
        b"",
        FeePreset.create(original.contents.model_copy(update={"coverage_end": date(2025, 12, 31)}))
        .model_dump_json()
        .encode(),
    ]:
        target.write_bytes(payload)
        with pytest.raises(TiveloError) as error:
            load_fee_preset(version="2026.1.0")
        assert error.value.code == ErrorCode.ARTIFACT_CORRUPT
    target.unlink()
    with pytest.raises(TiveloError) as error:
        load_fee_preset(version="2026.1.0")
    assert error.value.code == ErrorCode.ARTIFACT_CORRUPT


def test_source_and_epoch_conflicts_are_rejected():
    contents = load_fee_preset(version="2026.1.0").contents
    for changes in [
        {"sources": contents.sources[1:]},
        {
            "rates": (
                *contents.rates,
                contents.rates[0].model_copy(update={"rate_id": "duplicate-epoch"}),
            )
        },
        {"corroborating_source_ids": ("missing",)},
    ]:
        with pytest.raises(ValidationError):
            FeePresetContents.model_validate_json(
                contents.model_copy(update=changes).model_dump_json()
            )
