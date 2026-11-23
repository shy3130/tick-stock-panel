"""Merge persistent watchlist demand and short-lived detail leases for one market worker."""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

from app.services import watchlist

LEASE_SECONDS = 90
_SYMBOL = re.compile(r"\d{6}\.(SH|SZ|BJ)\Z")
logger = logging.getLogger(__name__)


class QuoteDemand:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, tuple[float, frozenset[str]]] = {}
        self._watchlist: tuple[tuple, frozenset[str]] | None = None

    def _expire(self, now: float) -> None:
        self._leases = {k: v for k, v in self._leases.items() if v[0] > now}

    def subscribe(self, client_id: str, symbols: list[str]) -> None:
        wanted = frozenset(s.strip().upper() for s in symbols)
        if len(symbols) > 64 or any(not _SYMBOL.fullmatch(s) for s in wanted):
            raise ValueError("最多订阅 64 个有效标的代码（如 000985.SH）")
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            if wanted:
                if client_id not in self._leases and len(self._leases) >= 512:
                    raise ValueError("实时订阅窗口过多，请稍后重试")
                self._leases[client_id] = (now + LEASE_SECONDS, wanted)
            else:
                self._leases.pop(client_id, None)

    def unsubscribe(self, client_id: str) -> None:
        with self._lock:
            self._leases.pop(client_id, None)

    def symbols(self, data_dir: Path) -> set[str]:
        now = time.monotonic()
        path = data_dir / "user_data" / "watchlist.parquet"
        try:
            stat = path.stat()
            signature = (str(path), stat.st_mtime_ns, stat.st_size, watchlist.revision())
        except OSError:
            signature = (str(path), None, watchlist.revision())
        with self._lock:
            cached = self._watchlist
        if cached is None or cached[0] != signature:
            # Disk IO stays outside the lease lock; all page readers share this worker.
            try:
                saved = frozenset(r["symbol"] for r in watchlist.list_symbols() if r.get("symbol"))
            except Exception:  # noqa: BLE001 -- a broken watchlist must not stop market/detail quotes
                logger.warning("自选订阅读取失败，保留上一份有效清单", exc_info=True)
                saved = cached[1] if cached else frozenset()
            else:
                with self._lock:
                    self._watchlist = (signature, saved)
        else:
            saved = cached[1]
        with self._lock:
            self._expire(now)
            return set(saved).union(*(v[1] for v in self._leases.values()))
