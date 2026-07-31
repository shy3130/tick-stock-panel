from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from inspect import Parameter, signature
from time import monotonic
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from app.services.dow_monitor_minute_result_calculator import calculate_minute_result
from app.services.dow_monitor_minute_result_history import (
    DowMonitorMinuteResultHistoryBuilder,
)
from app.services.dow_monitor_minute_result_models import (
    DowMonitorMinuteResult,
    MinuteResultKey,
    normalize_monitor_symbol,
)
from app.services.dow_monitor_models import DowNotification, MonitoredSymbol

MARKET_ZONES = {
    "cn": ZoneInfo("Asia/Shanghai"),
    "hk": ZoneInfo("Asia/Hong_Kong"),
    "us": ZoneInfo("America/New_York"),
}
WARMUP_DAYS = 10
LIVE_MINUTE_MAX_AGE = timedelta(minutes=2)
MAX_CHECKPOINT_ROWS = 500


class MaterializerStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    last_started_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    pending_minutes: int = 0
    last_written_rows: int = 0
    scanned_keys: int = 0
    remaining_keys: int = 0
    market: str | None = None
    last_duration_seconds: float | None = None


class MaterializeError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class MaterializeRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: tuple[DowMonitorMinuteResult, ...] = ()
    inserted_keys: tuple[MinuteResultKey, ...] = ()
    written_rows: int = 0
    scanned_keys: int = 0
    remaining_keys: int = 0
    error: str | MaterializeError | None = None


class MissingMinuteKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: tuple[MinuteResultKey, ...] = ()
    scanned_keys: int = 0


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

    @property
    def repository(self):
        return self._repository

    @staticmethod
    def _call_with_deadline(method, *args, deadline: float | None, **kwargs):
        if deadline is not None:
            try:
                parameters = tuple(signature(method).parameters.values())
            except (TypeError, ValueError):
                parameters = ()
            if any(
                parameter.name == "deadline"
                or parameter.kind == Parameter.VAR_KEYWORD
                for parameter in parameters
            ):
                kwargs["deadline"] = deadline
        return method(*args, **kwargs)

    @staticmethod
    def _require_budget(deadline: float | None) -> None:
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError("minute result resource budget exhausted")

    def materialize(
        self,
        symbols: Sequence[MonitoredSymbol],
        now: datetime | None = None,
        *,
        max_rows: int | None = None,
        deadline: float | None = None,
        market_day: date | None = None,
    ) -> MaterializeRun:
        if max_rows is not None:
            return self._materialize_bounded(
                symbols,
                now,
                max_rows=max_rows,
                deadline=deadline,
                market_day=market_day,
            )
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

        groups: dict[tuple[str, date], list[MonitoredSymbol]] = defaultdict(list)
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
                    candidate_keys = self._history_builder.candidate_keys(
                        history,
                        item,
                        market_day,
                    )
                    missing_keys = candidate_keys - existing
                    if not missing_keys:
                        continue
                    contexts.extend(
                        self._history_builder.build_contexts(
                            history,
                            item,
                            market_day,
                            True,
                            notifications=notifications,
                            decision_minutes={
                                key.decision_minute for key in missing_keys
                            },
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

    def _materialize_bounded(
        self,
        symbols: Sequence[MonitoredSymbol],
        now: datetime | None,
        *,
        max_rows: int,
        deadline: float | None,
        market_day: date | None,
    ) -> MaterializeRun:
        if max_rows <= 0:
            raise ValueError("max_rows must be positive")
        anchor = now or self._now_fn()
        if anchor.tzinfo is None or anchor.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        started = monotonic()
        self._status.last_started_at = anchor
        self._status.last_written_rows = 0
        self._status.scanned_keys = 0
        self._status.remaining_keys = 0
        self._status.market = None
        enabled = [item for item in symbols if item.enabled]
        if not enabled:
            self._status.last_success_at = anchor
            self._status.last_error = None
            self._status.pending_minutes = 0
            self._status.last_duration_seconds = round(monotonic() - started, 6)
            return MaterializeRun()

        groups: dict[tuple[str, date], list[MonitoredSymbol]] = defaultdict(list)
        for item in enabled:
            local_day = (
                market_day
                if market_day is not None
                else anchor.astimezone(MARKET_ZONES[item.market]).date()
            )
            groups[(item.market, local_day)].append(item)

        rows: list[DowMonitorMinuteResult] = []
        inserted_keys: list[MinuteResultKey] = []
        errors: list[tuple[str, str]] = []
        written = 0
        scanned = 0
        remaining = 0
        try:
            notifications = tuple(self._notifications_fn())
        except Exception as exc:
            message = self._safe_error(exc)
            self._status.last_error = message
            return MaterializeRun(error=message)

        for (market, market_day), items in groups.items():
            self._status.market = market
            zone = MARKET_ZONES[market]
            day_start = datetime.combine(
                market_day,
                datetime.min.time(),
                tzinfo=zone,
            ).astimezone(UTC)
            group_missing_count = 0
            try:
                gap = self.find_missing_keys(
                    items,
                    market_day,
                    day_start,
                    anchor,
                    deadline=deadline,
                )
                scanned += gap.scanned_keys
                missing_keys = list(gap.keys)
                group_missing_count = len(missing_keys)
                if not missing_keys:
                    continue
                self._require_budget(deadline)
                capacity = max(0, max_rows - written)
                selected_keys = missing_keys[:capacity]
                remaining += len(missing_keys) - len(selected_keys)
                if not selected_keys:
                    continue
                selected_symbols = sorted({key.symbol for key in selected_keys})
                earliest = min(key.decision_minute for key in selected_keys)
                latest = max(key.decision_minute for key in selected_keys)
                history = self._call_with_deadline(
                    self._source.load_raw_history,
                    selected_symbols,
                    day_start,
                    latest + timedelta(minutes=1),
                    candle_start=earliest - timedelta(days=WARMUP_DAYS),
                    deadline=deadline,
                )
                self._require_budget(deadline)
                selected_set = set(selected_keys)
                contexts = []
                for item in items:
                    self._require_budget(deadline)
                    item_minutes = {
                        key.decision_minute
                        for key in selected_keys
                        if key.symbol == normalize_monitor_symbol(item.symbol)
                    }
                    if not item_minutes:
                        continue
                    contexts.extend(
                        self._call_with_deadline(
                            self._history_builder.build_contexts,
                            history,
                            item,
                            market_day,
                            True,
                            notifications=notifications,
                            decision_minutes=item_minutes,
                            deadline=deadline,
                        )
                    )
                concurrent_existing = self._call_with_deadline(
                    self._repository.existing_keys,
                    selected_symbols,
                    earliest,
                    latest + timedelta(milliseconds=1),
                    deadline=deadline,
                )
                self._require_budget(deadline)
                missing_contexts = []
                for context in contexts:
                    key = MinuteResultKey(
                        market=context.market,
                        symbol=context.symbol,
                        decision_minute=context.decision_minute,
                    )
                    if key in selected_set and key not in concurrent_existing:
                        missing_contexts.append(context)
                group_rows = []
                for context in missing_contexts[: max(0, max_rows - written)]:
                    self._require_budget(deadline)
                    group_rows.append(calculate_minute_result(context))
                if group_rows:
                    group_written = self._call_with_deadline(
                        self._repository.insert_results,
                        group_rows,
                        deadline=deadline,
                    )
                    written += group_written
                    rows.extend(group_rows)
                    inserted_keys.extend(
                        MinuteResultKey(
                            market=row.market,
                            symbol=row.symbol,
                            decision_minute=row.decision_minute,
                        )
                        for row in group_rows[:group_written]
                    )
                if deadline is not None and monotonic() >= deadline:
                    remaining += max(0, len(selected_keys) - len(group_rows))
            except TimeoutError:
                remaining += max(1, group_missing_count)
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
        self._status.pending_minutes = remaining
        self._status.last_written_rows = written
        self._status.scanned_keys = scanned
        self._status.remaining_keys = remaining
        self._status.last_duration_seconds = round(monotonic() - started, 6)
        if error is None:
            self._status.last_success_at = anchor
        return MaterializeRun(
            rows=tuple(rows),
            inserted_keys=tuple(inserted_keys),
            written_rows=written,
            scanned_keys=scanned,
            remaining_keys=remaining,
            error=error,
        )

    def find_missing_keys(
        self,
        items: Sequence[MonitoredSymbol],
        market_day: date,
        start: datetime,
        end: datetime,
        *,
        deadline: float | None = None,
    ) -> MissingMinuteKeys:
        candidates = self._call_with_deadline(
            self._source.candidate_minute_keys,
            items,
            market_day,
            end,
            deadline=deadline,
        )
        self._require_budget(deadline)
        existing = self._call_with_deadline(
            self._repository.existing_keys,
            [item.symbol for item in items],
            start,
            end + timedelta(milliseconds=1),
            deadline=deadline,
        )
        missing = tuple(
            sorted(
                candidates - existing,
                key=lambda key: (key.decision_minute, key.symbol),
            )
        )
        return MissingMinuteKeys(keys=missing, scanned_keys=len(candidates))

    def materialize_checkpoint(
        self,
        *,
        symbol: MonitoredSymbol,
        session_open: datetime,
        window_end: datetime,
        max_rows: int = MAX_CHECKPOINT_ROWS,
    ) -> MaterializeRun:
        if max_rows <= 0:
            raise ValueError("max_rows must be positive")
        if session_open.tzinfo is None or session_open.utcoffset() is None:
            raise ValueError("session_open must be timezone-aware")
        if window_end.tzinfo is None or window_end.utcoffset() is None:
            raise ValueError("window_end must be timezone-aware")
        if window_end <= session_open:
            raise ValueError("window_end must be later than session_open")
        effective_max_rows = min(max_rows, MAX_CHECKPOINT_ROWS)

        try:
            market_day = window_end.astimezone(MARKET_ZONES[symbol.market]).date()
            history = self._source.load_raw_history(
                [symbol.symbol],
                session_open,
                window_end,
                candle_start=session_open - timedelta(days=WARMUP_DAYS),
            )
            candidate_keys = {
                key
                for key in self._history_builder.candidate_keys(
                    history,
                    symbol,
                    market_day,
                )
                if session_open < key.decision_minute <= window_end
            }
            existing = self._repository.existing_keys(
                [symbol.symbol],
                session_open,
                window_end + timedelta(milliseconds=1),
            )
            missing_keys = candidate_keys - existing
            if not missing_keys:
                return MaterializeRun()

            contexts = self._history_builder.build_contexts(
                history,
                symbol,
                market_day,
                True,
                notifications=tuple(self._notifications_fn()),
                decision_minutes={key.decision_minute for key in missing_keys},
            )
            missing_contexts = [
                context
                for context in contexts
                if session_open < context.decision_minute <= window_end
                and MinuteResultKey(
                    market=context.market,
                    symbol=context.symbol,
                    decision_minute=context.decision_minute,
                ) in missing_keys
            ]
            if len(missing_contexts) > effective_max_rows:
                return MaterializeRun(
                    error=MaterializeError(
                        code="BACKFILL_BUDGET_EXCEEDED",
                        message=(
                            f"checkpoint requires {len(missing_contexts)} rows; "
                            f"limit is {effective_max_rows}"
                        ),
                    )
                )

            rows = [calculate_minute_result(context) for context in missing_contexts]
            written = self._repository.insert_results(rows) if rows else 0
            return MaterializeRun(
                rows=tuple(rows),
                inserted_keys=tuple(
                    MinuteResultKey(
                        market=row.market,
                        symbol=row.symbol,
                        decision_minute=row.decision_minute,
                    )
                    for row in rows[:written]
                ),
                written_rows=written,
            )
        except Exception as exc:
            return MaterializeRun(
                error=MaterializeError(
                    code="BACKFILL_FAILED",
                    message=self._safe_error(exc),
                )
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
        **_kwargs,
    ) -> MaterializeRun:
        return MaterializeRun(error=self._status.last_error)

    def status(self) -> MaterializerStatus:
        return self._status.model_copy(deep=True)
