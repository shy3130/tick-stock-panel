from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from app.services.dow_monitor_minute_result_calculator import calculate_minute_result
from app.services.dow_monitor_minute_result_history import (
    DowMonitorMinuteResultHistoryBuilder,
)
from app.services.dow_monitor_minute_result_models import (
    DowMonitorMinuteResult,
    MinuteResultKey,
)
from app.services.dow_monitor_models import DowNotification, MonitoredSymbol

MARKET_ZONES = {
    "cn": ZoneInfo("Asia/Shanghai"),
    "hk": ZoneInfo("Asia/Hong_Kong"),
    "us": ZoneInfo("America/New_York"),
}
WARMUP_DAYS = 10
LIVE_MINUTE_MAX_AGE = timedelta(minutes=2)


class MaterializerStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    last_started_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    pending_minutes: int = 0
    last_written_rows: int = 0


class MaterializeRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: tuple[DowMonitorMinuteResult, ...] = ()
    inserted_keys: tuple[MinuteResultKey, ...] = ()
    written_rows: int = 0
    error: str | None = None


class DowMonitorMinuteResultMaterializer:
    def __init__(
        self,
        *,
        source,
        repository,
        history_builder: DowMonitorMinuteResultHistoryBuilder,
        notifications_fn: Callable[[], Sequence[DowNotification]],
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._source = source
        self._repository = repository
        self._history_builder = history_builder
        self._notifications_fn = notifications_fn
        self._now_fn = now_fn
        self._status = MaterializerStatus()

    def materialize(
        self,
        symbols: Sequence[MonitoredSymbol],
        now: datetime | None = None,
    ) -> MaterializeRun:
        anchor = now or self._now_fn()
        if anchor.tzinfo is None or anchor.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        self._status.last_started_at = anchor
        self._status.last_written_rows = 0
        enabled = [item for item in symbols if item.enabled]
        if not enabled:
            self._status.last_success_at = anchor
            self._status.last_error = None
            self._status.pending_minutes = 0
            return MaterializeRun()

        groups: dict[tuple[str, object], list[MonitoredSymbol]] = defaultdict(list)
        for item in enabled:
            local_day = anchor.astimezone(MARKET_ZONES[item.market]).date()
            groups[(item.market, local_day)].append(item)

        rows: list[DowMonitorMinuteResult] = []
        inserted_keys: list[MinuteResultKey] = []
        errors: list[tuple[str, str]] = []
        pending = 0
        written = 0
        try:
            notifications = tuple(self._notifications_fn())
        except Exception as exc:
            message = self._safe_error(exc)
            self._status.last_error = message
            return MaterializeRun(error=message)

        for (market, market_day), items in groups.items():
            zone = MARKET_ZONES[market]
            local_start = datetime.combine(
                market_day,
                datetime.min.time(),
                tzinfo=zone,
            )
            day_start = local_start.astimezone(UTC)
            candle_start = day_start - timedelta(days=WARMUP_DAYS)
            group_symbols = [item.symbol for item in items]
            try:
                history = self._source.load_raw_history(
                    group_symbols,
                    day_start,
                    anchor,
                    candle_start=candle_start,
                )
                existing = self._repository.existing_keys(
                    group_symbols,
                    day_start,
                    anchor + timedelta(minutes=1),
                )
                contexts = []
                for item in items:
                    contexts.extend(
                        self._history_builder.build_contexts(
                            history,
                            item,
                            market_day,
                            True,
                            notifications=notifications,
                        )
                    )
                latest_by_symbol = {
                    item.symbol: max(
                        (
                            context.decision_minute
                            for context in contexts
                            if context.symbol == item.symbol
                        ),
                        default=None,
                    )
                    for item in items
                }
                missing_contexts = []
                for context in contexts:
                    key = MinuteResultKey(
                        market=context.market,
                        symbol=context.symbol,
                        decision_minute=context.decision_minute,
                    )
                    if key in existing:
                        continue
                    latest = latest_by_symbol.get(context.symbol)
                    live = (
                        latest == context.decision_minute
                        and timedelta(0) <= anchor - context.decision_minute <= LIVE_MINUTE_MAX_AGE
                    )
                    missing_contexts.append(
                        context.model_copy(update={"backfill": not live})
                    )
                group_rows = [
                    calculate_minute_result(context)
                    for context in missing_contexts
                ]
                pending += len(group_rows)
                if group_rows:
                    group_written = self._repository.insert_results(group_rows)
                    written += group_written
                    pending -= group_written
                    rows.extend(group_rows)
                    inserted_keys.extend(
                        MinuteResultKey(
                            market=row.market,
                            symbol=row.symbol,
                            decision_minute=row.decision_minute,
                        )
                        for row in group_rows[:group_written]
                    )
            except Exception as exc:
                errors.append((market, self._safe_error(exc)))

        error = (
            None
            if not errors
            else errors[0][1]
            if len(errors) == 1
            else "; ".join(f"{market}: {message}" for market, message in errors)
        )
        self._status.last_error = error
        self._status.pending_minutes = pending
        self._status.last_written_rows = written
        if error is None:
            self._status.last_success_at = anchor
        return MaterializeRun(
            rows=tuple(rows),
            inserted_keys=tuple(inserted_keys),
            written_rows=written,
            error=error,
        )

    def status(self) -> MaterializerStatus:
        return self._status.model_copy(deep=True)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return str(exc).replace("\n", " ")[:500]


class UnavailableMinuteResultMaterializer:
    def __init__(self, error: str) -> None:
        self._status = MaterializerStatus(
            enabled=False,
            last_error=error.replace("\n", " ")[:500],
        )

    def materialize(
        self,
        _symbols: Sequence[MonitoredSymbol],
        _now: datetime | None = None,
    ) -> MaterializeRun:
        return MaterializeRun(error=self._status.last_error)

    def status(self) -> MaterializerStatus:
        return self._status.model_copy(deep=True)
