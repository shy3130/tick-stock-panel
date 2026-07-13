"""Private beta invite access with one active browser per invite code."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

COOKIE_NAME = "sycee_invite"
COOKIE_MAX_AGE = 365 * 24 * 3600
_STATE_VERSION = 1
_TOKEN_BYTES = 32


def _normalize_code(code: str) -> str:
    return code.strip().casefold()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InviteSession:
    token: str
    expires_at: float


class InviteAccessStore:
    """Persistent invite sessions.

    Invite codes are reusable. Redeeming a code rotates its browser token, so one
    code can authorize at most one browser at a time. Only hashes are written to
    disk; plaintext codes remain in process environment/memory.
    """

    def __init__(self, data_dir: Path, codes: list[str] | tuple[str, ...]) -> None:
        normalized = tuple(dict.fromkeys(_normalize_code(code) for code in codes if code.strip()))
        self._code_digests = frozenset(_digest(code) for code in normalized)
        self._path = data_dir / "user_data" / "invites.json"
        self._lock = threading.RLock()
        self._slots: dict[str, dict[str, float | str]] = {}
        self._load()

    @property
    def enabled(self) -> bool:
        return bool(self._code_digests)

    @property
    def capacity(self) -> int:
        return len(self._code_digests)

    def redeem(self, code: str) -> InviteSession | None:
        code_digest = _digest(_normalize_code(code))
        if code_digest not in self._code_digests:
            return None

        now = time.time()
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        expires_at = now + COOKIE_MAX_AGE
        with self._lock:
            self._slots[code_digest] = {
                "session_hash": _digest(token),
                "issued_at": now,
                "expires_at": expires_at,
            }
            self._save_locked()
        return InviteSession(token=token, expires_at=expires_at)

    def is_valid_session(self, token: str | None) -> bool:
        if not self.enabled or not token:
            return False
        token_digest = _digest(token)
        now = time.time()
        with self._lock:
            for slot in self._slots.values():
                if slot.get("session_hash") != token_digest:
                    continue
                expires_at = slot.get("expires_at")
                return isinstance(expires_at, (int, float)) and expires_at > now
        return False

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            slots = raw.get("slots") if isinstance(raw, dict) else None
            if not isinstance(slots, dict):
                return
            now = time.time()
            self._slots = {
                code_digest: slot
                for code_digest, slot in slots.items()
                if code_digest in self._code_digests
                and isinstance(slot, dict)
                and isinstance(slot.get("expires_at"), (int, float))
                and slot["expires_at"] > now
            }
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("invites.json malformed: %s", exc)

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _STATE_VERSION,
            "slots": self._slots,
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
        with suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
        with suppress(OSError):
            os.chmod(self._path, 0o600)


_store_lock = threading.Lock()
_store: InviteAccessStore | None = None
_store_signature: tuple[str, str] | None = None


def _configured_codes(raw: str) -> tuple[str, ...]:
    return tuple(code.strip() for code in raw.split(",") if code.strip())


def get_store() -> InviteAccessStore:
    """Return the process-wide store, refreshing it when configuration changes."""
    from app.config import settings

    global _store, _store_signature
    signature = (str(settings.data_dir), settings.invite_codes)
    if _store is not None and _store_signature == signature:
        return _store
    with _store_lock:
        if _store is None or _store_signature != signature:
            _store = InviteAccessStore(settings.data_dir, _configured_codes(settings.invite_codes))
            _store_signature = signature
    return _store
