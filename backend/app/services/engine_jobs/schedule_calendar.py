"""Compile once with the installed market package. Dispatch from the sealed UTC times."""

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from operator import attrgetter
from typing import Self

from pydantic import Field, model_validator
from tivelo_contracts.base import CalendarDate, Contract, Digest, Key, UTCDateTime
from tivelo_contracts.errors import ErrorCode, TiveloError

from .schedule_models import DispatchTime, ScheduleRequest, TradingDayTrigger
from .storage import JobError


class TradingOccurrence(Contract):
    trading_date: CalendarDate
    due_at: UTCDateTime


class TradingCalendar(Contract):
    preset_sha256: Digest
    calendar_id: Key
    calendar_sha256: Digest
    occurrences: tuple[TradingOccurrence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if any(
            a.trading_date >= b.trading_date or a.due_at >= b.due_at
            for a, b in zip(self.occurrences, self.occurrences[1:], strict=False)
        ):
            raise ValueError("trading dates and dispatch instants must increase")
        return self

    def summary(self):
        return {
            **self.model_dump(mode="json", exclude={"occurrences"}),
            "trading_days": len(self.occurrences),
            "last_due_at": self.occurrences[-1].due_at.isoformat(),
        }

    def occurrence(self, at: datetime, cursor: datetime) -> DispatchTime | None:
        by_time = attrgetter("due_at")
        first = bisect_left(self.occurrences, cursor, key=by_time)
        if first == len(self.occurrences) or self.occurrences[first].due_at != cursor:
            raise JobError("ARTIFACT_CORRUPT", "计划游标不在已锁定的交易日历中")
        last = bisect_right(self.occurrences, at, key=by_time) - 1
        if last < first:
            return None
        current = self.occurrences[last]
        following = self.occurrences[last + 1].due_at if last + 1 < len(self.occurrences) else None
        return DispatchTime(current.due_at, following, last - first, current.trading_date)


class FrozenTradingSchedule(Contract):
    request: ScheduleRequest
    calendar: TradingCalendar


def calendar_choices():
    try:
        from tivelo_market_cn import presets
    except ModuleNotFoundError as exc:
        if exc.name != "tivelo_market_cn":
            raise
        return []
    # Previous market wheels can compile an explicitly named preset.
    discover = getattr(presets, "installed_calendar_presets", None)
    if discover is None:
        return []
    choices = []
    for preset in discover():
        venues = {}
        for year in preset.contents.years:
            venues.setdefault(year.venue_id, []).append(year.year)
        choices.append(
            {
                "version": preset.contents.preset_version,
                "preset_sha256": preset.version_id,
                "venues": {venue: sorted(years) for venue, years in venues.items()},
            }
        )
    return choices


def freeze_trading_days(trigger: TradingDayTrigger, *, as_of: datetime) -> TradingCalendar:
    try:
        from tivelo_market_cn.calendar import CNCalendar
        from tivelo_market_cn.presets import compile_calendar, load_calendar_preset
    except ModuleNotFoundError as exc:
        if exc.name != "tivelo_market_cn":
            raise
        raise JobError("RULES_UNAVAILABLE", "请安装提供沪深交易日历的市场规则包") from None

    try:
        preset = load_calendar_preset(
            version=trigger.calendar_version, expected_sha256=trigger.preset_sha256
        )
        version = compile_calendar(
            preset, venue_id=trigger.venue_id, start=trigger.start_date, end=trigger.end_date
        )
        days = CNCalendar(version).trading_days(trigger.start_date, trigger.end_date, as_of=as_of)
    except TiveloError as exc:
        messages = {
            ErrorCode.RULES_UNAVAILABLE: "指定的交易日历版本尚未安装",
            ErrorCode.DATA_COVERAGE_INSUFFICIENT: "已安装日历未覆盖所选交易所的完整日期范围",
            ErrorCode.ARTIFACT_CORRUPT: "安装的交易日历与指定摘要不一致或文件已损坏",
            ErrorCode.FUTURE_INFORMATION: "所选区间的日历公告尚未生效",
        }
        raise JobError(exc.code.value, messages.get(exc.code, "所选交易日历无法使用")) from exc
    offset = timedelta(minutes=trigger.offset_minutes)
    occurrences = []
    for day in days:
        due = (day.open_at if trigger.anchor == "open" else day.close_at) + offset
        if due < day.available_at:
            raise JobError("FUTURE_INFORMATION", "派发时点早于相应交易日历的可知时刻")
        occurrences.append(TradingOccurrence(trading_date=day.trading_date, due_at=due))
    if not occurrences:
        raise JobError("DATA_COVERAGE_INSUFFICIENT", "所选日期范围内没有交易日")
    return TradingCalendar(
        preset_sha256=preset.version_id,
        calendar_id=version.contents.calendar_id,
        calendar_sha256=version.version_id,
        occurrences=tuple(occurrences),
    )
