"""Shared rate-limit parsing and scoped logging for credential-bearing HTTP requests."""

import logging
import math
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def retry_after(value: str) -> float | None:
    try:
        seconds = float(value)
    except ValueError:
        try:
            at = parsedate_to_datetime(value)
            if at.tzinfo is None:
                return None
            seconds = max(0.0, (at - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


@contextmanager
def checked_request_logging():
    # HTTPX logs the entire URL at INFO. Other concurrent callers keep their own logs.
    caller = threading.get_ident()
    transport_log = logging.getLogger("httpx")

    def other_thread(record):
        return record.thread != caller

    transport_log.addFilter(other_thread)
    try:
        yield
    finally:
        transport_log.removeFilter(other_thread)
