from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import polars as pl

from app.market_rules import market_for_symbol
from app.services.dow_monitor_data import MarketSessionPolicy, market_session_policy

BarCompletion = Literal["FORMING", "FINAL"]
TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "60m": 60}


@dataclass(frozen=True)
class TimeframeBars:
    completed: list[dict]
    forming: dict
    completion: BarCompletion
    source_timestamp: datetime | None

    @property
    def all_bars(self) -> list[dict]:
        if self.completion == "FINAL" or not self.forming:
            return list(self.completed)
        return [*self.completed, self.forming]


@dataclass
class _Bucket:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float

    def add(self, row: dict) -> None:
        self.high = max(self.high, float(row["high"]))
        self.low = min(self.low, float(row["low"]))
        self.close = float(row["close"])
        self.volume += float(row.get("volume") or 0.0)
        self.amount += float(row.get("amount") or 0.0)

    def as_dict(self) -> dict:
        return {
            "timestamp": self.start.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
        }


def bucket_start(local_dt: datetime, session_start: time, minutes: int) -> datetime:
    anchor = datetime.combine(local_dt.date(), session_start, tzinfo=local_dt.tzinfo)
    elapsed = int((local_dt - anchor).total_seconds() // 60)
    return anchor + timedelta(minutes=(elapsed // minutes) * minutes)


def _local_datetime(value: object, zone: ZoneInfo) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _session_segment(
    local_dt: datetime,
    policy: MarketSessionPolicy,
    *,
    merge_close: bool,
) -> tuple[time, time] | None:
    local_time = local_dt.time().replace(tzinfo=None)
    for session_start, session_end in policy.sessions:
        if session_start <= local_time < session_end:
            return session_start, session_end
    if merge_close and local_time == policy.sessions[-1][1]:
        return policy.sessions[-1]
    return None


def _regular_minutes(
    symbol: str,
    minute_rows: pl.DataFrame,
    zone: ZoneInfo,
    policy: MarketSessionPolicy,
    now_local: datetime,
) -> list[tuple[datetime, tuple[time, time], dict]]:
    if minute_rows.is_empty():
        return []

    normalized_symbol = symbol.strip().upper()
    merge_close = market_for_symbol(normalized_symbol) in {"cn", "hk"}
    candidates: list[tuple[datetime, tuple[time, time], dict]] = []
    for row in minute_rows.to_dicts():
        row_symbol = str(row.get("symbol") or normalized_symbol).strip().upper()
        if row_symbol != normalized_symbol or row.get("datetime") is None:
            continue
        local_dt = _local_datetime(row["datetime"], zone)
        if local_dt > now_local or local_dt.weekday() >= 5:
            continue
        segment = _session_segment(local_dt, policy, merge_close=merge_close)
        if segment is not None:
            candidates.append((local_dt, segment, row))

    ordered = sorted(candidates, key=lambda item: item[0])
    observed = {local_dt for local_dt, _, _ in ordered}
    return [
        item
        for item in ordered
        if item[0].time().replace(tzinfo=None) != item[1][1]
        or item[0] - timedelta(minutes=1) in observed
    ]


def _aggregate_minutes(
    regular_rows: list[tuple[datetime, tuple[time, time], dict]],
    minutes: int,
) -> list[_Bucket]:
    buckets: dict[tuple[datetime, datetime], _Bucket] = {}
    for local_dt, (session_start, session_end), row in regular_rows:
        session_end_dt = datetime.combine(
            local_dt.date(),
            session_end,
            tzinfo=local_dt.tzinfo,
        )
        if local_dt == session_end_dt:
            start = max(
                datetime.combine(
                    local_dt.date(),
                    session_start,
                    tzinfo=local_dt.tzinfo,
                ),
                session_end_dt - timedelta(minutes=minutes),
            )
        else:
            start = bucket_start(local_dt, session_start, minutes)
        end = min(start + timedelta(minutes=minutes), session_end_dt)
        key = (start, end)
        bucket = buckets.get(key)
        if local_dt == session_end_dt and bucket is None:
            continue
        if bucket is None:
            buckets[key] = _Bucket(
                start=start,
                end=end,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0.0),
                amount=float(row.get("amount") or 0.0),
            )
        else:
            bucket.add(row)
    return [buckets[key] for key in sorted(buckets)]


def _frame_from_buckets(
    buckets: list[_Bucket],
    now_local: datetime,
    source_timestamp: datetime | None,
) -> TimeframeBars:
    if not buckets:
        return TimeframeBars(
            completed=[],
            forming={},
            completion="FINAL",
            source_timestamp=source_timestamp,
        )

    bars = [bucket.as_dict() for bucket in buckets]
    completion: BarCompletion = "FINAL" if now_local >= buckets[-1].end else "FORMING"
    return TimeframeBars(
        completed=bars if completion == "FINAL" else bars[:-1],
        forming=bars[-1],
        completion=completion,
        source_timestamp=source_timestamp,
    )


def _daily_date(row: dict, zone: ZoneInfo) -> date | None:
    value = row.get("date", row.get("timestamp", row.get("datetime")))
    if value is None:
        return None
    if isinstance(value, datetime):
        return _local_datetime(value, zone).date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def _daily_bar(row_date: date, row: dict) -> dict:
    return {
        "timestamp": row_date.isoformat(),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row.get("volume") or 0.0),
        "amount": float(row.get("amount") or 0.0),
    }


def _build_daily(
    symbol: str,
    daily_rows: pl.DataFrame,
    regular_rows: list[tuple[datetime, tuple[time, time], dict]],
    now_local: datetime,
    policy: MarketSessionPolicy,
) -> TimeframeBars:
    normalized_symbol = symbol.strip().upper()
    by_date: dict[date, dict] = {}
    if not daily_rows.is_empty():
        for row in daily_rows.to_dicts():
            row_symbol = str(row.get("symbol") or normalized_symbol).strip().upper()
            row_date = _daily_date(row, now_local.tzinfo)
            if (
                row_symbol == normalized_symbol
                and row_date is not None
                and row_date < now_local.date()
            ):
                by_date[row_date] = _daily_bar(row_date, row)

    today_rows = [item for item in regular_rows if item[0].date() == now_local.date()]
    source_timestamp: datetime | None = None
    if today_rows:
        first_dt, _, first_row = today_rows[0]
        aggregate = _Bucket(
            start=datetime.combine(
                first_dt.date(),
                time.min,
                tzinfo=first_dt.tzinfo,
            ),
            end=datetime.combine(
                first_dt.date(),
                policy.sessions[-1][1],
                tzinfo=first_dt.tzinfo,
            ),
            open=float(first_row["open"]),
            high=float(first_row["high"]),
            low=float(first_row["low"]),
            close=float(first_row["close"]),
            volume=float(first_row.get("volume") or 0.0),
            amount=float(first_row.get("amount") or 0.0),
        )
        for _, _, row in today_rows[1:]:
            aggregate.add(row)
        by_date[now_local.date()] = {
            **aggregate.as_dict(),
            "timestamp": now_local.date().isoformat(),
        }
        source_timestamp = max(local_dt for local_dt, _, _ in today_rows)
    elif by_date:
        latest_date = max(by_date)
        source_timestamp = datetime.combine(
            latest_date,
            time.min,
            tzinfo=now_local.tzinfo,
        )

    if not by_date:
        return TimeframeBars([], {}, "FINAL", source_timestamp)

    ordered = [by_date[row_date] for row_date in sorted(by_date)]
    latest_date = date.fromisoformat(ordered[-1]["timestamp"])
    market_close = datetime.combine(
        latest_date,
        policy.sessions[-1][1],
        tzinfo=now_local.tzinfo,
    )
    completion: BarCompletion = (
        "FORMING" if latest_date == now_local.date() and now_local < market_close else "FINAL"
    )
    return TimeframeBars(
        completed=ordered if completion == "FINAL" else ordered[:-1],
        forming=ordered[-1],
        completion=completion,
        source_timestamp=source_timestamp,
    )


def build_timeframes(
    symbol: str,
    minute_rows: pl.DataFrame,
    daily_rows: pl.DataFrame,
    now: datetime,
) -> dict[str, TimeframeBars]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    policy = market_session_policy(symbol)
    zone = ZoneInfo(policy.timezone)
    now_local = now.astimezone(zone)
    regular_rows = _regular_minutes(symbol, minute_rows, zone, policy, now_local)
    source_timestamp = max(
        (local_dt for local_dt, _, _ in regular_rows),
        default=None,
    )

    frames = {
        timeframe: _frame_from_buckets(
            _aggregate_minutes(regular_rows, minutes),
            now_local,
            source_timestamp,
        )
        for timeframe, minutes in TIMEFRAME_MINUTES.items()
    }
    frames["day"] = _build_daily(
        symbol,
        daily_rows,
        regular_rows,
        now_local,
        policy,
    )
    return frames
