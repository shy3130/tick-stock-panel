"""Keep a provider alive until its last call / streaming iterator completes."""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator

from app.data_providers.base import ProviderRequestError

logger = logging.getLogger(__name__)


class _Stream(Iterator):
    def __init__(self, iterator, release):
        self.iterator, self.release = iterator, release

    def __next__(self):
        try:
            return next(self.iterator)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.release is not None:
            release, self.release = self.release, None
            try:
                close = getattr(self.iterator, "close", None)
                if close:
                    close()
            finally:
                release()

    def __del__(self):
        self.close()


class ManagedProvider:
    """Stable handle; swaps do not close an in-flight provider, including iter_daily."""

    builtin = True

    def __init__(self, provider):
        self._lock = threading.RLock()
        self._current = [provider, 0, False]

    def _release(self, entry):
        with self._lock:
            entry[1] -= 1
            close = entry[2] and entry[1] == 0
        if close:
            self._close(entry[0])

    @staticmethod
    def _close(provider):
        try:
            provider.close()
        except Exception:  # noqa: BLE001 — cleanup cannot invalidate the replacement
            logger.warning("provider close failed", exc_info=False)

    def replace(self, provider):
        with self._lock:
            old = self._current
            self._current = [provider, 0, False]
            old[2] = True
            close = old[1] == 0
        if close:
            self._close(old[0])

    def close(self):
        with self._lock:
            old = self._current
            if old[2]:
                return
            old[2] = True
            close = old[1] == 0
        if close:
            self._close(old[0])

    def __getattr__(self, name):
        with self._lock:
            value = getattr(self._current[0], name)
        if not callable(value):
            return value

        def invoke(*args, **kwargs):
            with self._lock:
                entry = self._current
                if entry[2]:
                    raise ProviderRequestError("数据源已停止，请重新选择数据源")
                method = getattr(entry[0], name)
                entry[1] += 1
            try:
                result = method(*args, **kwargs)
            except BaseException:
                self._release(entry)
                raise
            if isinstance(result, Iterator):
                return _Stream(result, lambda: self._release(entry))
            self._release(entry)
            return result
        return invoke
