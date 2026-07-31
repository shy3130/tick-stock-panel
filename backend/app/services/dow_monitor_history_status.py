from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.dow_monitor_models import HistoryBackfillStatus

_ACTIVE_STATES = {"queued", "running", "rebuilding"}
_VALID_STATES = _ACTIVE_STATES | {"completed", "partial", "failed"}


def _identity(symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    if not normalized.endswith(".HK"):
        return normalized
    code = normalized[:-3]
    return f"{int(code)}.HK" if code.isdigit() else normalized


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


class DowMonitorHistoryStatusReader:
    def __init__(
        self,
        path: Path | str,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        stale_after: timedelta = timedelta(minutes=10),
    ) -> None:
        self._path = Path(path)
        self._now_fn = now_fn
        self._stale_after = stale_after

    def for_symbols(
        self,
        symbols: Sequence[str],
    ) -> dict[str, HistoryBackfillStatus]:
        requested = list(symbols)
        if not requested:
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            raw_symbols = payload["symbols"]
            if not isinstance(raw_symbols, dict):
                raise ValueError("symbols must be an object")
        except FileNotFoundError:
            return {
                symbol: HistoryBackfillStatus(status="pending")
                for symbol in requested
            }
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return {
                symbol: HistoryBackfillStatus(
                    status="unknown",
                    last_error="STATUS_UNAVAILABLE",
                )
                for symbol in requested
            }

        document_updated_at = _parse_time(payload.get("updated_at"))
        indexed = {_identity(key): value for key, value in raw_symbols.items()}
        return {
            symbol: self._status_for(
                indexed.get(_identity(symbol)),
                document_updated_at=document_updated_at,
            )
            for symbol in requested
        }

    def _status_for(
        self,
        raw: object,
        *,
        document_updated_at: datetime | None,
    ) -> HistoryBackfillStatus:
        if raw is None:
            return HistoryBackfillStatus(status="pending")
        if not isinstance(raw, dict):
            return HistoryBackfillStatus(
                status="unknown",
                last_error="STATUS_UNAVAILABLE",
            )

        state = str(raw.get("state") or raw.get("status") or "unknown").lower()
        if state not in _VALID_STATES:
            state = "unknown"
        try:
            progress = max(0, min(100, int(raw.get("progress") or 0)))
        except (TypeError, ValueError):
            progress = 0
        missing = raw.get("missing_timeframes")
        missing_timeframes = (
            tuple(str(value) for value in missing)
            if isinstance(missing, list)
            else ()
        )
        updated_at = _parse_time(raw.get("updated_at")) or document_updated_at
        last_error = raw.get("last_error")
        last_error_text = str(last_error) if last_error is not None else None

        if state in _ACTIVE_STATES and self._is_stale(updated_at):
            state = "unknown"
            last_error_text = "STATUS_STALE"

        return HistoryBackfillStatus(
            status=state,
            progress=progress,
            missing_timeframes=missing_timeframes,
            last_error=last_error_text,
            updated_at=updated_at,
        )

    def _is_stale(self, updated_at: datetime | None) -> bool:
        if updated_at is None:
            return True
        now = self._now_fn()
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(UTC) - updated_at.astimezone(UTC) > self._stale_after
