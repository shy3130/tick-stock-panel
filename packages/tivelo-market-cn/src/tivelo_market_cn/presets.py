"""Offline, source-backed calendar compilation for regular CN equity auctions.

The bundled catalog is an announced schedule, not proof of realized trading or
security eligibility. Compiled calendars use the existing calendar protocol.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from importlib import resources
from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator
from tivelo_contracts.base import (
    CalendarDate,
    Contract,
    Digest,
    Key,
    UTCDateTime,
    canonical_bytes,
    fingerprint,
    parse_json,
)
from tivelo_contracts.errors import ErrorCode, TiveloError

from .calendar import (
    CN_TIMEZONE,
    CalendarContents,
    CalendarDay,
    CalendarVersion,
    SessionInterval,
)

Venue = Literal["XSHG", "XSHE", "XBSE"]
Clock = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
Weekday = Annotated[int, Field(strict=True, ge=0, le=6)]
_DATE = TypeAdapter(CalendarDate)
_DIGEST = TypeAdapter(Digest)
_BUILTIN_DIGESTS = {
    "2026.1.0": "a621c11f613c020ba2d8062301715d63689d87c75b2d797599d239a6b2201c9e",
    "2026.2.0": "91f329cec3bbc1733a29322c83b2ad20d6c12d4397897fd41082effbca0b0dd1",
}


class CalendarSource(Contract):
    source_id: Key
    title: Key
    publisher: Key
    document_number: Key | None = None
    url: str = Field(pattern=r"^https://[^\s]+$", max_length=2048)
    sha256: Digest
    byte_size: int = Field(strict=True, gt=0)
    published_on: CalendarDate
    publication_precision: Literal["date"] = "date"
    knowledge_policy: Literal["next_cn_midnight"] = "next_cn_midnight"
    retrieved_at: UTCDateTime

    @model_validator(mode="after")
    def chronology(self) -> Self:
        if self.retrieved_at.astimezone(CN_TIMEZONE).date() < self.published_on:
            raise ValueError("source cannot be retrieved before its publication date")
        return self

    @property
    def available_at(self) -> datetime:
        return datetime.combine(
            self.published_on + timedelta(days=1), time(), CN_TIMEZONE
        ).astimezone(UTC)


class LocalSession(Contract):
    session_id: Key
    kind: Literal["continuous", "auction"]
    start: Clock
    end: Clock

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("local sessions must be nonempty and cannot cross midnight")
        return self


class SessionSchedule(Contract):
    schedule_id: Key
    venue_id: Venue
    effective_from: CalendarDate
    source_ids: tuple[Key, ...] = Field(min_length=1)
    clauses: tuple[Key, ...] = Field(min_length=1)
    weekdays: tuple[Weekday, ...] = Field(min_length=1, max_length=7)
    sessions: tuple[LocalSession, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.weekdays != tuple(sorted(set(self.weekdays))):
            raise ValueError("trading weekdays must be unique and ordered")
        if len({s.session_id for s in self.sessions}) != len(self.sessions):
            raise ValueError("session IDs must be unique")
        if not any(s.kind == "continuous" for s in self.sessions):
            raise ValueError("equity daily execution requires a continuous session")
        if any(a.end > b.start for a, b in zip(self.sessions, self.sessions[1:], strict=False)):
            raise ValueError("sessions must be ordered and nonoverlapping")
        return self


class HolidayClosure(Contract):
    reason: Key
    start: CalendarDate
    end: CalendarDate

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.end < self.start:
            raise ValueError("holiday ranges are inclusive and ordered")
        return self


class CalendarYear(Contract):
    venue_id: Venue
    year: int = Field(strict=True, ge=1990, le=2199)
    source_ids: tuple[Key, ...] = Field(min_length=1)
    closures: tuple[HolidayClosure, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        first, last = date(self.year, 1, 1), date(self.year, 12, 31)
        if any(item.end < first or item.start > last for item in self.closures):
            raise ValueError("every holiday range must intersect the declared year")
        if any(a.end >= b.start for a, b in zip(self.closures, self.closures[1:], strict=False)):
            raise ValueError("holiday ranges must be ordered and nonoverlapping")
        return self


class CalendarPresetContents(Contract):
    schema_version: Literal["1.0"] = "1.0"
    preset_id: Literal["cn.equity.auction-calendar"] = "cn.equity.auction-calendar"
    preset_version: Key
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    scope: Literal["announced_regular_equity_auctions"] = "announced_regular_equity_auctions"
    bar_convention_id: Literal["cn.equity.regular-auction.daily.v1"] = (
        "cn.equity.regular-auction.daily.v1"
    )
    verified_on: CalendarDate
    sources: tuple[CalendarSource, ...] = Field(min_length=1, max_length=256)
    schedules: tuple[SessionSchedule, ...] = Field(min_length=1, max_length=128)
    years: tuple[CalendarYear, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def references(self) -> Self:
        ids = tuple(source.source_id for source in self.sources)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("source IDs must be unique and sorted")
        if any(
            source.retrieved_at.astimezone(CN_TIMEZONE).date() > self.verified_on
            for source in self.sources
        ):
            raise ValueError("verification cannot precede source retrieval")
        years = tuple((item.venue_id, item.year) for item in self.years)
        schedules = tuple((item.venue_id, item.effective_from) for item in self.schedules)
        if years != tuple(sorted(set(years))) or schedules != tuple(sorted(set(schedules))):
            raise ValueError("calendar years and schedule epochs must be unique and sorted")
        if len({item.schedule_id for item in self.schedules}) != len(self.schedules):
            raise ValueError("schedule IDs must be unique")
        for item in (*self.schedules, *self.years):
            if item.source_ids != tuple(sorted(set(item.source_ids))) or not set(
                item.source_ids
            ) <= set(ids):
                raise ValueError("each source reference must be declared, unique and sorted")
        for venue in {item.venue_id for item in self.years}:
            covered = [item.year for item in self.years if item.venue_id == venue]
            if covered != list(range(covered[0], covered[-1] + 1)):
                raise ValueError("announced calendar years must be contiguous per venue")
            if not any(
                item.venue_id == venue and item.effective_from <= date(covered[0], 1, 1)
                for item in self.schedules
            ):
                raise ValueError("calendar coverage requires an effective session schedule")
        return self


class CalendarPreset(Contract):
    preset_format: Literal["tivelo-cn-calendar-preset-v1"] = "tivelo-cn-calendar-preset-v1"
    version_id: Digest
    contents: CalendarPresetContents

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.version_id != fingerprint(self.contents):
            raise ValueError("calendar preset fingerprint mismatch")
        return self

    @classmethod
    def create(cls, contents: CalendarPresetContents) -> Self:
        return cls(version_id=fingerprint(contents), contents=contents)


def load_calendar_preset(*, version: str, expected_sha256: str | None = None) -> CalendarPreset:
    """Load one bundled version; never resolve 'latest', fetch or install data."""
    expected = _BUILTIN_DIGESTS.get(version)
    if expected is None:
        raise TiveloError(ErrorCode.RULES_UNAVAILABLE, "Requested calendar preset is not installed")
    if expected_sha256 is not None and _DIGEST.validate_python(expected_sha256) != expected:
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Installed calendar differs from its pinned digest"
        )
    try:
        raw = (
            resources.files("tivelo_market_cn")
            .joinpath("calendar_presets", f"{version}.json")
            .read_text(encoding="utf-8")
        )
        preset = CalendarPreset.model_validate(parse_json(raw))
        if preset.version_id != expected or preset.contents.preset_version != version:
            raise ValueError("bundled preset differs from the release manifest")
        return preset
    except (OSError, ValueError) as exc:
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Installed calendar preset is invalid"
        ) from exc


def installed_calendar_presets() -> tuple[CalendarPreset, ...]:
    """Verified announced calendars available in this distribution, newest publication first."""
    presets = (load_calendar_preset(version=version) for version in _BUILTIN_DIGESTS)
    return tuple(
        sorted(presets, key=lambda p: (p.contents.verified_on, p.contents.preset_version), reverse=True)
    )


def compile_calendar(
    preset: CalendarPreset, *, venue_id: str, start: date, end: date
) -> CalendarVersion:
    """Compile an explicit range; per-date knowledge checks remain in CNCalendar.

    The caller must include any next session needed for T+1. No day outside the
    requested/verified range is invented, including beyond a year boundary.
    """
    preset = CalendarPreset.model_validate_json(canonical_bytes(preset))
    start, end = _DATE.validate_python(start), _DATE.validate_python(end)
    if end < start:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Calendar range is reversed")
    contents = preset.contents
    years = {item.year: item for item in contents.years if item.venue_id == venue_id}
    if not years:
        raise TiveloError(ErrorCode.UNSUPPORTED_ASSET, "Calendar preset does not cover this venue")
    if not set(range(start.year, end.year + 1)) <= years.keys():
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Calendar preset does not cover this range"
        )
    schedules = tuple(item for item in contents.schedules if item.venue_id == venue_id)
    sources = {item.source_id: item for item in contents.sources}
    days = []
    current = start
    while current <= end:
        year = years[current.year]
        schedule = next(item for item in reversed(schedules) if item.effective_from <= current)
        available_at = max(
            sources[key].available_at for key in (*year.source_ids, *schedule.source_ids)
        )
        holiday = next((item for item in year.closures if item.start <= current <= item.end), None)
        closed_reason = (
            holiday.reason
            if holiday
            else ("weekend" if current.weekday() not in schedule.weekdays else None)
        )
        sessions = ()
        if closed_reason is None:
            sessions = tuple(
                SessionInterval(
                    session_id=item.session_id,
                    kind=item.kind,
                    start=datetime.combine(current, time.fromisoformat(item.start), CN_TIMEZONE),
                    end=datetime.combine(current, time.fromisoformat(item.end), CN_TIMEZONE),
                )
                for item in schedule.sessions
            )
        days.append(
            CalendarDay(
                trading_date=current,
                is_open=closed_reason is None,
                sessions=sessions,
                closed_reason=closed_reason,
                available_at=available_at,
            )
        )
        current += timedelta(days=1)
    return CalendarVersion.create(
        CalendarContents(
            calendar_id=f"cn.equity.{venue_id.lower()}.regular-auction",
            venue_id=venue_id,
            bar_convention_id=contents.bar_convention_id,
            source_id=contents.preset_id,
            source_sha256=preset.version_id,
            available_at=min(day.available_at for day in days),
            days=tuple(days),
        )
    )
