import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_fee_presets import policy
from test_price_presets import at
from test_quantity_presets import classification
from test_rules import instrument
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import MarketAdapterSpec
from tivelo_contracts.market import ReferencePrice

from tivelo_market_cn.adapter import CNMarketProfile, create_rules
from tivelo_market_cn.portfolio import CNPortfolioProfile
from tivelo_market_cn.profile_presets import (
    CashEquityProfileRequest,
    compile_market_adapter,
    main,
)


@pytest.mark.parametrize("venue", ("XSHG", "XSHE"))
def test_compiled_profile_pins_prices_quantities_and_next_session_across_an_epoch(venue):
    warning = classification(venue, risk_status="risk_warning")
    normal = classification(venue)
    request = CashEquityProfileRequest(
        venue_id=venue,
        start=date(2026, 7, 3),
        end=date(2026, 7, 7),
        broker_policy=policy(includes=("handling", "regulatory")),
        instrument_classes=(warning, normal),
        calendar_source="preset",
    )
    adapter = compile_market_adapter(request)
    assert adapter == compile_market_adapter(
        request.model_copy(update={"instrument_classes": (normal, warning)})
    )
    profile = CNMarketProfile.model_validate_json(adapter.configuration_json)
    rules = create_rules(configuration_json=adapter.configuration_json)
    security = warning.bind(
        instrument(
            venue_id=venue,
            calendar_id=profile.calendar.contents.calendar_id,
        )
    )
    before = at("2026-07-03T10:00:00+08:00")
    after = at("2026-07-06T09:30:00+08:00")
    assert rules.trading_terms(security, at=before).limit_up_ratio == Decimal("0.05")
    assert rules.trading_terms(security, at=after).limit_up_ratio == Decimal("0.10")
    assert rules.sellable_at(security, before) == after
    assert {
        item.revision_id
        for item in rules.configuration.terms
        if item.spec_revision == security.spec_revision
    } == {rules.trading_terms(security, at=instant).revision_id for instant in (before, after)}
    assert rules.buy_quantity(security, Decimal(199), at=after, order_type="limit") == 100
    assert rules.fee_breakdown(
        security, "buy", Decimal(200), Decimal(10), at=after
    ).total == Decimal("5.02")
    with pytest.raises(TiveloError) as error:
        rules.trading_terms(security, at=after + timedelta(days=2))
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


def test_profile_cli_defaults_to_normal_main_board_and_fails_on_unknown_inputs(tmp_path, capsys):
    request = CashEquityProfileRequest(
        venue_id="XSHG",
        start=date(2025, 1, 3),
        end=date(2025, 1, 6),
        broker_policy=policy(),
        calendar_source="preset",
    )
    request_file = tmp_path / "request.json"
    request_file.write_text(request.model_dump_json(), encoding="utf-8")
    args = ["profile", "--request", str(request_file)]
    assert main(args) == 0
    adapter = MarketAdapterSpec.model_validate_json(capsys.readouterr().out)
    profile = CNMarketProfile.model_validate_json(adapter.configuration_json)
    assert profile.configuration.instrument_classes == (classification("XSHG"),)
    assert profile.profile_format == "tivelo-cn-profile-v3"
    assert main(["profile-schema"]) == 0
    assert "broker_policy" in json.loads(capsys.readouterr().out)["required"]
    request_file.write_text(
        request.model_copy(update={"preset_version": "latest"}).model_dump_json(),
        encoding="utf-8",
    )
    assert main(args) == 1
    captured = capsys.readouterr()
    assert not captured.out and json.loads(captured.err)["code"] == "RULES_UNAVAILABLE"

    future = request.model_copy(update={"start": date(2027, 1, 1), "end": date(2027, 1, 4)})
    with pytest.raises(TiveloError) as error:
        compile_market_adapter(future)
    assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
    with pytest.raises(ValidationError):
        CashEquityProfileRequest.model_validate(
            {**request.model_dump(), "instrument_classes": (classification("XSHE"),)}
        )
    late = request.model_copy(
        update={"broker_policy": policy(available_at=at("2025-01-03T10:00:00+08:00"))}
    )
    with pytest.raises(TiveloError) as error:
        compile_market_adapter(late)
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


def test_preset_portfolio_pins_all_venues_and_preserves_board_rules(tmp_path, capsys):
    request = CashEquityProfileRequest(
        start=date(2025, 1, 24),
        end=date(2025, 2, 6),
        broker_policy=policy(includes=("handling", "regulatory")),
        calendar_source="preset",
    )
    request_file = tmp_path / "portfolio.json"
    request_file.write_text(request.model_dump_json(exclude_none=True), encoding="utf-8")
    assert main(["profile", "--request", str(request_file)]) == 0
    adapter = MarketAdapterSpec.model_validate_json(capsys.readouterr().out)
    profile = CNPortfolioProfile.model_validate_json(adapter.configuration_json)
    assert profile.request.preset_version == "2026.2.0"
    assert len(profile.venue_rules) == 3
    assert {(item.venue_id, item.board) for item in profile.request.instrument_classes} == {
        ("XSHG", "main"),
        ("XSHG", "star"),
        ("XSHE", "main"),
        ("XSHE", "chinext"),
        ("XBSE", "beijing"),
    }
    assert compile_market_adapter(profile.request) == adapter
    rules = create_rules(configuration_json=adapter.configuration_json)
    days = rules.trading_days(request.start, request.end, as_of=at("2025-02-06T15:00:00+08:00"))
    assert [day.trading_date for day in days] == [
        date(2025, 1, 24),
        date(2025, 1, 27),
        date(2025, 2, 5),
        date(2025, 2, 6),
    ]
    opening = days[1].open_at
    for venue, board, minimum, rounded, price_limit in (
        ("XSHG", "main", 100, 200, "0.10"),
        ("XSHG", "star", 200, 201, "0.20"),
        ("XSHE", "main", 100, 200, "0.10"),
        ("XSHE", "chinext", 100, 200, "0.20"),
        ("XBSE", "beijing", 100, 201, "0.30"),
    ):
        security = classification(venue, board).bind(
            instrument(venue_id=venue, calendar_id=f"cn.equity.{venue.lower()}.regular-auction")
        )
        assert rules.supports(security)
        assert rules.matches_calendar(security, days[1])
        assert rules.quantity_terms(security, "limit", at=opening).buy_minimum == minimum
        assert rules.buy_quantity(security, Decimal(201), at=opening, order_type="limit") == rounded
        assert rules.trading_terms(security, at=opening).limit_up_ratio == Decimal(price_limit)
        assert rules.sellable_at(security, opening) == days[2].open_at
        assert rules.fees(security, "buy", Decimal(200), Decimal(10), at=opening) == Decimal("5.02")
    assert rules.next_open(days[1].trading_date, as_of=opening) == days[2].open_at
    assert not rules.supports(security.model_copy(update={"venue_id": "XNAS"}))
    assert not rules.matches_calendar(security.model_copy(update={"calendar_id": "wrong"}), days[1])

    first, *others = profile.venue_rules
    changed = profile.model_copy(
        update={"venue_rules": (first.model_copy(update={"sha256": "0" * 64}), *others)}
    )
    with pytest.raises(TiveloError) as error:
        create_rules(configuration_json=changed.model_dump_json())
    assert error.value.code == ErrorCode.DATA_CONFLICT


def test_explicit_old_portfolio_keeps_previous_venue_scope():
    adapter = compile_market_adapter(
        CashEquityProfileRequest(
            start=date(2025, 1, 2),
            end=date(2025, 1, 6),
            broker_policy=policy(),
            preset_version="2026.1.0",
            calendar_source="preset",
        )
    )
    profile = CNPortfolioProfile.model_validate_json(adapter.configuration_json)
    assert {item.venue_id for item in profile.request.instrument_classes} == {"XSHG", "XSHE"}
    assert compile_market_adapter(profile.request) == adapter


def test_beijing_price_and_quantity_rules_across_july_release():
    normal = classification("XBSE", "beijing")
    debut = classification("XBSE", "beijing", listing_phase="ipo_first_day")
    adapter = compile_market_adapter(
        CashEquityProfileRequest(
            venue_id="XBSE",
            start=date(2026, 7, 3),
            end=date(2026, 7, 7),
            broker_policy=policy(),
            instrument_classes=(normal, debut),
            calendar_source="preset",
        )
    )
    rules = create_rules(configuration_json=adapter.configuration_json)
    recorded = instrument(venue_id="XBSE", calendar_id="cn.equity.xbse.regular-auction")
    stock = normal.bind(recorded)
    ipo = debut.bind(recorded)
    revisions = set()
    for day in (3, 6):
        opening = at(f"2026-07-0{day}T09:30:00+08:00")
        reference = ReferencePrice(
            instrument_id=stock.instrument_id,
            trading_date=date(2026, 7, day),
            value="16.76",
            kind="previous_close",
            available_at=opening,
            source_id="fixture",
            revision_id="1",
        )
        bounds = rules.price_bounds(stock, reference, at=opening)
        assert (bounds.lower, bounds.upper) == (Decimal("11.74"), Decimal("21.78"))
        revisions.add(bounds.rule_revision)
        assert rules.price_bounds(ipo, None, at=opening).state == "unlimited"
        with pytest.raises(TiveloError) as error:
            rules.quantity_terms(ipo, "market", at=opening)
        assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
        assert (
            rules.sell_quantity(
                stock, Decimal(50), available=Decimal(55), at=opening, order_type="limit"
            )
            == 0
        )
        assert (
            rules.sell_quantity(
                stock, Decimal(55), available=Decimal(55), at=opening, order_type="limit"
            )
            == 55
        )
        buy = rules.fee_breakdown(stock, "buy", Decimal(200), Decimal(10), at=opening)
        sell = rules.fee_breakdown(stock, "sell", Decimal(200), Decimal(10), at=opening)
        assert {c.fee_id: c.amount for c in buy.components} == {
            "commission": Decimal("5"),
            "handling": Decimal("0.25"),
            "regulatory": Decimal("0.04"),
            "transfer": Decimal("0.02"),
        }
        assert (buy.total, sell.total) == (Decimal("5.31"), Decimal("6.31"))
    assert len(revisions) == 2


@pytest.mark.parametrize("venue_id", [None, "XBSE"])
def test_default_profile_uses_dates_from_bars_without_calendar_lookup(monkeypatch, venue_id):
    def unexpected_calendar(**kwargs):
        pytest.fail("bar-driven profile cannot load an exchange-calendar preset")

    monkeypatch.setattr(
        "tivelo_market_cn.profile_presets.load_calendar_preset", unexpected_calendar
    )
    start, end = date(2025, 1, 2), date(2025, 1, 6)
    request = CashEquityProfileRequest(
        start=start, end=end, broker_policy=policy(), venue_id=venue_id
    )
    profile = create_rules(configuration_json=compile_market_adapter(request).configuration_json)
    assert profile.uses_bars
    expected_venues = {"XBSE", "XSHG", "XSHE"} if venue_id is None else {venue_id}
    assert {item.venue_id for item in profile.request.instrument_classes} == expected_venues
    convention = "fixture.daily"
    scope = SimpleNamespace(
        market_id="cn.equity",
        bar_convention_id=convention,
        timeframes=("1d",),
        instrument_ids=("B",),
        start=at("2025-01-01T00:00:00+08:00"),
        end=at("2025-01-07T00:00:00+08:00"),
    )
    reader = SimpleNamespace(
        manifest=SimpleNamespace(snapshot_id="a" * 64),
        datasets=(
            SimpleNamespace(
                contents=SimpleNamespace(record_type="bar", price_basis="raw", scope=scope)
            ),
        ),
        bars=lambda query: tuple(
            SimpleNamespace(
                trading_date=date(2025, 1, day),
                bar_start=at(f"2025-01-0{day}T09:30:00+08:00"),
                bar_end=at(f"2025-01-0{day}T15:00:00+08:00"),
            )
            for day in (2, 6)
        ),
    )
    rules = profile.bind_daily_snapshot(reader, universe=("B",), start=start, end=end)
    days = rules.trading_days(start, end, as_of=scope.end)
    assert [day.trading_date for day in days] == [start, end]
    assert [day.bar_convention_id for day in days] == [convention, convention]
    security = classification("XBSE", "beijing").bind(
        instrument(venue_id="XBSE", calendar_id="cn.equity.xbse.regular-auction")
    )
    assert rules.sellable_at(security, days[0].open_at) == days[-1].open_at
    assert rules.sellable_at(security, days[-1].open_at) > scope.end
