import json
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_market_cn.calendar import CalendarContents, CalendarDay, CalendarVersion, CNCalendar

AS_OF = datetime(2025, 1, 3, tzinfo=UTC)


def open_day(day):
    return CalendarDay(
        trading_date=day,
        is_open=True,
        sessions=(
            dict(
                session_id="fixture.am",
                kind="continuous",
                start=f"{day}T01:30:00Z",
                end=f"{day}T03:30:00Z",
            ),
            dict(
                session_id="fixture.pm",
                kind="continuous",
                start=f"{day}T05:00:00Z",
                end=f"{day}T07:00:00Z",
            ),
        ),
    )


@pytest.fixture
def version():
    # The Monday closure is fictional, deliberately proving that weekdays alone
    # cannot drive the calendar. This is not an exchange holiday fixture.
    days = tuple(
        CalendarDay(
            trading_date=date(2025, 1, 3) + timedelta(days=i),
            is_open=False,
            closed_reason="synthetic.closure",
        )
        if i in (1, 2, 3)
        else open_day(date(2025, 1, 3) + timedelta(days=i))
        for i in range(6)
    )
    return CalendarVersion.create(
        CalendarContents(
            calendar_id="fixture.cn",
            venue_id="XSHG",
            bar_convention_id="fixture.continuous.v1",
            source_id="synthetic",
            source_sha256="a" * 64,
            available_at="2024-12-01T00:00:00Z",
            days=days,
        )
    )


def test_regular_sessions_use_published_stock_schedule_and_declared_closures():
    from tivelo_market_cn.presets import compile_calendar, load_calendar_preset

    trading, closed = date(2025, 7, 4), date(2025, 7, 5)
    calendar = CNCalendar(
        compile_calendar(
            load_calendar_preset(version="2026.1.0"), venue_id="XSHG", start=trading, end=closed
        )
    )
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    regular = calendar.regular_sessions(trading, as_of=observed)
    assert [s.session_id for s in regular] == ["morning", "afternoon", "closing-auction"]
    assert sum((s.end - s.start for s in regular), timedelta()) == timedelta(minutes=240)
    assert calendar.regular_sessions(closed, as_of=observed) == ()
    with pytest.raises(TiveloError) as error:
        calendar.regular_sessions(trading, as_of=datetime(2000, 1, 1, tzinfo=UTC))
    assert error.value.code == ErrorCode.FUTURE_INFORMATION


def test_next_trading_open_uses_declared_closures_instead_of_natural_days(version):
    calendar = CNCalendar(version)
    assert calendar.is_open(date(2025, 1, 3), as_of=AS_OF)
    assert not calendar.is_open(date(2025, 1, 6), as_of=AS_OF)
    assert calendar.next_trading_open(date(2025, 1, 3), as_of=AS_OF) == datetime(
        2025, 1, 7, 1, 30, tzinfo=UTC
    )
    assert calendar.next_trading_open(date(2025, 1, 7), as_of=AS_OF) == datetime(
        2025, 1, 8, 1, 30, tzinfo=UTC
    )
    assert calendar.daily_bounds(date(2025, 1, 7), as_of=AS_OF) == (
        datetime(2025, 1, 7, 1, 30, tzinfo=UTC),
        datetime(2025, 1, 7, 7, tzinfo=UTC),
        "fixture.continuous.v1",
    )
    days = calendar.trading_days(date(2025, 1, 3), date(2025, 1, 8), as_of=AS_OF)
    assert tuple(day.trading_date.day for day in days) == (3, 7, 8)
    assert days[0].open_at == datetime(2025, 1, 3, 1, 30, tzinfo=UTC)
    assert days[0].close_at == datetime(2025, 1, 3, 7, tzinfo=UTC)


@pytest.mark.parametrize(
    "time,expected",
    [
        ("01:29:59", None),
        ("01:30:00", "fixture.am"),
        ("03:29:59", "fixture.am"),
        ("03:30:00", None),
        ("04:00:00", None),
        ("05:00:00", "fixture.pm"),
        ("07:00:00", None),
    ],
)
def test_half_open_sessions_and_lunch_gap(version, time, expected):
    session = CNCalendar(version).session_at(
        datetime.fromisoformat(f"2025-01-03T{time}+00:00"), as_of=AS_OF
    )
    assert (session.session_id if session else None) == expected


def test_unknown_date_and_unknown_next_session_fail_without_guessing(version):
    calendar = CNCalendar(version)
    with pytest.raises(TiveloError) as raised:
        calendar.is_open(date(2025, 1, 2), as_of=AS_OF)
    assert raised.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
    with pytest.raises(TiveloError) as raised:
        calendar.next_trading_open(date(2025, 1, 8), as_of=AS_OF)
    assert raised.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
    with pytest.raises(TiveloError) as raised:
        calendar.daily_bounds(date(2025, 1, 6), as_of=AS_OF)
    assert raised.value.code == ErrorCode.UNSUPPORTED_CAPABILITY


def test_unpublished_calendar_is_future_information(version):
    calendar = CNCalendar(version)
    with pytest.raises(TiveloError) as raised:
        calendar.is_open(date(2025, 1, 3), as_of=datetime(2024, 11, 1, tzinfo=UTC))
    assert raised.value.code == ErrorCode.FUTURE_INFORMATION
    with pytest.raises(ValueError):
        calendar.session_at(datetime(2025, 1, 3, 10), as_of=AS_OF)


def test_later_holiday_publication_is_checked_on_every_intervening_date(version):
    values = version.contents.model_dump()
    values["days"][3]["available_at"] = "2025-01-06T00:00:00Z"
    calendar = CNCalendar(CalendarVersion.create(CalendarContents(**values)))
    assert calendar.is_open(date(2025, 1, 3), as_of=AS_OF)
    with pytest.raises(TiveloError) as error:
        calendar.next_trading_open(date(2025, 1, 3), as_of=AS_OF)
    assert error.value.code == ErrorCode.FUTURE_INFORMATION
    assert calendar.next_trading_open(
        date(2025, 1, 3), as_of=datetime(2025, 1, 6, tzinfo=UTC)
    ) == datetime(2025, 1, 7, 1, 30, tzinfo=UTC)


def test_daily_execution_clock_includes_the_closing_auction():
    day = CalendarDay(
        trading_date="2025-01-03",
        is_open=True,
        sessions=(
            dict(
                session_id="opening",
                kind="auction",
                start="2025-01-03T01:15:00Z",
                end="2025-01-03T01:25:00Z",
            ),
            dict(
                session_id="continuous",
                kind="continuous",
                start="2025-01-03T01:30:00Z",
                end="2025-01-03T06:57:00Z",
            ),
            dict(
                session_id="closing",
                kind="auction",
                start="2025-01-03T06:57:00Z",
                end="2025-01-03T07:00:00Z",
            ),
        ),
    )
    calendar = CNCalendar(
        CalendarVersion.create(
            CalendarContents(
                calendar_id="fixture",
                venue_id="XSHG",
                bar_convention_id="fixture.auction-inclusive",
                source_id="synthetic",
                source_sha256="a" * 64,
                available_at="2025-01-01T00:00:00Z",
                days=(day,),
            )
        )
    )
    (result,) = calendar.trading_days(date(2025, 1, 3), date(2025, 1, 3), as_of=AS_OF)
    assert result.open_at == datetime(2025, 1, 3, 1, 30, tzinfo=UTC)
    assert result.close_at == datetime(2025, 1, 3, 7, tzinfo=UTC)


def test_calendar_hash_covers_closures_and_no_missing_dates_are_allowed(version):
    value = json.loads(canonical_bytes(version))
    value["contents"]["days"][1]["closed_reason"] = "modified"
    with pytest.raises(ValidationError, match="fingerprint"):
        CalendarVersion.model_validate(value)
    value = version.contents.model_dump()
    value["days"] = (*value["days"][:2], *value["days"][3:])
    with pytest.raises(ValidationError, match="contiguous"):
        CalendarContents.model_validate(value)


@pytest.mark.parametrize(
    "change", ["overlap", "unsorted", "wrong_date", "no_continuous", "duplicate_id", "empty"]
)
def test_invalid_session_definitions_are_rejected(change):
    value = open_day(date(2025, 1, 3)).model_dump(mode="json")
    if change == "overlap":
        value["sessions"][1]["start"] = "2025-01-03T02:30:00Z"
    elif change == "unsorted":
        value["sessions"].reverse()
    elif change == "wrong_date":
        value["sessions"][1]["end"] = "2025-01-04T07:00:00Z"
    elif change == "no_continuous":
        for session in value["sessions"]:
            session["kind"] = "auction"
    elif change == "duplicate_id":
        value["sessions"][1]["session_id"] = value["sessions"][0]["session_id"]
    else:
        value["sessions"] = []
    with pytest.raises(ValidationError):
        CalendarDay.model_validate(value)
