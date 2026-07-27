from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from app.services.dow_monitor_models import (
    DowMinuteDecision,
    DowNotification,
    DowTimeframeState,
    MonitoredSymbol,
)


def _monitor_symbol_identity(symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    if normalized.endswith(".HK"):
        code = normalized[:-3]
        if code.isdigit():
            return f"{int(code)}.HK"
    return normalized


class DowMonitorStore:
    """File-backed monitored symbols, timeframe states, and immutable notifications."""

    _lock = threading.RLock()

    def __init__(self, data_dir: Path) -> None:
        self._directory = Path(data_dir) / "user_data"
        self._symbols_path = self._directory / "dow_monitor_symbols.json"
        self._states_path = self._directory / "dow_monitor_states.json"
        self._decisions_path = self._directory / "dow_monitor_minute_decisions.json"
        self._notifications_path = self._directory / "dow_monitor_notifications.jsonl"
        self._activations_path = self._directory / "dow_monitor_activations.json"
        self._notifications, self._read_at = self._load_notifications()
        self._event_keys = {notification.event_key for notification in self._notifications}

    def list_symbols(self) -> list[MonitoredSymbol]:
        with self._lock:
            return self._load_models(self._symbols_path, MonitoredSymbol)

    def upsert_symbol(
        self,
        symbol: str,
        market: Literal["cn", "hk", "us"],
        enabled: bool,
    ) -> MonitoredSymbol:
        with self._lock:
            symbols = self._load_models(self._symbols_path, MonitoredSymbol)
            now = datetime.now(UTC)
            for index, existing in enumerate(symbols):
                if _monitor_symbol_identity(existing.symbol) == _monitor_symbol_identity(symbol):
                    updated = existing.model_copy(
                        update={"market": market, "enabled": enabled, "updated_at": now}
                    )
                    symbols[index] = updated
                    self._write_json(self._symbols_path, symbols)
                    return updated
            created = MonitoredSymbol(
                symbol=symbol,
                market=market,
                enabled=enabled,
                created_at=now,
                updated_at=now,
            )
            symbols.append(created)
            self._write_json(self._symbols_path, symbols)
            return created

    def remove_symbol(self, symbol: str) -> bool:
        with self._lock:
            symbols = self._load_models(self._symbols_path, MonitoredSymbol)
            kept_symbols = [item for item in symbols if item.symbol != symbol]
            if len(kept_symbols) == len(symbols):
                return False
            self._write_json(self._symbols_path, kept_symbols)

            states = self._load_models(self._states_path, DowTimeframeState)
            self._write_json(self._states_path, [item for item in states if item.symbol != symbol])
            decisions = self._load_models(self._decisions_path, DowMinuteDecision)
            self._write_json(
                self._decisions_path,
                [
                    item
                    for item in decisions
                    if _monitor_symbol_identity(item.symbol) != _monitor_symbol_identity(symbol)
                ],
            )
            return True

    def save_state(self, state: DowTimeframeState) -> DowTimeframeState:
        with self._lock:
            states = self._load_models(self._states_path, DowTimeframeState)
            for index, existing in enumerate(states):
                if existing.symbol == state.symbol and existing.timeframe == state.timeframe:
                    states[index] = state
                    break
            else:
                states.append(state)
            self._write_json(self._states_path, states)
            return state

    def get_state(self, symbol: str, timeframe: str) -> DowTimeframeState | None:
        with self._lock:
            for state in self._load_models(self._states_path, DowTimeframeState):
                if state.symbol == symbol and state.timeframe == timeframe:
                    return state
            return None

    def save_minute_decision(self, snapshot: DowMinuteDecision) -> DowMinuteDecision:
        with self._lock:
            decisions = self._load_models(self._decisions_path, DowMinuteDecision)
            identity = _monitor_symbol_identity(snapshot.symbol)
            for index, existing in enumerate(decisions):
                if _monitor_symbol_identity(existing.symbol) != identity:
                    continue
                if existing.decision_minute >= snapshot.decision_minute:
                    return existing
                decisions[index] = snapshot
                break
            else:
                decisions.append(snapshot)
            self._write_json(self._decisions_path, decisions)
            return snapshot

    def get_minute_decision(self, symbol: str) -> DowMinuteDecision | None:
        with self._lock:
            identity = _monitor_symbol_identity(symbol)
            for decision in self._load_models(self._decisions_path, DowMinuteDecision):
                if _monitor_symbol_identity(decision.symbol) == identity:
                    return decision
            return None

    def append_notification(self, notification: DowNotification) -> bool:
        with self._lock:
            self._refresh_notifications()
            if notification.event_key in self._event_keys:
                return False
            self._append_jsonl_record(notification.model_dump(mode="json"))
            self._notifications.append(notification)
            self._event_keys.add(notification.event_key)
            return True

    def list_notifications(
        self,
        market: Literal["cn", "hk", "us"] | None = None,
        unread_only: bool = False,
        limit: int = 100,
    ) -> list[DowNotification]:
        with self._lock:
            self._refresh_notifications()
            result = [
                notification.model_copy(
                    update={
                        "read_at": self._read_at.get(
                            notification.notification_id, notification.read_at
                        )
                    }
                )
                for notification in self._notifications
                if market is None or notification.market == market
            ]
            if unread_only:
                result = [notification for notification in result if notification.read_at is None]
            result.sort(key=lambda notification: notification.triggered_at, reverse=True)
            return result[:limit]

    def get_notification(self, notification_id: str) -> DowNotification | None:
        with self._lock:
            self._refresh_notifications()
            for notification in self._notifications:
                if notification.notification_id == notification_id:
                    return notification.model_copy(
                        update={
                            "read_at": self._read_at.get(
                                notification.notification_id,
                                notification.read_at,
                            )
                        }
                    )
            return None

    def mark_read(self, notification_id: str) -> bool:
        with self._lock:
            self._refresh_notifications()
            if not any(
                notification.notification_id == notification_id
                for notification in self._notifications
            ):
                return False
            if notification_id not in self._read_at:
                read_at = datetime.now(UTC)
                self._append_jsonl_record(
                    {
                        "kind": "read-receipt",
                        "notification_id": notification_id,
                        "read_at": read_at.isoformat(),
                    }
                )
                self._read_at[notification_id] = read_at
            return True

    def _load_models(
        self,
        path: Path,
        model_type: (
            type[MonitoredSymbol]
            | type[DowTimeframeState]
            | type[DowMinuteDecision]
        ),
    ):
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        models = []
        for item in payload:
            try:
                models.append(model_type.model_validate(item))
            except (TypeError, ValueError):
                continue
        return models

    def _load_notifications(self) -> tuple[list[DowNotification], dict[str, datetime]]:
        if not self._notifications_path.is_file():
            return [], {}
        notifications: list[DowNotification] = []
        read_at: dict[str, datetime] = {}
        try:
            lines = self._notifications_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return [], {}
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if item.get("kind") == "read-receipt":
                try:
                    read_at[item["notification_id"]] = datetime.fromisoformat(item["read_at"])
                except (KeyError, TypeError, ValueError):
                    continue
                continue
            try:
                notifications.append(DowNotification.model_validate(item))
            except (TypeError, ValueError):
                continue
        return notifications, read_at

    def _append_jsonl_record(self, record: dict) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        needs_separator = False
        if self._notifications_path.is_file() and self._notifications_path.stat().st_size:
            with self._notifications_path.open("rb") as handle:
                handle.seek(-1, 2)
                needs_separator = handle.read(1) not in {b"\n", b"\r"}
        with self._notifications_path.open("a", encoding="utf-8") as handle:
            if needs_separator:
                handle.write("\n")
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _refresh_notifications(self) -> None:
        self._notifications, self._read_at = self._load_notifications()
        self._event_keys = {notification.event_key for notification in self._notifications}

    def _write_json(
        self,
        path: Path,
        models: (
            list[MonitoredSymbol]
            | list[DowTimeframeState]
            | list[DowMinuteDecision]
        ),
    ) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self._directory,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(
                [model.model_dump(mode="json") for model in models], temporary, ensure_ascii=False
            )
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
