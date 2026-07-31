from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars as exchange_calendars
import pandas as pd

MARKET_CALENDARS = {"cn": "XSHG", "hk": "XHKG", "us": "XNYS"}
BEIJING = ZoneInfo("Asia/Shanghai")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


class HalfHourWindowCalendar:
    def __init__(self) -> None:
        self._calendars = {
            market: exchange_calendars.get_calendar(name)
            for market, name in MARKET_CALENDARS.items()
        }

    def session_window_ends(
        self,
        market: str,
        beijing_trade_date: date,
    ) -> list[datetime]:
        calendar = self._calendar(market)
        session = pd.Timestamp(beijing_trade_date)
        if not calendar.is_session(session):
            return []

        opened = calendar.session_open(session).to_pydatetime()
        closed = calendar.session_close(session).to_pydatetime()
        break_start = calendar.session_break_start(session)
        break_end = calendar.session_break_end(session)
        segments = (
            [(opened, closed)]
            if pd.isna(break_start) or pd.isna(break_end)
            else [
                (opened, break_start.to_pydatetime()),
                (break_end.to_pydatetime(), closed),
            ]
        )

        checkpoints: list[datetime] = []
        for start, end in segments:
            checkpoint = start + timedelta(minutes=30)
            while checkpoint <= end:
                checkpoints.append(checkpoint.astimezone(BEIJING))
                checkpoint += timedelta(minutes=30)
            if (checkpoints and checkpoints[-1] != end.astimezone(BEIJING)) or not checkpoints:
                checkpoints.append(end.astimezone(BEIJING))
        return checkpoints

    def completed_window_ends(
        self,
        market: str,
        now: datetime,
    ) -> list[datetime]:
        current = _aware(now).astimezone(BEIJING)
        candidates: list[tuple[datetime, datetime, date]] = []
        for days_back in range(0, 3):
            session_date = current.date() - timedelta(days=days_back)
            windows = self.session_window_ends(market, session_date)
            if not windows:
                continue
            opened = self._calendar(market).session_open(
                pd.Timestamp(session_date)
            ).to_pydatetime().astimezone(BEIJING)
            candidates.append((opened, windows[-1], session_date))
        eligible = [
            item
            for item in candidates
            if item[0] <= current <= item[1] + timedelta(hours=2)
        ]
        if not eligible:
            return []
        _, _, session_date = max(eligible, key=lambda item: item[0])
        return [
            checkpoint
            for checkpoint in self.session_window_ends(market, session_date)
            if checkpoint <= current
        ]

    def is_regular_session_time(
        self,
        market: str,
        observed_at: datetime,
    ) -> bool:
        observed = _aware(observed_at).astimezone(UTC)
        for days_back in range(0, 2):
            session_date = observed.astimezone(BEIJING).date() - timedelta(
                days=days_back
            )
            calendar = self._calendar(market)
            session = pd.Timestamp(session_date)
            if not calendar.is_session(session):
                continue
            opened = calendar.session_open(session).to_pydatetime()
            closed = calendar.session_close(session).to_pydatetime()
            if not opened <= observed <= closed:
                continue
            break_start = calendar.session_break_start(session)
            break_end = calendar.session_break_end(session)
            if pd.isna(break_start) or pd.isna(break_end):
                return True
            return not (
                break_start.to_pydatetime()
                <= observed
                < break_end.to_pydatetime()
            )
        return False

    def session_open(self, market: str, trade_date: date) -> datetime | None:
        calendar = self._calendar(market)
        session = pd.Timestamp(trade_date)
        if not calendar.is_session(session):
            return None
        return calendar.session_open(session).to_pydatetime().astimezone(BEIJING)

    def trade_date_for_checkpoint(
        self,
        market: str,
        window_end: datetime,
    ) -> date:
        checkpoint = _aware(window_end).astimezone(BEIJING)
        for days_back in range(0, 3):
            candidate = checkpoint.date() - timedelta(days=days_back)
            if checkpoint in self.session_window_ends(market, candidate):
                return candidate
        raise ValueError("checkpoint does not belong to a regular session")

    def _calendar(self, market: str):
        try:
            return self._calendars[market]
        except KeyError as exc:
            raise ValueError(f"unsupported market: {market}") from exc
