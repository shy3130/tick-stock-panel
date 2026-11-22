"""Build the daily execution dates from the locked raw bars."""

from datetime import datetime, time, timedelta

from tivelo_contracts.data import BarQuery
from tivelo_contracts.errors import ErrorCode, TiveloError

from .calendar import CN_TIMEZONE, CalendarContents, CalendarDay, CalendarVersion, SessionInterval


def read_bar_calendar(reader, *, universe, start, end) -> CalendarVersion:
    first = datetime.combine(start, time.min, CN_TIMEZONE)
    cutoff = datetime.combine(end, time.max, CN_TIMEZONE)
    conventions = {
        scope.bar_convention_id
        for version in reader.datasets
        if version.contents.record_type == "bar"
        and version.contents.price_basis == "raw"
        and (scope := version.contents.scope) is not None
        and scope.market_id == "cn.equity"
        and "1d" in scope.timeframes
        and set(universe).intersection(scope.instrument_ids)
        and scope.start < cutoff
        and scope.end > first
    }
    if len(conventions) != 1 or None in conventions:
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Daily raw bars need one declared time convention"
        )
    bars = reader.bars(
        BarQuery(
            instrument_ids=universe,
            timeframe="1d",
            price_basis="raw",
            start=first,
            end=cutoff,
            as_of=cutoff,
        )
    )
    bounds = {}
    for bar in bars:
        pair = (bar.bar_start, bar.bar_end)
        if (
            not start <= bar.trading_date <= end
            or bounds.setdefault(bar.trading_date, pair) != pair
        ):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Daily bars disagree on the session bounds")
    if not bounds:
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT, "No daily bars in the requested range"
        )
    dates = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        pair = bounds.get(day)
        if pair is None:
            dates.append(
                CalendarDay(trading_date=day, is_open=False, closed_reason="no_daily_bars")
            )
        else:
            dates.append(
                CalendarDay(
                    trading_date=day,
                    is_open=True,
                    sessions=(
                        SessionInterval(
                            session_id="daily-bar",
                            kind="continuous",
                            start=pair[0],
                            end=pair[1],
                        ),
                    ),
                )
            )
    (convention,) = conventions
    return CalendarVersion.create(
        CalendarContents(
            calendar_id="cn.equity.portfolio.regular-auction",
            venue_id="cn.equity",
            bar_convention_id=convention,
            source_id="snapshot.daily-bars",
            source_sha256=reader.manifest.snapshot_id,
            available_at=cutoff,
            days=tuple(dates),
        )
    )
