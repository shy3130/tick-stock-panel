from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_market_cn.classification import CNEquityClass
from tivelo_market_cn.quantity_presets import (
    QuantityPreset,
    QuantityPresetContents,
    load_quantity_preset,
    resolve_quantity_rules,
)


def at(value="2025-01-03"):
    return datetime.fromisoformat(value).replace(hour=2, tzinfo=UTC)


def classification(venue="XSHG", board="main", **changes):
    return CNEquityClass(
        **dict(venue_id=venue, board=board, risk_status="normal", listing_phase="regular") | changes
    )


@pytest.mark.parametrize("year", [2024, 2025, 2026])
@pytest.mark.parametrize(
    "venue,board,minimum,step,limit,market,odd_lots",
    [
        ("XSHG", "main", 100, 100, 1000000, 1000000, "remainder_once"),
        ("XSHE", "main", 100, 100, 1000000, 1000000, "remainder_once"),
        ("XSHG", "star", 200, 1, 100000, 50000, "all_remaining"),
        ("XSHE", "chinext", 100, 100, 300000, 150000, "remainder_once"),
    ],
)
def test_offline_board_and_order_type_quantities(
    year, venue, board, minimum, step, limit, market, odd_lots
):
    preset = load_quantity_preset(version="2026.1.0")
    selected = resolve_quantity_rules(preset, classification(venue, board), at=at(f"{year}-08-03"))
    assert selected.scope == "continuous_auction_quantity_and_type"
    assert [q.order_type for q in selected.order_quantities] == ["limit", "market"]
    for q in selected.order_quantities:
        assert q.buy_minimum == q.sell_minimum == minimum
        assert q.buy_increment == q.sell_increment == step
        assert q.buy_maximum == q.sell_maximum == (market if q.order_type == "market" else limit)
        assert q.odd_lot_policy == odd_lots
    assert selected.preset_version_id == preset.version_id


@pytest.mark.parametrize(
    "venue,board,state,phase,expected",
    [
        ("XSHG", "main", "risk_warning", "regular", ("limit",)),
        ("XSHG", "main", "normal", "delisting", ("limit",)),
        ("XSHG", "main", "normal", "delisting_first", ("limit",)),
        ("XSHG", "main", "normal", "ipo_first_five", ("limit", "market")),
        ("XSHG", "star", "risk_warning", "regular", ("limit", "market")),
        ("XSHE", "main", "risk_warning", "regular", ("limit", "market")),
        ("XSHE", "main", "normal", "relisting_first", ("limit",)),
        ("XSHE", "chinext", "normal", "ipo_first_five", ("limit",)),
        ("XSHE", "chinext", "risk_warning", "delisting_first", ("limit",)),
    ],
)
def test_class_specific_type_restrictions(venue, board, state, phase, expected):
    result = resolve_quantity_rules(
        load_quantity_preset(version="2026.1.0"),
        classification(venue, board, risk_status=state, listing_phase=phase),
        at=at(),
    )
    assert tuple(q.order_type for q in result.order_quantities) == expected


@pytest.mark.parametrize(
    "day,epoch",
    [
        ("2026-04-24", "2023"),
        ("2026-04-25", "2023"),
        ("2026-07-05", "2023"),
        ("2026-07-06", "2026"),
    ],
)
def test_rule_announcement_does_not_advance_effective_epoch(day, epoch):
    result = resolve_quantity_rules(
        load_quantity_preset(version="2026.1.0"), classification(), at=at(day)
    )
    assert result.rule_id == f"sse.main.quantities.{epoch}"
    assert result.available_at <= at(day)


@pytest.mark.parametrize("value", ["2023-12-31", "2027-01-01"])
def test_dates_outside_explicit_coverage_are_rejected(value):
    with pytest.raises(TiveloError) as error:
        resolve_quantity_rules(
            load_quantity_preset(version="2026.1.0"), classification(), at=at(value)
        )
    assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT


def test_uncertified_class_does_not_inherit_a_different_board_policy():
    with pytest.raises(TiveloError) as error:
        resolve_quantity_rules(
            load_quantity_preset(version="2026.1.0"),
            classification(board="star", listing_phase="relisting_first"),
            at=at(),
        )
    assert error.value.code == ErrorCode.UNSUPPORTED_ASSET


def test_preset_loading_is_offline_and_explicitly_pinned(monkeypatch):
    import socket

    def network(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "socket", network)
    first = load_quantity_preset(version="2026.1.0")
    assert load_quantity_preset(version="2026.1.0", expected_sha256=first.version_id) == first
    assert len(first.contents.sources) == 11 and len(first.contents.schedules) == 8
    for version, digest, code in (
        ("latest", None, ErrorCode.UNSUPPORTED_CAPABILITY),
        ("../2026.1.0", None, ErrorCode.UNSUPPORTED_CAPABILITY),
        ("2026.1.0", "a" * 64, ErrorCode.ARTIFACT_CORRUPT),
    ):
        with pytest.raises(TiveloError) as error:
            load_quantity_preset(version=version, expected_sha256=digest)
        assert error.value.code == code


@pytest.mark.parametrize("failure", ["missing", "bad_json", "replaced"])
def test_installed_resources_cannot_be_missing_or_replaced(monkeypatch, tmp_path, failure):
    from tivelo_market_cn import quantity_presets

    original = load_quantity_preset(version="2026.1.0")
    directory = tmp_path / "quantity_presets"
    directory.mkdir()
    if failure != "missing":
        if failure == "bad_json":
            payload = "{"
        else:
            contents = original.contents.model_copy(update={"verified_on": at().date()})
            payload = QuantityPreset.create(contents).model_dump_json()
        (directory / "2026.1.0.json").write_text(payload)
    monkeypatch.setattr(quantity_presets.resources, "files", lambda package: tmp_path)
    with pytest.raises(TiveloError) as error:
        load_quantity_preset(version="2026.1.0")
    assert error.value.code == ErrorCode.ARTIFACT_CORRUPT


@pytest.mark.parametrize("change", ["source", "duplicate", "types"])
def test_incomplete_or_ambiguous_catalog_is_rejected(change):
    value = load_quantity_preset(version="2026.1.0").contents.model_dump(mode="json")
    if change == "source":
        value["schedules"][0]["source_ids"] = ["missing"]
    elif change == "duplicate":
        value["schedules"].append(value["schedules"][0])
    else:
        value["schedules"][0]["order_quantities"].reverse()
    with pytest.raises(ValidationError):
        QuantityPresetContents.model_validate(value)


def test_source_backed_quantities_drive_existing_installed_market_factory():
    from importlib.metadata import entry_points

    from test_rules import at as synthetic_at
    from test_rules import calendar, fee, instrument, terms

    from tivelo_market_cn.adapter import CNMarketProfile
    from tivelo_market_cn.rules import RuleConfiguration

    state = classification()
    selected = resolve_quantity_rules(load_quantity_preset(version="2026.1.0"), state, at=at())
    configuration = RuleConfiguration(
        schema_version="1.3",
        profile_id="fixture.quantities",
        profile_version="1",
        currency="CNY",
        terms=(
            terms(
                spec_revision=state.spec_revision, order_quantity_rules=selected.order_quantities
            ),
        ),
        instrument_classes=(state,),
        required_fee_ids=("commission",),
        fee_rules=(fee(),),
    )
    profile = CNMarketProfile(configuration=configuration, calendar=calendar().version)
    (entry,) = entry_points(group="tivelo.market_rules.v1", name="tivelo.market.cn.cash-equity")
    rules = entry.load()(configuration_json=profile.model_dump_json())
    assert (
        rules.order_quantity(
            state.bind(instrument()),
            "sell",
            Decimal(199),
            order_type="market",
            available=Decimal(299),
            at=synthetic_at(3),
        )
        == 199
    )
