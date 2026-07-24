from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import polars as pl

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
    DowNotification,
    DowTimeframeState,
    MonitoredSymbol,
)

logger = logging.getLogger(__name__)

TIMEFRAMES = ("5m", "15m", "30m", "60m", "day")
NotificationIndex = dict[tuple[str, str], list[DowNotification]]


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
    ) -> None:
        self.store = store
        self._data_gateway = data_gateway
        self._dow_client = dow_client
        self._daily_loader = daily_loader
        self.poll_seconds = poll_seconds
        self._now_fn = now_fn
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_started_at: datetime | None = None
        self._last_completed_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_success_by_symbol: dict[str, datetime] = {}
        self._last_error: str | None = None
        self._errors: dict[str, str] = {}
        self._latest_quotes_by_symbol: dict[str, dict] = {}

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="dow-monitor")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        try:
            await task
        finally:
            self._task = None

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
        now = self._now()
        self._last_started_at = now
        enabled = [
            item for item in await asyncio.to_thread(self.store.list_symbols) if item.enabled
        ]
        if not enabled:
            self._last_error = None
            self._last_success_at = now
            self._last_completed_at = self._now()
            return

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
        warmup_errors: dict[str, str] = {}
        if cold_live_symbols:
            try:
                history = await asyncio.to_thread(
                    self._data_gateway.load_history,
                    cold_live_symbols,
                    now,
                )
            except Exception as exc:
                message = str(exc)
                warmup_errors.update(dict.fromkeys(cold_live_symbols, message))
            else:
                history_rows = history.minute_rows
                for symbol in cold_live_symbols:
                    coverage = history.coverage_by_symbol.get(symbol)
                    if coverage is None:
                        warmup_errors[symbol] = "HISTORY_INCOMPLETE:NO_PRIOR_SESSION"
                    elif coverage.state != "COMPLETE":
                        warmup_errors[symbol] = (
                            f"HISTORY_INCOMPLETE:{coverage.reason or 'NO_PRIOR_SESSION'}"
                        )

        notification_index = await asyncio.to_thread(self._load_notification_index)
        any_success = False
        cycle_errors: list[str] = []
        for item in enabled:
            warmup_error = warmup_errors.get(item.symbol)
            if warmup_error is not None:
                await asyncio.to_thread(
                    self._mark_all,
                    item,
                    "ANALYSIS_PAUSED",
                    now,
                )
                self._errors[item.symbol] = warmup_error
                cycle_errors.append(f"{item.symbol}: {warmup_error}")
                continue
            try:
                error, symbol_succeeded = await asyncio.to_thread(
                    self._evaluate_symbol,
                    item,
                    batch,
                    now,
                    notification_index,
                    item.symbol in cold_symbols,
                    history_rows,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = str(exc)
                symbol_succeeded = False
                await asyncio.to_thread(self._mark_all, item, "ANALYSIS_PAUSED", now)
                logger.exception("dow monitor symbol failed: %s", item.symbol)
            if symbol_succeeded:
                self._last_success_by_symbol[item.symbol] = now
                any_success = True
            if error is None:
                self._errors.pop(item.symbol, None)
            else:
                self._errors[item.symbol] = error
                cycle_errors.append(f"{item.symbol}: {error}")

        self._last_error = "; ".join(cycle_errors) or None
        if any_success:
            self._last_success_at = now
        self._last_completed_at = self._now()

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
        canonical_minutes = (
            self._merge_warmup_minutes(
                item,
                history_rows,
                batch.minute_rows,
            )
            if cold_start
            else batch.minute_rows
        )
        frames_by_cutoff: dict[str | None, dict[str, TimeframeBars]] = {}
        errors: list[str] = []
        successes = 0
        for timeframe in TIMEFRAMES:
            previous_state = self.store.get_state(item.symbol, timeframe)
            cutoff = (
                None
                if cold_start
                else previous_state.source_timestamp
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
                None if cold_start else previous_state,
                frame,
                now,
            )
            try:
                result = self._dow_client.evaluate(
                    item.symbol,
                    timeframe,
                    bars,
                    completion,
                    now,
                )
            except Exception as exc:
                self._mark_one(
                    item,
                    timeframe,
                    "ANALYSIS_PAUSED",
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
        return "; ".join(dict.fromkeys(errors)) or None, successes > 0

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

    def overview(self, market: str = "all") -> dict:
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

        symbols = []
        source_timestamps: list[datetime] = []
        for item in self.store.list_symbols():
            if market != "all" and item.market != market:
                continue
            quote = self._latest_quotes_by_symbol.get(item.symbol, {})
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
                    "states": states,
                    "latest_notification": latest_by_symbol.get(item.symbol),
                    "last_success_at": self._as_json_time(
                        self._last_success_for_symbol(item.symbol)
                    ),
                    "last_error": self._errors.get(item.symbol),
                }
            )
        return {
            "symbols": symbols,
            "source": "webstock",
            "source_timestamp": self._as_json_time(max(source_timestamps, default=None)),
        }

    def _retain_latest_quotes(self, quotes: list[dict]) -> None:
        for row in quotes:
            symbol = str(row.get("symbol") or "").strip().upper()
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
        return {
            "running": self._task is not None and not self._task.done(),
            "poll_seconds": self.poll_seconds,
            "source": "webstock",
            "enabled_markets": enabled_markets,
            "open_enabled_markets": sorted(open_enabled_markets),
            "last_started_at": self._as_json_time(self._last_started_at),
            "last_completed_at": self._as_json_time(self._last_completed_at),
            "last_success_at": self._as_json_time(self._last_success_at),
            "last_error": self._last_error,
            "errors": dict(self._errors),
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
