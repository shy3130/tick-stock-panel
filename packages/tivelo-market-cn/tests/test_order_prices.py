from datetime import timedelta
from decimal import Decimal
from importlib import metadata

import pytest
from pydantic import ValidationError
from test_fee_presets import compile as compile_fees
from test_price_presets import at, base
from test_quantity_presets import classification
from test_rules import instrument
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.market import ReferencePrice

from tivelo_market_cn.adapter import CNMarketProfile, create_rules
from tivelo_market_cn.calendar import CN_TIMEZONE, CalendarVersion
from tivelo_market_cn.order_prices import ContinuousBook, continuous_limit_reference
from tivelo_market_cn.presets import compile_calendar, load_calendar_preset
from tivelo_market_cn.price_presets import compile_price_terms, load_price_preset
from tivelo_market_cn.rules import RuleConfiguration


def limit_fixture(
    venue="XSHG",
    board="main",
    moment="2025-01-03T10:00:00+08:00",
    phase="regular",
    risk="normal",
):
    instant = at(moment)
    day = instant.astimezone(CN_TIMEZONE).date()
    state = classification(venue, board, listing_phase=phase, risk_status=risk)
    prices = compile_price_terms(load_price_preset(version="2026.1.0"), state, (base(state),))
    fees = compile_fees(venue=venue, start=day.isoformat())
    calendar = compile_calendar(
        load_calendar_preset(version="2026.1.0"),
        venue_id=venue,
        start=day,
        end=day,
    )
    profile = CNMarketProfile(
        profile_format="tivelo-cn-profile-v4",
        calendar=calendar,
        fee_compilation=fees,
        price_compilations=(prices,),
        continuous_limit=continuous_limit_reference(version="2026.1.0"),
        configuration=RuleConfiguration(
            schema_version="1.2",
            profile_id="synthetic-continuous-auction",
            profile_version="1",
            currency="CNY",
            terms=prices.terms,
            instrument_classes=(state,),
            required_fee_ids=fees.required_fee_ids,
            fee_rules=fees.fee_rules,
        ),
    )
    security = state.bind(
        instrument(
            venue_id=venue,
            calendar_id=calendar.contents.calendar_id,
            available_at=instant - timedelta(days=1),
            valid_from=instant - timedelta(days=1),
            listing_date={
                "state": "known",
                "value": day if phase == "ipo_first_five" else "2024-01-01",
            },
        )
    )
    (session,) = [s for s in calendar.contents.days[0].sessions if s.start <= instant < s.end]
    book = ContinuousBook(
        instrument_id=security.instrument_id,
        session_id=session.session_id,
        at=instant,
        available_at=instant,
        source_id="test-book",
        revision_id="book-3",
        price_basis="raw",
        currency="CNY",
        state="continuous",
        best_bid="1.99",
        best_ask="2.01",
        last_trade="2",
        last_trade_at=instant - timedelta(seconds=2),
    )
    reference = ReferencePrice(
        instrument_id=security.instrument_id,
        trading_date=day,
        value="2",
        kind="exchange_ex_rights",
        available_at=instant - timedelta(hours=1),
        source_id="test-exchange",
        revision_id="open-4",
    )
    return profile, security, book, reference


@pytest.mark.parametrize(
    "venue,board,buy,sell",
    [
        ("XSHG", "main", "2.11", "1.89"),
        ("XSHE", "main", "2.11", "1.89"),
        ("XSHE", "chinext", "2.11", "1.89"),
        ("XSHG", "star", "2.05", "1.95"),
    ],
)
@pytest.mark.parametrize("day,epoch", [("2025-01-03", "2023"), ("2026-07-06", "2026")])
def test_board_and_epoch_limits_via_installed_factory(venue, board, buy, sell, day, epoch):
    profile, security, book, reference = limit_fixture(venue, board, f"{day}T10:00:00+08:00")
    (entry,) = metadata.entry_points(
        group="tivelo.market_rules.v1", name="tivelo.market.cn.cash-equity"
    )
    rules = entry.load()(configuration_json=profile.model_dump_json())
    for side, boundary, basis in [("buy", buy, "best_ask"), ("sell", sell, "best_bid")]:
        boundary = Decimal(boundary)
        result = rules.check_limit_price(
            security, side, boundary, at=book.at, book=book, reference=reference
        )
        assert result.result == "price_valid" and result.cage_boundary == boundary
        assert result.basis_kind == basis and result.daily_bounds.reference_revision == "open-4"
        assert result.book_revision == "book-3" and result.policy == profile.continuous_limit
        assert all(epoch in clause for clause in result.clauses)
        more_aggressive = boundary + (Decimal("0.01") if side == "buy" else Decimal("-0.01"))
        assert (
            rules.check_limit_price(
                security,
                side,
                more_aggressive,
                at=book.at,
                book=book,
                reference=reference,
            ).result
            == "outside_price_cage"
        )


@pytest.mark.parametrize(
    "inputs,error",
    [
        ({}, ErrorCode.RULES_UNAVAILABLE),
        ({"risk": "risk_warning"}, ErrorCode.UNSUPPORTED_CAPABILITY),
        ({"board": "star"}, ErrorCode.UNSUPPORTED_CAPABILITY),
        ({"venue": "XSHE", "board": "chinext"}, ErrorCode.UNSUPPORTED_CAPABILITY),
    ],
)
def test_cash_admission_requires_official_quantity_and_supported_account_scope(inputs, error):
    profile, security, book, _ = limit_fixture(**inputs)
    rules = create_rules(configuration_json=profile.model_dump_json())
    with pytest.raises(TiveloError) as denied:
        rules.cash_limit_deadline(security, "buy", at=book.at)
    assert denied.value.code == error


@pytest.mark.parametrize(
    "side,missing,expected",
    [
        ("buy", ("best_ask",), "best_bid"),
        ("sell", ("best_bid",), "best_ask"),
        ("buy", ("best_ask", "best_bid"), "last_trade"),
        ("sell", ("best_ask", "best_bid", "last_trade", "last_trade_at"), "session_reference"),
    ],
)
def test_basis_precedence_uses_only_explicitly_empty_levels(side, missing, expected):
    profile, security, book, reference = limit_fixture()
    book = book.model_copy(update=dict.fromkeys(missing))
    result = create_rules(configuration_json=profile.model_dump_json()).check_limit_price(
        security,
        side,
        Decimal(2),
        at=book.at,
        book=book,
        reference=reference,
    )
    assert result.basis_kind == expected
    assert result.session_reference_revision == (
        "open-4" if expected == "session_reference" else None
    )


def test_high_low_rounding_unlimited_sessions_and_daily_price_limits():
    profile, security, book, reference = limit_fixture()
    rules = create_rules(configuration_json=profile.model_dump_json())
    high = book.model_copy(
        update={
            "best_bid": Decimal("9.99"),
            "best_ask": Decimal("10"),
            "last_trade": Decimal("9.99"),
        }
    )
    high_reference = reference.model_copy(update={"value": Decimal(10)})
    result = rules.check_limit_price(
        security, "buy", Decimal("10.20"), at=high.at, book=high, reference=high_reference
    )
    assert result.cage_boundary == Decimal("10.20") and result.result == "price_valid"
    assert (
        rules.check_limit_price(
            security,
            "buy",
            Decimal("2.201"),
            at=book.at,
            book=book,
            reference=reference,
        ).result
        == "invalid_limit_tick"
    )
    assert (
        rules.check_limit_price(
            security,
            "buy",
            Decimal("2.21"),
            at=book.at,
            book=book,
            reference=reference,
        ).result
        == "outside_daily_limits"
    )

    profile, security, book, _ = limit_fixture(board="star", phase="ipo_first_five")
    rules = create_rules(configuration_json=profile.model_dump_json())
    low = book.model_copy(
        update={
            "best_ask": Decimal("0.20"),
            "best_bid": Decimal("0.19"),
            "last_trade": None,
            "last_trade_at": None,
        }
    )
    check = rules.check_limit_price(security, "buy", Decimal("0.22"), at=low.at, book=low)
    assert check.daily_bounds.state == "unlimited"
    assert check.cage_boundary == Decimal("0.21") and check.result == "outside_price_cage"

    no_bid = low.model_copy(update={"best_ask": Decimal("0.01"), "best_bid": None})
    check = rules.check_limit_price(security, "sell", Decimal("0.01"), at=no_bid.at, book=no_bid)
    assert check.cage_boundary == Decimal("0.01") and check.result == "price_valid"
    empty = no_bid.model_copy(update={"best_ask": None})
    with pytest.raises(TiveloError) as error:
        rules.check_limit_price(security, "buy", Decimal("0.01"), at=empty.at, book=empty)
    assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT


def test_absent_incomplete_stale_or_unreliable_book_cannot_approve():
    profile, security, book, reference = limit_fixture()
    rules = create_rules(configuration_json=profile.model_dump_json())
    cases = (
        (None, ErrorCode.DATA_COVERAGE_INSUFFICIENT),
        ({"at": book.at - timedelta(seconds=1)}, ErrorCode.DATA_COVERAGE_INSUFFICIENT),
        ({"at": book.at + timedelta(seconds=1)}, ErrorCode.FUTURE_INFORMATION),
        ({"available_at": book.at + timedelta(microseconds=1)}, ErrorCode.FUTURE_INFORMATION),
        ({"session_id": "other"}, ErrorCode.DATA_CONFLICT),
        ({"instrument_id": "other"}, ErrorCode.DATA_CONFLICT),
        ({"state": "halted"}, ErrorCode.UNSUPPORTED_CAPABILITY),
        ({"best_ask": Decimal("1.99")}, ErrorCode.CONTRACT_INVALID),
        ({"best_bid": Decimal("1.993")}, ErrorCode.CONTRACT_INVALID),
        ({"best_bid": Decimal("1.65")}, ErrorCode.DATA_CONFLICT),
        ({"price_basis": "qfq"}, ErrorCode.CONTRACT_INVALID),
        ({"last_trade_at": book.at - timedelta(days=1)}, ErrorCode.CONTRACT_INVALID),
        ({"last_trade_at": book.at + timedelta(microseconds=1)}, ErrorCode.CONTRACT_INVALID),
    )
    for changes, code in cases:
        candidate = book.model_copy(update=changes) if changes is not None else None
        with pytest.raises(TiveloError) as error:
            rules.check_limit_price(
                security, "buy", Decimal(2), at=book.at, book=candidate, reference=reference
            )
        assert error.value.code == code

    for local_time in ("09:29:59", "11:30:00", "14:57:00"):
        with pytest.raises(TiveloError) as error:
            rules.check_limit_price(
                security,
                "buy",
                Decimal(2),
                at=at(f"2025-01-03T{local_time}+08:00"),
                book=book,
                reference=reference,
            )
        assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY

    with pytest.raises(ValidationError):
        ContinuousBook.model_validate(book.model_dump(exclude={"last_trade"}))


def test_profile_pins_calendar_and_rules_without_changing_prior_profiles():
    profile, _, _, _ = limit_fixture()
    assert CNMarketProfile.model_validate_json(canonical_bytes(profile)) == profile
    payload = profile.model_dump(mode="json")
    payload["profile_format"] = "tivelo-cn-profile-v3"
    with pytest.raises(ValidationError):
        CNMarketProfile.model_validate(payload)
    payload.pop("continuous_limit")
    prior = CNMarketProfile.model_validate(payload)
    assert "continuous_limit" not in prior.model_dump()
    old_rules = create_rules(configuration_json=prior.model_dump_json())
    new_rules = create_rules(configuration_json=profile.model_dump_json())
    assert new_rules.reference != old_rules.reference
    assert new_rules.requires_order_book is True

    for changes in (
        {"continuous_limit": profile.continuous_limit.model_copy(update={"sha256": "0" * 64})},
        {
            "calendar": CalendarVersion.create(
                profile.calendar.contents.model_copy(update={"source_id": "other"})
            )
        },
    ):
        with pytest.raises(TiveloError) as error:
            create_rules(configuration_json=profile.model_copy(update=changes).model_dump_json())
        assert error.value.code == ErrorCode.DATA_CONFLICT


def test_observed_book_age_is_pinned_and_does_not_cross_continuous_sessions():
    profile, security, book, reference = limit_fixture(moment="2025-01-03T13:00:00+08:00")
    payload = profile.model_dump(mode="json")
    exact = create_rules(configuration_json=profile.model_dump_json())
    assert exact.reference.parameters_json == "{}"
    with pytest.raises(ValidationError):
        CNMarketProfile.model_validate({**payload, "observed_book_max_age_ms": 500})

    payload["profile_format"] = "tivelo-cn-profile-v5"
    with pytest.raises(ValidationError):
        CNMarketProfile.model_validate(payload)
    for bad_age in (0, True, "500", 86_400_001):
        with pytest.raises(ValidationError):
            CNMarketProfile.model_validate({**payload, "observed_book_max_age_ms": bad_age})
    bounded = CNMarketProfile.model_validate({**payload, "observed_book_max_age_ms": 500})
    rules = create_rules(configuration_json=bounded.model_dump_json())
    assert rules.reference.parameters_json == '{"observed_book_max_age_ms":500}'
    assert rules.reference.sha256 != exact.reference.sha256

    submitted = book.at + timedelta(milliseconds=500)
    observed = book.model_copy(update={"available_at": submitted})
    receipt = rules.check_limit_price(
        security, "buy", Decimal(2), at=submitted, book=observed, reference=reference
    )
    assert (receipt.at, receipt.book_event_at, receipt.book_available_at) == (
        submitted,
        book.at,
        submitted,
    )
    previous_session = observed.model_copy(
        update={
            "at": book.at - timedelta(hours=2),
            "last_trade": None,
            "last_trade_at": None,
        }
    )
    for engine, when, sample, code in (
        (exact, submitted, observed, ErrorCode.DATA_COVERAGE_INSUFFICIENT),
        (rules, submitted - timedelta(microseconds=1), observed, ErrorCode.FUTURE_INFORMATION),
        (
            rules,
            submitted + timedelta(microseconds=1),
            observed,
            ErrorCode.DATA_COVERAGE_INSUFFICIENT,
        ),
        (rules, submitted, previous_session, ErrorCode.DATA_CONFLICT),
    ):
        with pytest.raises(TiveloError) as error:
            engine.check_limit_price(
                security, "buy", Decimal(2), at=when, book=sample, reference=reference
            )
        assert error.value.code == code
