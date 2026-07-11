"""Reference-counted, generation-aware read-only connection pool (Task 16 /
spec §4).

Each logical database is backed by a :class:`ConnectionSet`. Connections are
keyed by *resolved path* — and because a new snapshot generation is a new file
path, a generation swap is simply acquiring a lease on a different path. The
previous generation's connection is *retired* on swap but only actually closed
once no lease still holds it, so a query running across a swap never has its
connection closed underneath it. ``close()`` likewise defers closing any
connection whose refcount is still positive.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator


class _Entry:
    __slots__ = ("conn", "path", "refs", "retired")

    def __init__(self, conn: Any, path: str) -> None:
        self.conn = conn
        self.path = path
        self.refs = 0
        self.retired = False  # superseded by a newer generation


class ConnectionSet:
    """A small pool of read-only connections for one logical database.

    ``opener(path)`` must return a fresh connection for ``path``.
    """

    def __init__(self, opener: Callable[[str], Any]) -> None:
        self._opener = opener
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._current_path: str | None = None
        self._closed = False

    def _acquire(self, path: str) -> _Entry:
        with self._lock:
            if self._closed:
                raise RuntimeError("ConnectionSet is closed")
            entry = self._entries.get(path)
            if entry is None:
                entry = _Entry(self._opener(path), path)
                self._entries[path] = entry
            # Generation swap: retire the outgoing current (kept alive until its
            # own leases drain).
            if self._current_path is not None and self._current_path != path:
                prev = self._entries.get(self._current_path)
                if prev is not None:
                    prev.retired = True
                    self._maybe_close(prev)
            self._current_path = path
            entry.refs += 1
            return entry

    def _release(self, entry: _Entry) -> None:
        with self._lock:
            entry.refs -= 1
            self._maybe_close(entry)

    def _maybe_close(self, entry: _Entry) -> None:
        # Close only when no lease holds it AND it is either retired or the whole
        # set is closing. The live current generation is never closed here.
        if entry.refs <= 0 and (entry.retired or self._closed):
            self._entries.pop(entry.path, None)
            if self._current_path == entry.path:
                self._current_path = None
            try:
                entry.conn.close()
            except Exception:  # noqa: BLE001 - closing best-effort
                pass

    @contextmanager
    def lease(self, path: str) -> Iterator[Any]:
        """Lease the connection for ``path`` for the duration of the block."""
        entry = self._acquire(path)
        try:
            yield entry.conn
        finally:
            self._release(entry)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for entry in list(self._entries.values()):
                self._maybe_close(entry)

    # Introspection for tests.
    def open_count(self) -> int:
        with self._lock:
            return len(self._entries)
