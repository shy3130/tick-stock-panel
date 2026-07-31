from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from threading import Lock
from time import monotonic
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import polars as pl

from app.services.dow_minute_decision import (
    MinuteDecisionContext,
    build_minute_decision,
)
from app.services.dow_monitor_bar_safety import InsufficientDowBars
from app.services.dow_monitor_bars import (
    TIMEFRAME_MINUTES,
    TimeframeBars,
    build_timeframes,
)
from app.services.dow_monitor_client import (
    DowEngineResult,
    DowEngineUnavailable,
    DowLongTermSnapshot,
    DowSnapshot,
)
from app.services.dow_monitor_data import WebStockBatch, market_session_policy
from app.services.dow_monitor_indicators import enrich_dow_chart_bars
from app.services.dow_monitor_models import (
    DowMinuteDecision,
    DowNotification,
    DowTimeframeState,
    HistoryBackfillStatus,
    MonitoredSymbol,
)

try:  # Optional in isolated backend tests; available in the deployed monorepo.
    from longbridge_stock.clickhouse_realtime import (
        fetch_realtime_signal_rows as _fetch_realtime_signal_rows,
    )
except Exception:  # pragma: no cover - exercised by fallback behavior.
    _fetch_realtime_signal_rows = None

logger = logging.getLogger(__name__)

TIMEFRAMES = ("5m", "15m", "30m", "60m", "day")
MAX_PARALLEL_SYMBOLS = 3
CAPITAL_DELAY_THRESHOLD = timedelta(minutes=15)
NotificationIndex = dict[tuple[str, str], list[DowNotification]]


def _completed_bucket_marker(
    symbol: str,
    source_timestamp: datetime,
    minutes: int,
) -> tuple[datetime, int] | None:
    policy = market_session_policy(symbol)
    zone = ZoneInfo(policy.timezone)
    local_source = source_timestamp.astimezone(zone)
    local_time = local_source.time().replace(tzinfo=None)
    completed_at = local_source + timedelta(minutes=1)
    for session_start, session_end in policy.sessions:
        if not (session_start <= local_time < session_end):
            continue
        session_start_at = datetime.combine(
            local_source.date(),
            session_start,
            tzinfo=zone,
        )
        session_end_at = datetime.combine(
            local_source.date(),
            session_end,
            tzinfo=zone,
        )
        elapsed_minutes = max(
            0,
            int(
                (
                    min(completed_at, session_end_at) - session_start_at
                ).total_seconds()
                // 60
            ),
        )
        completed_buckets = elapsed_minutes // minutes
        if completed_at >= session_end_at and elapsed_minutes % minutes:
            completed_buckets += 1
        return session_start_at, completed_buckets
    return None


def _completed_daily_marker(
    symbol: str,
    source_timestamp: datetime,
) -> date | None:
    policy = market_session_policy(symbol)
    zone = ZoneInfo(policy.timezone)
    local_source = source_timestamp.astimezone(zone)
    if local_source.weekday() >= 5:
        return None
    final_close = max(end for _start, end in policy.sessions)
    close_at = datetime.combine(local_source.date(), final_close, tzinfo=zone)
    return (
        local_source.date()
        if local_source + timedelta(minutes=1) >= close_at
        else None
    )


def due_timeframes_for_minute(
    symbol: str,
    source_timestamp: datetime,
    previous_states: dict[str, DowTimeframeState],
) -> tuple[str, ...]:
    """Return only periods whose stable input changed or needs recovery."""
    policy = market_session_policy(symbol)
    zone = ZoneInfo(policy.timezone)
    due: list[str] = []
    for timeframe in TIMEFRAMES:
        previous = previous_states.get(timeframe)
        if (
            previous is None
            or previous.source_timestamp is None
            or previous.freshness_state != "LIVE"
            or bool(previous.snapshot.get("evaluation_error"))
        ):
            due.append(timeframe)
            continue
        previous_local = previous.source_timestamp.astimezone(zone)
        if timeframe == "day":
            completed_day = _completed_daily_marker(symbol, source_timestamp)
            if completed_day is None:
                continue
            previous_is_final = (
                previous.snapshot.get("bar_completion") == "FINAL"
                and not bool(previous.snapshot.get("provisional", False))
            )
            if (
                previous_local.date() < completed_day
                or (
                    previous_local.date() == completed_day
                    and not previous_is_final
                )
            ):
                due.append(timeframe)
            continue
        minutes = TIMEFRAME_MINUTES[timeframe]
        current_marker = _completed_bucket_marker(symbol, source_timestamp, minutes)
        previous_marker = _completed_bucket_marker(
            symbol,
            previous.source_timestamp,
            minutes,
        )
        if current_marker is None or current_marker[1] <= 0:
            continue
        if (
            previous_marker is None
            or previous_marker[0] != current_marker[0]
            or previous_marker[1] < current_marker[1]
        ):
            due.append(timeframe)
    return tuple(due)


def _monitor_symbol_identity(symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    if normalized.endswith(".HK"):
        code = normalized[:-3]
        if code.isdigit():
            return f"{int(code)}.HK"
    return normalized


@dataclass(frozen=True)
class ActivationState:
    active: bool
    family: str | None
    structure_id: str | None
    activation_sequence: int


@dataclass(frozen=True)
class EventTransition:
    next: ActivationState
    notify: bool


def signal_family(action_code: str) -> str | None:
    if action_code in {"OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"}:
        return action_code
    return None


def notification_side(action_code: str) -> Literal["BUY", "SELL", "RISK"] | None:
    if action_code == "OPEN_LONG":
        return "BUY"
    if action_code == "OPEN_SHORT":
        return "SELL"
    if action_code in {"CLOSE_LONG", "CLOSE_SHORT"}:
        return "RISK"
    return None


def long_term_signal_family(snapshot: DowLongTermSnapshot) -> str | None:
    if (
        snapshot.bar_completion != "FINAL"
        or snapshot.provisional
        or snapshot.signal_stage not in {"TRIGGER", "CONFIRMED"}
        or snapshot.line_id is None
        or not snapshot.line_id.strip()
    ):
        return None
    if snapshot.operation == "买入触发":
        return "LONG_TERM_BUY"
    if snapshot.operation == "卖出触发":
        return "LONG_TERM_SELL"
    return None


def _finite_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _finite_values(values: list[float | None]) -> list[float]:
    return [value for value in values if value is not None and math.isfinite(value)]


def _mean_last(values: list[float | None], window: int) -> float | None:
    sample = _finite_values(values[-window:])
    if len(sample) < window:
        return None
    return sum(sample) / len(sample)


def _max_last(values: list[float | None], window: int) -> float | None:
    sample = _finite_values(values[-window:])
    return max(sample) if sample else None


def _min_last(values: list[float | None], window: int) -> float | None:
    sample = _finite_values(values[-window:])
    return min(sample) if sample else None


def _momentum(values: list[float | None], window: int) -> float | None:
    if len(values) <= window:
        return None
    current = values[-1]
    base = values[-1 - window]
    if current is None or base in (None, 0):
        return None
    return current / base - 1.0


def _rsi_last(values: list[float | None], window: int) -> float | None:
    sample = _finite_values(values)
    if len(sample) <= window:
        return None
    changes = [sample[index] - sample[index - 1] for index in range(1, len(sample))]
    recent = changes[-window:]
    gains = [max(change, 0.0) for change in recent]
    losses = [abs(min(change, 0.0)) for change in recent]
    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _macd_hist(values: list[float | None]) -> float | None:
    sample = _finite_values(values)
    if len(sample) < 35:
        return None
    dif = [fast - slow for fast, slow in zip(_ema(sample, 12), _ema(sample, 26), strict=False)]
    dea = _ema(dif, 9)
    if not dea:
        return None
    return dif[-1] - dea[-1]


def _clickhouse_ident(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError(f"unsafe ClickHouse identifier: {value!r}")
    return value


def _clickhouse_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _clickhouse_symbol_tuple(symbols: list[str]) -> str:
    return "(" + ",".join(_clickhouse_string(symbol) for symbol in symbols) + ")"


def _clickhouse_query_json_each_row(sql: str) -> list[dict]:
    base_url = os.getenv("CLICKHOUSE_URL", "http://127.0.0.1:8123").rstrip("/")
    encoded = urllib.parse.quote(sql + " format JSONEachRow")
    request = urllib.request.Request(f"{base_url}/?query={encoded}", method="POST")
    user = os.getenv("CLICKHOUSE_USER", "default")
    password = os.getenv("CLICKHOUSE_PASSWORD", "")
    if user:
        request.add_header("X-ClickHouse-User", user)
    if password:
        request.add_header("X-ClickHouse-Key", password)
    timeout = float(os.getenv("CLICKHOUSE_READ_TIMEOUT_SECONDS", "5"))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def _parse_clickhouse_time(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _transition_values(
    previous: ActivationState | None,
    family: str | None,
    structure_id: str | None,
) -> EventTransition:
    active = family is not None and structure_id is not None
    if not active:
        return EventTransition(
            next=ActivationState(
                active=False,
                family=None,
                structure_id=None,
                activation_sequence=previous.activation_sequence if previous else 0,
            ),
            notify=False,
        )
    same = (
        previous is not None
        and previous.active
        and previous.family == family
        and previous.structure_id == structure_id
    )
    sequence = (
        previous.activation_sequence
        if same
        else (previous.activation_sequence + 1 if previous else 1)
    )
    return EventTransition(
        next=ActivationState(True, family, structure_id, sequence),
        notify=not same,
    )


def transition_event(
    previous: ActivationState | None,
    snapshot: DowSnapshot,
) -> EventTransition:
    family = signal_family(snapshot.action_code)
    structure_id = snapshot.line_id
    return _transition_values(previous, family, structure_id)


class DowMonitorService:
    def __init__(
        self,
        store,
        data_gateway,
        dow_client,
        daily_loader: Callable[[str, datetime], pl.DataFrame],
        poll_seconds: float = 15,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        minute_result_materializer=None,
        minute_result_scheduler=None,
        history_status_reader=None,
        half_hour_ai_repository=None,
    ) -> None:
        self.store = store
        self._data_gateway = data_gateway
        self._dow_client = dow_client
        self._daily_loader = daily_loader
        self.poll_seconds = poll_seconds
        self.max_parallel_symbols = MAX_PARALLEL_SYMBOLS
        self._now_fn = now_fn
        self._minute_result_materializer = minute_result_materializer
        self._minute_result_scheduler = minute_result_scheduler
        if (
            self._minute_result_scheduler is None
            and self._minute_result_materializer is not None
        ):
            from app.services.dow_monitor_minute_result_scheduler import (
                MinuteResultBackfillScheduler,
            )

            self._minute_result_scheduler = MinuteResultBackfillScheduler(
                self._minute_result_materializer,
                now_fn=self._now,
            )
        self._history_status_reader = history_status_reader
        self._half_hour_ai_repository = half_hour_ai_repository
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_started_at: datetime | None = None
        self._last_completed_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_success_by_symbol: dict[str, datetime] = {}
        self._last_error: str | None = None
        self._errors: dict[str, str] = {}
        self._latest_quotes_by_symbol: dict[str, dict] = {}
        self._next_day_direction_by_symbol: dict[str, dict] = {}
        self._metrics_lock = Lock()
        self._evaluated_symbols: list[str] = []
        self._last_evaluation_request_count = 0
        self._last_cache_skip_count = 0
        self._last_evaluated_timeframes: dict[str, list[str]] = {}
        self._last_cycle_duration_seconds: float | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        if self._minute_result_scheduler is not None:
            await self._minute_result_scheduler.start()
        self._task = asyncio.create_task(self._loop(), name="dow-monitor")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            if self._minute_result_scheduler is not None:
                await self._minute_result_scheduler.stop(timeout_seconds=5.0)
            return
        self._stop.set()
        try:
            await task
        finally:
            self._task = None
            if self._minute_result_scheduler is not None:
                await self._minute_result_scheduler.stop(timeout_seconds=5.0)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            started = monotonic()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dow monitor cycle failed")
            delay = max(0.0, self.poll_seconds - (monotonic() - started))
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def run_once(self) -> None:
        cycle_started = monotonic()
        now = self._now()
        self._last_started_at = now
        with self._metrics_lock:
            self._evaluated_symbols = []
            self._last_evaluation_request_count = 0
            self._last_cache_skip_count = 0
            self._last_evaluated_timeframes = {}
        enabled = [
            item for item in await asyncio.to_thread(self.store.list_symbols) if item.enabled
        ]
        if not enabled:
            self._last_error = None
            self._last_success_at = now
            self._last_completed_at = self._now()
            self._last_cycle_duration_seconds = round(monotonic() - cycle_started, 6)
            return
        if self._minute_result_scheduler is not None:
            try:
                self._minute_result_scheduler.request(enabled, now)
            except Exception:
                logger.exception("dow monitor minute result scheduling failed")

        starts_by_symbol, cold_symbols = await asyncio.to_thread(
            self._fetch_plan,
            enabled,
            now,
        )
        try:
            batch = await asyncio.to_thread(
                self._data_gateway.fetch_since,
                starts_by_symbol,
                now,
            )
        except Exception as exc:
            message = str(exc)
            self._last_error = message
            for item in enabled:
                self._errors[item.symbol] = message
                await asyncio.to_thread(self._mark_all, item, "STALE_DATA", now)
            self._last_completed_at = self._now()
            self._last_cycle_duration_seconds = round(monotonic() - cycle_started, 6)
            return
        self._retain_latest_quotes(batch.quotes)

        cold_live_symbols = [
            item.symbol
            for item in enabled
            if item.symbol in cold_symbols
            and (freshness := batch.freshness_by_symbol.get(item.symbol)) is not None
            and freshness.state == "LIVE"
        ]
        history_rows = pl.DataFrame()
        if cold_live_symbols:
            try:
                history = await asyncio.to_thread(
                    self._data_gateway.load_history,
                    cold_live_symbols,
                    now,
                )
            except Exception as exc:
                logger.warning(
                    "dow monitor history warmup unavailable; evaluating only due "
                    "timeframes with current inputs: %s",
                    exc,
                )
            else:
                history_rows = history.minute_rows

        notification_index = await asyncio.to_thread(self._load_notification_index)
        intraday_capital = await asyncio.to_thread(
            self._intraday_capital_by_symbol,
            [item.symbol for item in enabled],
        )
        semaphore = asyncio.Semaphore(self.max_parallel_symbols)

        async def evaluate_one(
            item: MonitoredSymbol,
        ) -> tuple[MonitoredSymbol, str | None, bool]:
            symbol_notification_index = {
                key: list(values)
                for key, values in notification_index.items()
                if key[0] == item.symbol
            }
            try:
                async with semaphore:
                    error, symbol_succeeded = await asyncio.to_thread(
                        self._evaluate_symbol,
                        item,
                        batch,
                        now,
                        symbol_notification_index,
                        item.symbol in cold_symbols,
                        history_rows,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = str(exc)
                symbol_succeeded = False
                await asyncio.to_thread(
                    self._mark_all_analysis_failures,
                    item,
                    exc,
                    now,
                )
                logger.exception("dow monitor symbol failed: %s", item.symbol)
            if symbol_succeeded:
                self._last_success_by_symbol[item.symbol] = now
                refreshed_decision = await asyncio.to_thread(
                    self._refresh_minute_decision,
                    item,
                    batch.minute_rows,
                    intraday_capital.get(_monitor_symbol_identity(item.symbol)),
                    now,
                )
                if refreshed_decision is not None:
                    await self._submit_live_minute_result(
                        item=item,
                        decision=refreshed_decision,
                        minute_rows=batch.minute_rows,
                        intraday_capital=intraday_capital.get(
                            _monitor_symbol_identity(item.symbol)
                        ),
                        notifications=symbol_notification_index,
                        now=now,
                    )
            return item, error, symbol_succeeded

        results = await asyncio.gather(*(evaluate_one(item) for item in enabled))
        with self._metrics_lock:
            self._evaluated_symbols = [
                item.symbol
                for item in enabled
                if self._last_evaluated_timeframes.get(item.symbol)
            ]
        any_success = False
        cycle_errors: list[str] = []
        for item, error, symbol_succeeded in results:
            any_success = any_success or symbol_succeeded
            if error is None:
                self._errors.pop(item.symbol, None)
            else:
                self._errors[item.symbol] = error
                cycle_errors.append(f"{item.symbol}: {error}")

        self._last_error = "; ".join(cycle_errors) or None
        if any_success:
            self._last_success_at = now
        self._last_completed_at = self._now()
        self._last_cycle_duration_seconds = round(monotonic() - cycle_started, 6)

    def _evaluate_symbol(
        self,
        item: MonitoredSymbol,
        batch: WebStockBatch,
        now: datetime,
        notification_index: NotificationIndex,
        cold_start: bool,
        history_rows: pl.DataFrame,
    ) -> tuple[str | None, bool]:
        freshness = batch.freshness_by_symbol.get(item.symbol)
        if freshness is None or freshness.state != "LIVE":
            self._mark_all(item, "STALE_DATA", now)
            return (
                (
                    freshness.reason
                    if freshness is not None and freshness.reason is not None
                    else "WebStock data is stale"
                ),
                False,
        )

        daily_rows = self._daily_loader(item.symbol, now)
        self._update_next_day_direction(item, daily_rows)
        canonical_minutes = (
            self._merge_warmup_minutes(
                item,
                history_rows,
                batch.minute_rows,
            )
            if cold_start
            else batch.minute_rows
        )
        previous_states = {
            state.timeframe: state
            for state in self.store.list_states()
            if state.symbol == item.symbol
        }
        latest = self._latest_completed_minute(item, batch.minute_rows, now)
        due_timeframes = (
            due_timeframes_for_minute(
                item.symbol,
                latest[1],
                previous_states,
            )
            if latest is not None
            else tuple(
                timeframe
                for timeframe in TIMEFRAMES
                if timeframe not in previous_states
                or previous_states[timeframe].freshness_state != "LIVE"
            )
        )
        with self._metrics_lock:
            self._last_cache_skip_count += len(TIMEFRAMES) - len(due_timeframes)
            self._last_evaluated_timeframes[item.symbol] = list(due_timeframes)
        frames_by_cutoff: dict[str | None, dict[str, TimeframeBars]] = {}
        errors: list[str] = []
        successes = 0
        for timeframe in due_timeframes:
            previous_state = previous_states.get(timeframe)
            cutoff = (
                previous_state.source_timestamp
                if previous_state is not None
                else None
            )
            cutoff_key = cutoff.isoformat() if cutoff is not None else None
            if cutoff_key not in frames_by_cutoff:
                minute_rows = self._incremental_minutes(item, canonical_minutes, cutoff)
                frames_by_cutoff[cutoff_key] = build_timeframes(
                    item.symbol,
                    minute_rows,
                    daily_rows,
                    now,
                )
            frame = frames_by_cutoff[cutoff_key][timeframe]
            bars, completion = self._merge_evaluation_bars(
                item,
                timeframe,
                previous_state,
                frame,
                now,
            )
            try:
                with self._metrics_lock:
                    self._last_evaluation_request_count += 1
                result = self._dow_client.evaluate(
                    item.symbol,
                    timeframe,
                    bars,
                    completion,
                    now,
                )
            except InsufficientDowBars as exc:
                self._mark_timeframe_failure(
                    item,
                    timeframe,
                    exc,
                    now,
                )
                errors.append(
                    f"{timeframe}: HISTORY_INCOMPLETE:"
                    f"VALID_BARS_{exc.valid_bars}_OF_{exc.required_bars}"
                )
                continue
            except Exception as exc:
                self._mark_timeframe_failure(
                    item,
                    timeframe,
                    exc,
                    now,
                )
                errors.append(str(exc))
                if not isinstance(exc, DowEngineUnavailable):
                    logger.exception(
                        "dow monitor timeframe failed: %s %s",
                        item.symbol,
                        timeframe,
                    )
                continue
            self._save_result(
                item,
                timeframe,
                frame,
                result,
                now,
                notification_index,
            )
            successes += 1
        decision_ready = successes > 0 or not due_timeframes or all(
            timeframe in previous_states
            and bool(previous_states[timeframe].snapshot)
            for timeframe in ("5m", "15m")
        )
        return "; ".join(dict.fromkeys(errors)) or None, decision_ready

    def _save_result(
        self,
        item: MonitoredSymbol,
        timeframe: str,
        frame: TimeframeBars,
        result: DowEngineResult,
        now: datetime,
        notification_index: NotificationIndex,
    ) -> None:
        previous_state = self.store.get_state(item.symbol, timeframe)
        previous = self._activation_from_state(
            item.symbol,
            timeframe,
            previous_state,
            result.snapshot,
            notification_index,
        )
        transition = transition_event(previous, result.snapshot)
        long_family = long_term_signal_family(result.long_term)
        long_previous = self._long_term_activation_from_state(
            item.symbol,
            timeframe,
            previous_state,
            result.long_term,
            notification_index,
        )
        long_transition = _transition_values(
            long_previous,
            long_family,
            (
                result.long_term.line_id.strip()
                if long_family is not None and result.long_term.line_id is not None
                else None
            ),
        )
        engine_payload = result.model_dump(mode="json", by_alias=True)
        chart = {
            "bars": enrich_dow_chart_bars(item.symbol, engine_payload["bars"]),
            "lines": deepcopy(engine_payload["lines"]),
            "signals": deepcopy(engine_payload["signals"]),
            "longTerm": deepcopy(engine_payload["longTerm"]),
        }
        if isinstance(engine_payload.get("turning"), dict):
            chart["turning"] = deepcopy(engine_payload["turning"])
        if isinstance(engine_payload.get("headShoulders"), dict):
            chart["headShoulders"] = deepcopy(engine_payload["headShoulders"])
        timestamps = [
            value
            for value in (
                previous_state.source_timestamp if previous_state else None,
                frame.source_timestamp,
            )
            if value is not None
        ]
        source_timestamp = max(timestamps, default=None)

        events = (
            (
                transition,
                notification_side(result.snapshot.action_code),
                result.snapshot.action,
                result.snapshot.phase,
            ),
            (
                long_transition,
                (
                    "BUY"
                    if long_transition.next.family == "LONG_TERM_BUY"
                    else "SELL"
                    if long_transition.next.family == "LONG_TERM_SELL"
                    else None
                ),
                result.long_term.operation,
                result.long_term.pattern_name,
            ),
        )
        for event_transition, side, action_name, shape_name in events:
            if event_transition.notify and side is not None:
                current = engine_payload["bars"][-1]
                current_ohlc = {
                    key: deepcopy(current[key])
                    for key in (
                        "timestamp",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                    )
                }
                event_key = "|".join(
                    (
                        item.symbol,
                        timeframe,
                        event_transition.next.family or "",
                        event_transition.next.structure_id or "",
                        str(event_transition.next.activation_sequence),
                    )
                )
                notification = DowNotification(
                    notification_id=uuid4().hex,
                    event_key=event_key,
                    symbol=item.symbol,
                    market=item.market,
                    timeframe=timeframe,
                    side=side,
                    action_name=action_name,
                    shape_name=shape_name,
                    triggered_at=result.evaluated_at,
                    trigger_price=float(current["close"]),
                    snapshot_payload=deepcopy(
                        {
                            "engine": engine_payload,
                            "current_ohlc": current_ohlc,
                            "source_timestamp": (
                                source_timestamp.isoformat()
                                if source_timestamp is not None
                                else None
                            ),
                            "activation": asdict(event_transition.next),
                        }
                    ),
                )
                if self.store.append_notification(notification):
                    notification_index.setdefault((item.symbol, timeframe), []).append(notification)

        self.store.save_state(
            DowTimeframeState(
                symbol=item.symbol,
                market=item.market,
                timeframe=timeframe,
                freshness_state="LIVE",
                source_timestamp=source_timestamp,
                snapshot=deepcopy(engine_payload["snapshot"]),
                chart=chart,
                updated_at=now,
            )
        )

    def _activation_from_state(
        self,
        symbol: str,
        timeframe: str,
        state: DowTimeframeState | None,
        current: DowSnapshot,
        notification_index: NotificationIndex,
    ) -> ActivationState | None:
        sequence = self._maximum_sequence(symbol, timeframe, notification_index)
        if state is None:
            recorded = self._recorded_activation_after_state(
                symbol,
                timeframe,
                None,
                current,
                notification_index,
            )
            return recorded or (ActivationState(False, None, None, sequence) if sequence else None)
        family = signal_family(str(state.snapshot.get("action_code") or ""))
        structure_id = state.snapshot.get("line_id")
        active = family is not None and isinstance(structure_id, str) and bool(structure_id)
        previous = ActivationState(
            active=active,
            family=family if active else None,
            structure_id=structure_id if active else None,
            activation_sequence=sequence,
        )
        if active:
            return previous
        recorded = self._recorded_activation_after_state(
            symbol,
            timeframe,
            state,
            current,
            notification_index,
        )
        return recorded or previous

    def _long_term_activation_from_state(
        self,
        symbol: str,
        timeframe: str,
        state: DowTimeframeState | None,
        current: DowLongTermSnapshot,
        notification_index: NotificationIndex,
    ) -> ActivationState | None:
        sequence = self._maximum_sequence(
            symbol,
            timeframe,
            notification_index,
            long_term=True,
        )
        current_family = long_term_signal_family(current)
        current_structure = (
            current.line_id.strip()
            if current_family is not None and current.line_id is not None
            else None
        )
        if state is None:
            recorded = self._recorded_activation_values(
                symbol,
                timeframe,
                None,
                current_family,
                current_structure,
                notification_index,
            )
            return recorded or (ActivationState(False, None, None, sequence) if sequence else None)
        raw = state.chart.get("longTerm")
        stored_family = None
        stored_structure = None
        if isinstance(raw, dict):
            operation = raw.get("operation")
            if (
                raw.get("bar_completion") == "FINAL"
                and raw.get("provisional") is False
                and raw.get("signal_stage") in {"TRIGGER", "CONFIRMED"}
                and isinstance(raw.get("line_id"), str)
                and bool(raw["line_id"].strip())
            ):
                stored_family = (
                    "LONG_TERM_BUY"
                    if operation == "买入触发"
                    else "LONG_TERM_SELL"
                    if operation == "卖出触发"
                    else None
                )
                stored_structure = raw["line_id"].strip() if stored_family is not None else None
        previous = ActivationState(
            active=stored_family is not None and stored_structure is not None,
            family=stored_family,
            structure_id=stored_structure,
            activation_sequence=sequence,
        )
        if previous.active:
            return previous
        recorded = self._recorded_activation_values(
            symbol,
            timeframe,
            state,
            current_family,
            current_structure,
            notification_index,
        )
        return recorded or previous

    def _recorded_activation_after_state(
        self,
        symbol: str,
        timeframe: str,
        state: DowTimeframeState | None,
        current: DowSnapshot,
        notification_index: NotificationIndex,
    ) -> ActivationState | None:
        family = signal_family(current.action_code)
        structure_id = current.line_id
        return self._recorded_activation_values(
            symbol,
            timeframe,
            state,
            family,
            structure_id,
            notification_index,
        )

    def _recorded_activation_values(
        self,
        symbol: str,
        timeframe: str,
        state: DowTimeframeState | None,
        family: str | None,
        structure_id: str | None,
        notification_index: NotificationIndex,
    ) -> ActivationState | None:
        if family is None or structure_id is None:
            return None
        for notification in notification_index.get((symbol, timeframe), []):
            activation = notification.snapshot_payload.get("activation")
            notification_family = activation.get("family") if isinstance(activation, dict) else None
            notification_structure = (
                activation.get("structure_id") if isinstance(activation, dict) else None
            )
            if notification_family is None or notification_structure is None:
                engine = notification.snapshot_payload.get("engine")
                snapshot = engine.get("snapshot") if isinstance(engine, dict) else None
                if isinstance(snapshot, dict):
                    notification_family = signal_family(str(snapshot.get("action_code") or ""))
                    notification_structure = snapshot.get("line_id")
            notification_source = self._notification_source_timestamp(notification)
            if state is None:
                notification_follows_state = True
            elif notification_source is not None and state.source_timestamp is not None:
                notification_follows_state = notification_source >= state.source_timestamp
            else:
                notification_follows_state = notification.triggered_at >= state.updated_at
            if (
                notification_family != family
                or notification_structure != structure_id
                or not notification_follows_state
            ):
                continue
            try:
                notification_sequence = (
                    int(activation["activation_sequence"])
                    if isinstance(activation, dict)
                    else int(notification.event_key.rsplit("|", 1)[1])
                )
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            return ActivationState(
                active=True,
                family=family,
                structure_id=structure_id,
                activation_sequence=notification_sequence,
            )
        return None

    @staticmethod
    def _notification_source_timestamp(
        notification: DowNotification,
    ) -> datetime | None:
        value = notification.snapshot_payload.get("source_timestamp")
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None

    def _maximum_sequence(
        self,
        symbol: str,
        timeframe: str,
        notification_index: NotificationIndex,
        *,
        long_term: bool = False,
    ) -> int:
        maximum = 0
        for notification in notification_index.get((symbol, timeframe), []):
            activation = notification.snapshot_payload.get("activation")
            family = activation.get("family") if isinstance(activation, dict) else None
            if not isinstance(family, str):
                parts = notification.event_key.split("|")
                family = parts[2] if len(parts) == 5 else None
            if isinstance(family, str) and family.startswith("LONG_TERM_") != long_term:
                continue
            if isinstance(activation, dict):
                try:
                    maximum = max(maximum, int(activation["activation_sequence"]))
                    continue
                except (KeyError, TypeError, ValueError):
                    pass
            try:
                maximum = max(maximum, int(notification.event_key.rsplit("|", 1)[1]))
            except (IndexError, ValueError):
                continue
        return maximum

    def _load_notification_index(self) -> NotificationIndex:
        index: NotificationIndex = {}
        for notification in self.store.list_notifications(limit=1_000_000):
            index.setdefault((notification.symbol, notification.timeframe), []).append(notification)
        return index

    def _fetch_plan(
        self,
        enabled: list[MonitoredSymbol],
        now: datetime,
    ) -> tuple[dict[str, datetime], set[str]]:
        starts: dict[str, datetime] = {}
        cold_symbols: set[str] = set()
        for item in enabled:
            timestamps: list[datetime] = []
            for timeframe in TIMEFRAMES:
                state = self.store.get_state(item.symbol, timeframe)
                if state is None or state.source_timestamp is None:
                    cold_symbols.add(item.symbol)
                    policy = market_session_policy(item.symbol)
                    zone = ZoneInfo(policy.timezone)
                    local_now = now.astimezone(zone)
                    local_midnight = local_now.replace(
                        hour=0,
                        minute=0,
                        second=0,
                        microsecond=0,
                    )
                    starts[item.symbol] = local_midnight.astimezone(UTC)
                    break
                timestamps.append(state.source_timestamp)
            else:
                starts[item.symbol] = min(timestamps)
        return starts, cold_symbols

    def _merge_warmup_minutes(
        self,
        item: MonitoredSymbol,
        history_rows: pl.DataFrame,
        live_rows: pl.DataFrame,
    ) -> pl.DataFrame:
        available = [frame for frame in (history_rows, live_rows) if not frame.is_empty()]
        if not available:
            return pl.DataFrame()
        combined = pl.concat(available, how="diagonal_relaxed")
        zone = ZoneInfo(market_session_policy(item.symbol).timezone)
        deduplicated: dict[tuple[str, datetime], dict] = {}
        for row in combined.to_dicts():
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol != item.symbol:
                continue
            value = row.get("datetime")
            if value is None:
                continue
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            local = (
                parsed.astimezone(zone).replace(tzinfo=None)
                if parsed.tzinfo is not None
                else parsed
            )
            deduplicated[(symbol, local)] = row
        if not deduplicated:
            return combined.head(0)
        return pl.DataFrame(list(deduplicated.values()), schema=combined.schema)

    def _incremental_minutes(
        self,
        item: MonitoredSymbol,
        minute_rows: pl.DataFrame,
        reliable: datetime | None,
    ) -> pl.DataFrame:
        if reliable is None or minute_rows.is_empty():
            return minute_rows

        zone = ZoneInfo(market_session_policy(item.symbol).timezone)
        reliable_local = reliable.astimezone(zone).replace(tzinfo=None)
        rows = []
        for row in minute_rows.to_dicts():
            if str(row.get("symbol") or "").strip().upper() != item.symbol:
                continue
            value = row.get("datetime")
            if value is None:
                continue
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            local = (
                parsed.astimezone(zone).replace(tzinfo=None)
                if parsed.tzinfo is not None
                else parsed
            )
            if local > reliable_local:
                rows.append(row)
        return pl.DataFrame(rows, schema=minute_rows.schema) if rows else minute_rows.head(0)

    def _merge_evaluation_bars(
        self,
        item: MonitoredSymbol,
        timeframe: str,
        previous: DowTimeframeState | None,
        frame: TimeframeBars,
        now: datetime,
    ) -> tuple[list[dict], str]:
        current = deepcopy(frame.all_bars)
        if previous is None:
            return current, frame.completion

        historical = previous.chart.get("bars")
        if not isinstance(historical, list) or not historical:
            return current, frame.completion
        if not current:
            completion = self._historical_completion(
                item,
                timeframe,
                previous,
                historical,
                now,
            )
            return deepcopy(historical), completion

        merged = {
            str(bar["timestamp"]): deepcopy(bar)
            for bar in historical
            if isinstance(bar, dict) and bar.get("timestamp") is not None
        }
        latest_historical = max(merged, default=None)
        previous_forming = previous.snapshot.get("bar_completion") == "FORMING"
        for bar in current:
            timestamp = str(bar["timestamp"])
            old = merged.get(timestamp)
            if old is not None and previous_forming and timestamp == latest_historical:
                combined = {
                    "timestamp": timestamp,
                    "open": float(old["open"]),
                    "high": max(float(old["high"]), float(bar["high"])),
                    "low": min(float(old["low"]), float(bar["low"])),
                    "close": float(bar["close"]),
                    "volume": float(old.get("volume") or 0.0) + float(bar.get("volume") or 0.0),
                }
                merged[timestamp] = combined
            else:
                merged[timestamp] = deepcopy(bar)
        return [merged[key] for key in sorted(merged)], frame.completion

    @staticmethod
    def _historical_completion(
        item: MonitoredSymbol,
        timeframe: str,
        previous: DowTimeframeState,
        historical: list[dict],
        now: datetime,
    ) -> str:
        prior = str(previous.snapshot.get("bar_completion") or "FINAL")
        if prior == "FINAL":
            return "FINAL"
        last = historical[-1]
        value = last.get("timestamp") if isinstance(last, dict) else None
        if value is None:
            return prior

        policy = market_session_policy(item.symbol)
        zone = ZoneInfo(policy.timezone)
        now_local = now.astimezone(zone)
        parsed = datetime.fromisoformat(str(value))
        if timeframe == "day":
            bar_date = parsed.date()
            close = datetime.combine(bar_date, policy.sessions[-1][1], tzinfo=zone)
            return "FINAL" if now_local >= close else prior

        parsed = parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)
        minutes = TIMEFRAME_MINUTES[timeframe]
        segment_end = next(
            (
                end
                for start, end in policy.sessions
                if start <= parsed.time().replace(tzinfo=None) < end
            ),
            None,
        )
        if segment_end is None:
            return prior
        session_end = datetime.combine(parsed.date(), segment_end, tzinfo=zone)
        bucket_end = min(parsed + timedelta(minutes=minutes), session_end)
        return "FINAL" if now_local >= bucket_end else prior

    def _mark_all(
        self,
        item: MonitoredSymbol,
        freshness_state: Literal["STALE_DATA", "ANALYSIS_PAUSED"],
        now: datetime,
    ) -> None:
        for timeframe in TIMEFRAMES:
            self._mark_one(item, timeframe, freshness_state, now)

    def _mark_all_analysis_failures(
        self,
        item: MonitoredSymbol,
        error: Exception,
        now: datetime,
    ) -> None:
        for timeframe in TIMEFRAMES:
            self._mark_timeframe_failure(item, timeframe, error, now)

    def _mark_timeframe_failure(
        self,
        item: MonitoredSymbol,
        timeframe: str,
        error: Exception,
        now: datetime,
    ) -> None:
        previous = self.store.get_state(item.symbol, timeframe)
        if (
            previous is None
            or previous.source_timestamp is None
            or not previous.snapshot
        ):
            self._mark_one(item, timeframe, "ANALYSIS_PAUSED", now)
            return
        snapshot = deepcopy(previous.snapshot)
        snapshot["evaluation_error"] = str(error).replace("\n", " ")[:500]
        snapshot["evaluation_failed_at"] = now.isoformat()
        self.store.save_state(
            previous.model_copy(
                update={
                    "freshness_state": "LIVE",
                    "snapshot": snapshot,
                }
            )
        )

    def _mark_one(
        self,
        item: MonitoredSymbol,
        timeframe: str,
        freshness_state: Literal["STALE_DATA", "ANALYSIS_PAUSED"],
        now: datetime,
    ) -> None:
        previous = self.store.get_state(item.symbol, timeframe)
        self.store.save_state(
            DowTimeframeState(
                symbol=item.symbol,
                market=item.market,
                timeframe=timeframe,
                freshness_state=freshness_state,
                source_timestamp=previous.source_timestamp if previous else None,
                snapshot=deepcopy(previous.snapshot) if previous else {},
                chart=deepcopy(previous.chart) if previous else {},
                updated_at=now,
            )
        )

    @staticmethod
    def _market_is_open(item: MonitoredSymbol, now: datetime) -> bool:
        policy = market_session_policy(item.symbol)
        local_now = now.astimezone(ZoneInfo(policy.timezone))
        if local_now.weekday() >= 5:
            return False
        return any(start <= local_now.time() < end for start, end in policy.sessions)

    @staticmethod
    def _latest_completed_minute(
        item: MonitoredSymbol,
        rows: pl.DataFrame,
        now: datetime,
    ) -> tuple[dict, datetime] | None:
        if rows.is_empty():
            return None
        policy = market_session_policy(item.symbol)
        zone = ZoneInfo(policy.timezone)
        local_now = now.astimezone(zone)
        latest: tuple[dict, datetime] | None = None
        identity = _monitor_symbol_identity(item.symbol)
        for row in rows.to_dicts():
            if _monitor_symbol_identity(row.get("symbol") or "") != identity:
                continue
            value = row.get("datetime")
            if value is None:
                continue
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            local_start = (
                parsed.astimezone(zone)
                if parsed.tzinfo is not None
                else parsed.replace(tzinfo=zone)
            ).replace(second=0, microsecond=0)
            if local_start.weekday() >= 5:
                continue
            if not any(
                start <= local_start.time() < end
                for start, end in policy.sessions
            ):
                continue
            if local_start + timedelta(minutes=1) > local_now:
                continue
            if latest is None or local_start > latest[1]:
                latest = (row, local_start)
        return latest

    def _refresh_minute_decision(
        self,
        item: MonitoredSymbol,
        minute_rows: pl.DataFrame,
        intraday_capital: dict | None,
        now: datetime,
    ) -> DowMinuteDecision | None:
        if not self._market_is_open(item, now):
            return
        latest = self._latest_completed_minute(item, minute_rows, now)
        if latest is None:
            return
        latest_row, source_timestamp = latest
        decision_minute = source_timestamp + timedelta(minutes=1)
        previous = self.store.get_minute_decision(item.symbol)
        if previous is not None and previous.decision_minute >= decision_minute:
            return

        trends: dict[str, str] = {}
        operations: dict[str, str] = {}
        state_bars: list[dict] = []
        for timeframe in TIMEFRAMES:
            state = self.store.get_state(item.symbol, timeframe)
            if state is None:
                continue
            long_term = state.chart.get("longTerm")
            if isinstance(long_term, dict):
                trend = long_term.get("trendDirection") or long_term.get("trend_direction")
                operation = long_term.get("operation")
                if isinstance(trend, str):
                    trends[timeframe] = trend
                if isinstance(operation, str):
                    operations[timeframe] = operation
            if timeframe == "15m" and isinstance(state.chart.get("bars"), list):
                state_bars = [
                    bar for bar in state.chart["bars"][-20:] if isinstance(bar, dict)
                ]

        lows = [
            float(bar["low"])
            for bar in state_bars
            if isinstance(bar.get("low"), (int, float))
        ]
        highs = [
            float(bar["high"])
            for bar in state_bars
            if isinstance(bar.get("high"), (int, float))
        ]
        identity = _monitor_symbol_identity(item.symbol)
        completed_volumes: list[float] = []
        for row in minute_rows.to_dicts():
            if _monitor_symbol_identity(row.get("symbol") or "") != identity:
                continue
            volume = row.get("volume")
            if not isinstance(volume, (int, float)):
                continue
            value = row.get("datetime")
            if value is None:
                continue
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            zone = ZoneInfo(market_session_policy(item.symbol).timezone)
            local_start = (
                parsed.astimezone(zone)
                if parsed.tzinfo is not None
                else parsed.replace(tzinfo=zone)
            ).replace(second=0, microsecond=0)
            if local_start <= source_timestamp:
                completed_volumes.append(float(volume))
        recent = completed_volumes[-21:-1]
        recent_average_volume = (
            sum(recent) / len(recent)
            if recent
            else completed_volumes[-1]
            if completed_volumes
            else None
        )
        capital = intraday_capital or {}
        snapshot = build_minute_decision(
            MinuteDecisionContext(
                symbol=item.symbol,
                market=item.market,
                decision_minute=decision_minute,
                source_timestamp=source_timestamp,
                trends=trends,
                operations=operations,
                completed_minute=True,
                latest_close=(
                    float(latest_row["close"])
                    if isinstance(latest_row.get("close"), (int, float))
                    else None
                ),
                support_price=min(lows) if lows else None,
                resistance_price=max(highs) if highs else None,
                minute_volume=(
                    float(latest_row["volume"])
                    if isinstance(latest_row.get("volume"), (int, float))
                    else None
                ),
                recent_average_volume=recent_average_volume,
                capital={
                    "total_net": capital.get("total_net", capital.get("flow_today")),
                    "large_net": capital.get("large_net"),
                    "flow_15m": capital.get("flow_15m"),
                    "flow_30m": capital.get("flow_30m"),
                },
                capital_state=capital.get("quality", "UNAVAILABLE"),
            )
        )
        self.store.save_minute_decision(snapshot)
        return snapshot

    async def _submit_live_minute_result(
        self,
        *,
        item: MonitoredSymbol,
        decision: DowMinuteDecision,
        minute_rows: pl.DataFrame,
        intraday_capital: dict | None,
        notifications: NotificationIndex,
        now: datetime,
    ) -> None:
        if self._minute_result_scheduler is None:
            return
        try:
            from app.services.dow_monitor_minute_result_live import (
                build_live_minute_result_context,
            )

            quote = self._latest_quotes_by_symbol.get(
                _monitor_symbol_identity(item.symbol),
                self._latest_quotes_by_symbol.get(item.symbol, {}),
            )
            depth = quote.get("depth") if isinstance(quote.get("depth"), dict) else quote
            context = await asyncio.to_thread(
                build_live_minute_result_context,
                item=item,
                decision=decision,
                minute_rows=minute_rows,
                states=self.store.list_states(),
                quote=quote,
                depth=depth,
                capital=intraday_capital,
                notifications=tuple(
                    notification
                    for values in notifications.values()
                    for notification in values
                ),
                updated_at=now,
            )
            self._minute_result_scheduler.submit_live(context)
        except Exception:
            logger.exception(
                "dow monitor realtime minute result append failed: %s",
                item.symbol,
            )

    def _present_minute_decision(
        self,
        item: MonitoredSymbol,
        decision: DowMinuteDecision | None,
        now: datetime,
    ) -> dict | None:
        if decision is None:
            return None
        if not self._market_is_open(item, now):
            presented = decision.model_copy(
                update={
                    "action": "OBSERVE",
                    "action_label": "继续观察",
                    "data_status": "MARKET_CLOSED",
                    "status_label": "已收盘",
                }
            )
            return presented.model_dump(mode="json")

        zone = ZoneInfo(market_session_policy(item.symbol).timezone)
        local_now = now.astimezone(zone)
        local_decision = decision.decision_minute.astimezone(zone)
        age_seconds = (local_now - local_decision).total_seconds()
        if age_seconds > 90:
            presented = decision.model_copy(
                update={
                    "action": "OBSERVE",
                    "action_label": "继续观察",
                    "data_status": "DELAYED",
                    "status_label": "数据延迟",
                }
            )
            return presented.model_dump(mode="json")
        if local_now >= local_decision + timedelta(minutes=1):
            presented = decision.model_copy(
                update={
                    "data_status": "WAITING_NEW_MINUTE",
                    "status_label": "等待新分钟数据",
                }
            )
            return presented.model_dump(mode="json")
        return decision.model_dump(mode="json")

    def overview(self, market: str = "all") -> dict:
        now = self._now()
        notifications = self.store.list_notifications(
            market=None if market == "all" else market,
            limit=1_000,
        )
        latest_by_symbol = {}
        for notification in notifications:
            latest_by_symbol.setdefault(
                notification.symbol,
                notification.model_dump(mode="json"),
            )

        items = [
            item
            for item in self.store.list_symbols()
            if market == "all" or item.market == market
        ]
        history_statuses = (
            self._history_status_reader.for_symbols([item.symbol for item in items])
            if self._history_status_reader is not None
            else {}
        )
        try:
            half_hour_ai_summaries = (
                self._half_hour_ai_repository.latest_summaries(
                    [(item.market, item.symbol) for item in items]
                )
                if self._half_hour_ai_repository is not None
                else {}
            )
        except Exception as exc:
            logger.debug("half-hour AI summaries unavailable: %s", exc)
            half_hour_ai_summaries = {}
        intraday_capital_by_symbol = self._intraday_capital_by_symbol(
            [item.symbol for item in items]
        )
        symbols = []
        source_timestamps: list[datetime] = []
        for item in items:
            quote = self._latest_quotes_by_symbol.get(
                _monitor_symbol_identity(item.symbol),
                self._latest_quotes_by_symbol.get(item.symbol, {}),
            )
            next_day_direction = self._next_day_direction_with_realtime(
                item.symbol,
                quote,
            )
            states = {}
            for timeframe in TIMEFRAMES:
                state = self.store.get_state(item.symbol, timeframe)
                if state is not None:
                    states[timeframe] = state.model_dump(mode="json")
                    if state.source_timestamp is not None:
                        source_timestamps.append(state.source_timestamp)
            symbols.append(
                {
                    **item.model_dump(mode="json"),
                    "name": quote.get("name"),
                    "last_price": quote.get("last_price"),
                    "change_pct": quote.get("change_pct"),
                    "quote_timestamp": quote.get("timestamp"),
                    "next_day_direction": next_day_direction,
                    "intraday_capital": intraday_capital_by_symbol.get(
                        _monitor_symbol_identity(item.symbol)
                    ),
                    "minute_decision": self._present_minute_decision(
                        item,
                        self.store.get_minute_decision(item.symbol),
                        now,
                    ),
                    "states": states,
                    "latest_notification": latest_by_symbol.get(item.symbol),
                    "last_success_at": self._as_json_time(
                        self._last_success_for_symbol(item.symbol)
                    ),
                    "last_error": self._errors.get(item.symbol),
                    "history_backfill": history_statuses.get(
                        item.symbol,
                        HistoryBackfillStatus(
                            status="unknown",
                            last_error="STATUS_READER_UNAVAILABLE",
                        ),
                    ).model_dump(mode="json"),
                    "half_hour_ai_analysis": self._half_hour_ai_summary(
                        item.market,
                        item.symbol,
                        half_hour_ai_summaries,
                    ),
                }
            )
        return {
            "symbols": symbols,
            "source": "webstock",
            "source_timestamp": self._as_json_time(max(source_timestamps, default=None)),
        }

    def _half_hour_ai_summary(self, market, symbol, summaries) -> dict:
        summary = summaries.get((market, symbol))
        if summary is None:
            return {
                "analysis_id": None,
                "status": (
                    "pending"
                    if self._half_hour_ai_repository is not None
                    else "unavailable"
                ),
                "window_end": None,
                "title": None,
                "summary": None,
            }
        return {
            "analysis_id": summary.analysis_id,
            "status": summary.status,
            "window_end": self._as_json_time(summary.window_end),
            "title": summary.title,
            "summary": summary.summary,
        }

    def list_half_hour_ai(self, market: str, symbol: str, trade_date) -> list[dict]:
        if self._half_hour_ai_repository is None:
            return []
        return [
            item.model_dump(mode="json")
            for item in self._half_hour_ai_repository.list_history(
                market,
                symbol,
                trade_date,
            )
        ]

    def get_half_hour_ai(self, market: str, symbol: str, analysis_id: str):
        if self._half_hour_ai_repository is None:
            return None
        item = self._half_hour_ai_repository.get_by_id(analysis_id)
        if item is None or item.market != market or item.symbol != symbol:
            return None
        return item.model_dump(mode="json", exclude={"input_snapshot"})

    def _intraday_capital_by_symbol(self, symbols: list[str]) -> dict[str, dict]:
        canonical_symbols = sorted(
            {_monitor_symbol_identity(symbol) for symbol in symbols if symbol.strip()}
        )
        if not canonical_symbols:
            return {}
        output: dict[str, dict] = {}
        if _fetch_realtime_signal_rows is not None:
            try:
                rows = _fetch_realtime_signal_rows(
                    canonical_symbols,
                    now=self._now(),
                    max_quote_age_minutes=24 * 60,
                )
            except Exception as exc:
                logger.debug("dow monitor intraday capital unavailable: %s", exc)
                rows = {}
            for symbol, row in rows.items():
                if not isinstance(row, dict):
                    continue
                output[_monitor_symbol_identity(symbol)] = {
                    "capital_minute": row.get("capital_minute"),
                    "total_net": _finite_float(row.get("total_net")),
                    "large_net": _finite_float(row.get("large_net")),
                    "total_in": _finite_float(row.get("total_in")),
                    "total_out": _finite_float(row.get("total_out")),
                    "large_net_ratio": _finite_float(row.get("large_net_ratio")),
                    "flow_15m": _finite_float(row.get("flow_15m")),
                    "flow_30m": _finite_float(row.get("flow_30m")),
                    "flow_today": _finite_float(row.get("flow_today")),
                    "last_flow_time": row.get("last_flow_time"),
                    "flow_points": int(row.get("flow_points") or 0),
                    "source": "trading_day",
                }
        windows_by_symbol = self._intraday_capital_windows_by_symbol(canonical_symbols)
        for symbol, windows in windows_by_symbol.items():
            if not windows:
                continue
            identity = _monitor_symbol_identity(symbol)
            current = output.setdefault(identity, {"source": "trading_day"})
            current["windows"] = windows
            latest = windows[0]
            current.setdefault("capital_minute", latest.get("end_time"))
            current.setdefault("total_net", latest.get("end_total_net"))
            current.setdefault("large_net", latest.get("end_large_net"))
        for capital in output.values():
            capital["quality"] = self._capital_quality(capital)
        return output

    def _capital_quality(self, capital: dict) -> str:
        values = (
            capital.get("total_net"),
            capital.get("large_net"),
            capital.get("flow_15m"),
            capital.get("flow_30m"),
            capital.get("flow_today"),
        )
        if all(value is None for value in values):
            return "UNAVAILABLE"

        capital_minute = _parse_clickhouse_time(capital.get("capital_minute"))
        if capital_minute is not None:
            zone = ZoneInfo("Asia/Shanghai")
            local_minute = (
                capital_minute.astimezone(zone)
                if capital_minute.tzinfo is not None
                else capital_minute.replace(tzinfo=zone)
            )
            age = self._now().astimezone(zone) - local_minute
            if age > CAPITAL_DELAY_THRESHOLD:
                return "DELAYED"

        flow_points = int(capital.get("flow_points") or 0)
        if (
            flow_points < 2
            or capital.get("flow_15m") is None
            or capital.get("flow_30m") is None
        ):
            return "INSUFFICIENT"
        return "COMPLETE"

    def _intraday_capital_windows_by_symbol(
        self,
        symbols: list[str],
        windows_minutes: tuple[int, ...] = (30, 45, 60),
    ) -> dict[str, list[dict]]:
        symbol_list = sorted(
            {_monitor_symbol_identity(symbol) for symbol in symbols if symbol.strip()}
        )
        if not symbol_list:
            return {}
        database = _clickhouse_ident(os.getenv("CLICKHOUSE_DATABASE", "longbridge"))
        now_text = self._now().astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
        try:
            rows = _clickhouse_query_json_each_row(
                f"""
                with parseDateTime64BestEffort({_clickhouse_string(now_text)}, 3, 'Asia/Shanghai') as anchor
                select q.symbol,
                       q.snapshot_minute,
                       q.last_done,
                       c.total_net,
                       c.large_net
                from (
                  select symbol,
                         snapshot_minute,
                         argMax(last_done, inserted_at) as last_done
                  from {database}.lb_realtime_quotes
                  where symbol in {_clickhouse_symbol_tuple(symbol_list)}
                    and snapshot_minute >= anchor - interval 1 day
                    and snapshot_minute <= anchor + interval 5 minute
                  group by symbol, snapshot_minute
                ) q
                left join (
                  select symbol,
                         snapshot_minute,
                         argMax(total_net, inserted_at) as total_net,
                         argMax(large_net, inserted_at) as large_net
                  from {database}.lb_realtime_capital
                  where symbol in {_clickhouse_symbol_tuple(symbol_list)}
                    and snapshot_minute >= anchor - interval 1 day
                    and snapshot_minute <= anchor + interval 5 minute
                  group by symbol, snapshot_minute
                ) c on q.symbol = c.symbol and q.snapshot_minute = c.snapshot_minute
                order by q.symbol, q.snapshot_minute
                """
            )
        except Exception as exc:
            logger.debug("dow monitor intraday capital windows unavailable: %s", exc)
            return {}

        rows_by_symbol: dict[str, list[dict]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            timestamp = _parse_clickhouse_time(row.get("snapshot_minute"))
            price = _finite_float(row.get("last_done"))
            total_net = _finite_float(row.get("total_net"))
            large_net = _finite_float(row.get("large_net"))
            if not symbol or timestamp is None or price is None:
                continue
            rows_by_symbol.setdefault(symbol, []).append(
                {
                    "time": timestamp,
                    "time_text": row.get("snapshot_minute"),
                    "price": price,
                    "total_net": total_net,
                    "large_net": large_net,
                }
            )

        output: dict[str, list[dict]] = {}
        for symbol, symbol_rows in rows_by_symbol.items():
            valid_rows = [
                row
                for row in symbol_rows
                if row.get("total_net") is not None or row.get("large_net") is not None
            ]
            if len(valid_rows) < 2:
                continue
            end_row = valid_rows[-1]
            end_time = end_row["time"]
            windows: list[dict] = []
            for minutes in windows_minutes:
                target = end_time - timedelta(minutes=minutes)
                start_candidates = [row for row in valid_rows if row["time"] <= target]
                start_row = start_candidates[-1] if start_candidates else valid_rows[0]
                if start_row is end_row:
                    continue
                start_price = _finite_float(start_row.get("price"))
                end_price = _finite_float(end_row.get("price"))
                price_change_pct = (
                    (end_price / start_price - 1.0) * 100.0
                    if start_price not in (None, 0) and end_price is not None
                    else None
                )
                start_total = _finite_float(start_row.get("total_net"))
                end_total = _finite_float(end_row.get("total_net"))
                start_large = _finite_float(start_row.get("large_net"))
                end_large = _finite_float(end_row.get("large_net"))
                windows.append(
                    {
                        "label": f"近{minutes}分钟",
                        "minutes": minutes,
                        "start_time": start_row.get("time_text"),
                        "end_time": end_row.get("time_text"),
                        "start_price": start_price,
                        "end_price": end_price,
                        "price_change_pct": price_change_pct,
                        "start_total_net": start_total,
                        "end_total_net": end_total,
                        "total_net_delta": (
                            end_total - start_total
                            if start_total is not None and end_total is not None
                            else None
                        ),
                        "start_large_net": start_large,
                        "end_large_net": end_large,
                        "large_net_delta": (
                            end_large - start_large
                            if start_large is not None and end_large is not None
                            else None
                        ),
                    }
                )
            if windows:
                output[symbol] = windows
        return output

    def _retain_latest_quotes(self, quotes: list[dict]) -> None:
        for row in quotes:
            symbol = _monitor_symbol_identity(row.get("symbol") or "")
            if not symbol:
                continue
            previous = self._latest_quotes_by_symbol.get(symbol)
            timestamp = row.get("timestamp")
            previous_timestamp = previous.get("timestamp") if previous else None
            if previous is None or (
                isinstance(timestamp, (int, float))
                and (
                    not isinstance(previous_timestamp, (int, float))
                    or timestamp >= previous_timestamp
                )
            ):
                self._latest_quotes_by_symbol[symbol] = deepcopy(row)

    def _update_next_day_direction(
        self,
        item: MonitoredSymbol,
        daily_rows: pl.DataFrame,
    ) -> None:
        context = self._compute_next_day_direction(item.symbol, daily_rows)
        if context is not None:
            self._next_day_direction_by_symbol[item.symbol] = context

    def _maybe_append_next_day_notification(
        self,
        item: MonitoredSymbol,
        now: datetime,
        notification_index: NotificationIndex,
    ) -> None:
        del item, now, notification_index
        return

    def _next_day_direction_with_realtime(self, symbol: str, quote: dict) -> dict | None:
        context = self._next_day_direction_by_symbol.get(symbol)
        if context is None:
            return None
        result = deepcopy(context)
        price = _finite_float(quote.get("last_price"))
        if price is None:
            result["realtime_signal"] = "OBSERVE"
            result["realtime_label"] = "等待实时价"
            result["realtime_reason"] = "暂无可用实时价"
            return result

        key_levels = result.get("key_levels") if isinstance(result.get("key_levels"), dict) else {}
        support = _finite_float(key_levels.get("support"))
        resistance = _finite_float(key_levels.get("resistance"))
        stop = _finite_float(key_levels.get("stop"))
        score = _finite_float(result.get("score")) or 0.0
        result["last_price"] = price
        if resistance is not None and price >= resistance and score >= 70.0:
            result["realtime_signal"] = "BUY_TRIGGER"
            result["realtime_label"] = "买点触发"
            result["realtime_reason"] = f"实时价突破关键位 {resistance:.3f}"
        elif stop is not None and price <= stop:
            result["realtime_signal"] = "RISK"
            result["realtime_label"] = "风险走弱"
            result["realtime_reason"] = f"实时价跌破风控线 {stop:.3f}"
        elif support is not None and price < support and score < 70.0:
            result["realtime_signal"] = "WEAK"
            result["realtime_label"] = "偏弱观察"
            result["realtime_reason"] = f"实时价低于支撑 {support:.3f}"
        elif score >= 85.0 and (support is None or price >= support):
            result["realtime_signal"] = "BUY_WATCH"
            result["realtime_label"] = "强势跟踪"
            result["realtime_reason"] = "日线评分强、实时价守在支撑上方"
        elif score >= 70.0:
            result["realtime_signal"] = "WATCH_LONG"
            result["realtime_label"] = "偏多跟踪"
            result["realtime_reason"] = "日线评分偏多、等待关键位确认"
        else:
            result["realtime_signal"] = "OBSERVE"
            result["realtime_label"] = "观察"
            result["realtime_reason"] = "次日方向优势不足"
        return result

    @staticmethod
    def _compute_next_day_direction(symbol: str, daily_rows: pl.DataFrame) -> dict | None:
        if daily_rows.is_empty():
            return None
        frame = daily_rows
        if "symbol" in frame.columns:
            frame = frame.filter(
                pl.col("symbol").cast(pl.Utf8).str.to_uppercase() == symbol.upper()
            )
        if frame.is_empty():
            return None

        date_column = next(
            (
                column
                for column in ("trade_date", "date", "datetime", "timestamp")
                if column in frame.columns
            ),
            None,
        )
        if date_column is not None:
            frame = frame.sort(date_column)
        rows = frame.tail(90).to_dicts()
        closes = [_finite_float(row.get("close")) for row in rows]
        opens = [_finite_float(row.get("open")) for row in rows]
        highs = [_finite_float(row.get("high")) for row in rows]
        lows = [_finite_float(row.get("low")) for row in rows]
        volumes = [_finite_float(row.get("volume")) for row in rows]
        valid_closes = [value for value in closes if value is not None]
        if len(valid_closes) < 20:
            return None

        close = closes[-1]
        open_ = opens[-1]
        if close is None:
            return None

        ma5 = _mean_last(closes, 5)
        ma20 = _mean_last(closes, 20)
        ma60 = _mean_last(closes, 60)
        prev_close = closes[-2] if len(closes) >= 2 else None
        prev_ma20 = _mean_last(closes[:-1], 20)
        momentum_20d = _momentum(closes, 20)
        momentum_60d = _momentum(closes, 60)
        vol_ma5 = _mean_last(volumes, 5)
        vol_ratio = (
            volumes[-1] / vol_ma5
            if volumes and volumes[-1] is not None and vol_ma5 not in (None, 0)
            else None
        )
        rsi = _rsi_last(closes, 14)
        macd_hist = _macd_hist(closes)
        prev_macd_hist = _macd_hist(closes[:-1])
        high60 = _max_last(highs, 60)
        low20 = _min_last(lows, 20)

        trend_ok = close > (ma20 or math.inf) and (ma20 or -math.inf) >= (ma60 or math.inf)
        short_trend_ok = close > (ma5 or math.inf) and (ma5 or -math.inf) >= (ma20 or math.inf)
        momentum_ok = (momentum_20d or -math.inf) > 0 and (momentum_60d or 0.0) > -0.03
        volume_ok = vol_ratio is not None and 1.05 <= vol_ratio <= 3.5
        rsi_ok = rsi is not None and 45.0 <= rsi <= 78.0
        macd_ok = (
            macd_hist is not None
            and (
                macd_hist > 0
                or (
                    prev_macd_hist is not None
                    and macd_hist > prev_macd_hist
                    and macd_hist > -0.03
                )
            )
        )
        breakout_ok = (
            (high60 is not None and close >= high60 * 0.98)
            or (
                ma20 is not None
                and prev_close is not None
                and prev_ma20 is not None
                and close > ma20
                and prev_close <= prev_ma20
            )
        )
        candle_ok = open_ is not None and close > open_

        score = (
            (18 if trend_ok else 0)
            + (12 if short_trend_ok else 0)
            + (18 if momentum_ok else 0)
            + (14 if volume_ok else 0)
            + (12 if rsi_ok else 0)
            + (16 if macd_ok else 0)
            + (10 if breakout_ok else 0)
        )
        if (momentum_20d or -math.inf) <= 0:
            score -= 18
        if rsi is not None and rsi > 82.0:
            score -= 18
        if candle_ok:
            score += 4
        score = max(0.0, min(100.0, float(score)))

        if score >= 85.0:
            direction_label = "强势偏多"
        elif score >= 70.0:
            direction_label = "偏多观察"
        elif score <= 35.0:
            direction_label = "偏空风险"
        else:
            direction_label = "中性震荡"

        evidence = []
        if trend_ok:
            evidence.append("趋势站上MA20且MA20不弱于MA60")
        if momentum_ok:
            evidence.append("20/60日动量配合")
        if volume_ok:
            evidence.append("量能温和放大")
        if macd_ok:
            evidence.append("MACD动能改善")
        if breakout_ok:
            evidence.append("接近或突破阶段高点")
        if rsi is not None and rsi > 82.0:
            evidence.append("RSI过热、降低追高信号")
        if not evidence:
            evidence.append("优势指标不足")

        as_of = None
        if date_column is not None:
            as_of = rows[-1].get(date_column)
            as_of = as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of)
        support = ma20 if ma20 is not None else low20
        return {
            "symbol": symbol,
            "as_of": as_of,
            "score": score,
            "probability": score / 100.0,
            "direction_label": direction_label,
            "key_levels": {
                "support": support,
                "resistance": high60,
                "stop": support * 0.97 if support is not None else None,
                "recent_low": low20,
            },
            "metrics": {
                "ma5": ma5,
                "ma20": ma20,
                "ma60": ma60,
                "momentum_20d": momentum_20d,
                "momentum_60d": momentum_60d,
                "vol_ratio_5d": vol_ratio,
                "rsi_14": rsi,
                "macd_hist": macd_hist,
            },
            "evidence": evidence[:4],
        }

    def detail(self, symbol: str, timeframe: str) -> dict | None:
        state = self.store.get_state(symbol.strip().upper(), timeframe)
        if state is None:
            return None
        return {
            **state.model_dump(mode="json"),
            "last_success_at": self._as_json_time(self._last_success_for_symbol(state.symbol)),
            "last_error": self._errors.get(state.symbol),
        }

    def status(self) -> dict:
        enabled_symbols = [item for item in self.store.list_symbols() if item.enabled]
        enabled_markets = sorted({item.market for item in enabled_symbols})
        now = self._now()
        open_enabled_markets: set[str] = set()
        for item in enabled_symbols:
            policy = market_session_policy(item.symbol)
            local_now = now.astimezone(ZoneInfo(policy.timezone))
            if local_now.weekday() >= 5:
                continue
            if any(start <= local_now.time() < end for start, end in policy.sessions):
                open_enabled_markets.add(item.market)
        minute_results = (
            self._minute_result_scheduler.status().model_dump(mode="json")
            if self._minute_result_scheduler is not None
            else self._minute_result_materializer.status().model_dump(mode="json")
            if self._minute_result_materializer is not None
            else {"enabled": False}
        )
        with self._metrics_lock:
            cycle_duration_seconds = self._last_cycle_duration_seconds
            evaluation_request_count = self._last_evaluation_request_count
            cache_skip_count = self._last_cache_skip_count
            evaluated_symbols = list(self._evaluated_symbols)
            evaluated_timeframes = deepcopy(self._last_evaluated_timeframes)
        return {
            "running": self._task is not None and not self._task.done(),
            "poll_seconds": self.poll_seconds,
            "max_parallel_symbols": self.max_parallel_symbols,
            "source": "webstock",
            "enabled_markets": enabled_markets,
            "open_enabled_markets": sorted(open_enabled_markets),
            "last_started_at": self._as_json_time(self._last_started_at),
            "last_completed_at": self._as_json_time(self._last_completed_at),
            "last_success_at": self._as_json_time(self._last_success_at),
            "last_error": self._last_error,
            "errors": dict(self._errors),
            "cycle_duration_seconds": cycle_duration_seconds,
            "evaluation_request_count": evaluation_request_count,
            "cache_skip_count": cache_skip_count,
            "evaluated_symbols": evaluated_symbols,
            "evaluated_timeframes": evaluated_timeframes,
            "minute_results": minute_results,
        }

    def _now(self) -> datetime:
        now = self._now_fn()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now_fn must return a timezone-aware datetime")
        return now

    def _last_success_for_symbol(self, symbol: str) -> datetime | None:
        runtime = self._last_success_by_symbol.get(symbol)
        if runtime is not None:
            return runtime
        persisted: list[datetime] = []
        for timeframe in TIMEFRAMES:
            state = self.store.get_state(symbol, timeframe)
            if state is not None and state.snapshot and state.source_timestamp is not None:
                persisted.append(state.source_timestamp)
        return max(persisted, default=None)

    @staticmethod
    def _as_json_time(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None
