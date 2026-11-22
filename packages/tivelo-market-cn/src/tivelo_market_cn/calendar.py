"""Explicit versioned CN sessions; never infer exchange holidays from weekdays."""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import Field, TypeAdapter, model_validator
from tivelo_contracts.base import (
    CalendarDate,
    Contract,
    Digest,
    Key,
    UTCDateTime,
    canonical_bytes,
    fingerprint,
    utc_datetime,
)
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.market import TradingDay

CN_TIMEZONE = ZoneInfo("Asia/Shanghai")
_DATE_ADAPTER = TypeAdapter(CalendarDate)


class SessionInterval(Contract):
    session_id: Key
    kind: Literal["continuous", "auction"]
    start: UTCDateTime
    end: UTCDateTime

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.end <= self.start:
            raise ValueError("session interval must be nonempty")
        return self


class CalendarDay(Contract):
    trading_date: CalendarDate
    is_open: bool = Field(strict=True)
    sessions: tuple[SessionInterval, ...] = ()
    closed_reason: Key | None = None
    available_at: UTCDateTime | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.is_open:
            if self.closed_reason is not None or not any(
                s.kind == "continuous" for s in self.sessions
            ):
                raise ValueError("open dates require continuous sessions and no closure reason")
        elif self.sessions or self.closed_reason is None:
            raise ValueError("closed dates require a reason and no sessions")
        if len({s.session_id for s in self.sessions}) != len(self.sessions):
            raise ValueError("session IDs must be unique within the date")
        for index, session in enumerate(self.sessions):
            if (
                session.start.astimezone(CN_TIMEZONE).date() != self.trading_date
                or session.end.astimezone(CN_TIMEZONE).date() != self.trading_date
            ):
                raise ValueError("CN session must belong to its explicit Beijing trading date")
            if index and self.sessions[index - 1].end > session.start:
                raise ValueError("sessions must be ordered and nonoverlapping")
        return self


class CalendarContents(Contract):
    schema_version: Literal["1.0"] = "1.0"
    calendar_id: Key
    venue_id: Key
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    bar_convention_id: Key
    source_id: Key
    source_sha256: Digest
    available_at: UTCDateTime
    days: tuple[CalendarDay, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def contiguous_dates(self) -> Self:
        for previous, following in zip(self.days, self.days[1:], strict=False):
            if following.trading_date - previous.trading_date != timedelta(days=1):
                raise ValueError(
                    "calendar dates must be ordered and contiguous, including closures"
                )
        return self


class CalendarVersion(Contract):
    version_id: Digest
    contents: CalendarContents

    @model_validator(mode="after")
    def verify_identity(self) -> Self:
        if self.version_id != fingerprint(self.contents):
            raise ValueError("calendar content fingerprint mismatch")
        return self

    @classmethod
    def create(cls, contents: CalendarContents) -> Self:
        return cls(version_id=fingerprint(contents), contents=contents)


class CNCalendar:
    def __init__(self, version: CalendarVersion, *, from_bars: bool = False):
        self._version = CalendarVersion.model_validate_json(canonical_bytes(version))
        self.from_bars = from_bars
        self._days = {day.trading_date: day for day in self.version.contents.days}
        self._open_dates = tuple(
            day.trading_date for day in self.version.contents.days if day.is_open
        )

    @property
    def version(self) -> CalendarVersion:
        return self._version

    def _day(self, trading_date: date, *, as_of: datetime) -> CalendarDay:
        day = _DATE_ADAPTER.validate_python(trading_date)
        if day not in self._days:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Date is outside the locked calendar"
            )
        record = self._days[day]
        if (
            not self.from_bars
            and (record.available_at or self.version.contents.available_at) > utc_datetime(as_of)
        ):
            raise TiveloError(ErrorCode.FUTURE_INFORMATION, "Calendar date was not yet known")
        return record

    def is_open(self, trading_date: date, *, as_of: datetime) -> bool:
        return self._day(trading_date, as_of=as_of).is_open

    def regular_sessions(
        self, trading_date: date, *, as_of: datetime
    ) -> tuple[SessionInterval, ...]:
        """From the first continuous opening through close, including the closing auction."""
        day = self._day(trading_date, as_of=as_of)
        if not day.is_open:
            return ()
        opening = next(session.start for session in day.sessions if session.kind == "continuous")
        return tuple(session for session in day.sessions if session.start >= opening)

    def daily_bounds(
        self, trading_date: date, *, as_of: datetime
    ) -> tuple[datetime, datetime, str]:
        day = self._day(trading_date, as_of=as_of)
        if not day.is_open:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Closed date has no daily trading bounds"
            )
        intervals = tuple(s for s in day.sessions if s.kind == "continuous")
        return intervals[0].start, intervals[-1].end, self.version.contents.bar_convention_id

    def session_at(self, moment: datetime, *, as_of: datetime) -> SessionInterval | None:
        moment = utc_datetime(moment)
        day = self._day(moment.astimezone(CN_TIMEZONE).date(), as_of=as_of)
        return next((s for s in day.sessions if s.start <= moment < s.end), None)

    def next_trading_open(self, after_date: date, *, as_of: datetime) -> datetime:
        day = self._day(after_date, as_of=as_of)
        index = bisect_right(self._open_dates, day.trading_date)
        if index < len(self._open_dates):
            following = self._days[self._open_dates[index]]
            cursor = day.trading_date + timedelta(days=1)
            while cursor <= following.trading_date:
                self._day(cursor, as_of=as_of)
                cursor += timedelta(days=1)
            return next(s.start for s in following.sessions if s.kind == "continuous")
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Next trading date is outside the locked calendar"
        )

    def trading_days(self, start: date, end: date, *, as_of: datetime) -> tuple[TradingDay, ...]:
        day, end = _DATE_ADAPTER.validate_python(start), _DATE_ADAPTER.validate_python(end)
        if end < day:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Trading-date range is reversed")
        result = []
        while day <= end:
            record = self._day(day, as_of=as_of)
            if record.is_open:
                opening = next(s.start for s in record.sessions if s.kind == "continuous")
                result.append(
                    TradingDay(
                        calendar_id=self.version.contents.calendar_id,
                        trading_date=day,
                        open_at=opening,
                        close_at=record.sessions[-1].end,
                        bar_convention_id=self.version.contents.bar_convention_id,
                        available_at=record.available_at or self.version.contents.available_at,
                    )
                )
            day += timedelta(days=1)
        return tuple(result)
