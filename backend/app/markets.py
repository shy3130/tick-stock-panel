"""Market profile single source of truth.

The module centralizes market-aware decisions needed by data and realtime paths:
price-limit semantics, adjustment labeling, and polling sessions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

Session = tuple[time, time]


@dataclass(frozen=True)
class MarketProfile:
    market: str
    has_price_limit: bool
    adjustment: str
    timezone: str
    sessions: tuple[Session, ...]


_A_SHARE = MarketProfile(
    market="a_share",
    has_price_limit=True,
    adjustment="xdxr",
    timezone="Asia/Shanghai",
    # Realtime polling keeps the existing A-share opening auction and close
    # buffers used by quote/depth services.
    sessions=((time(9, 15), time(11, 35)), (time(12, 55), time(15, 5))),
)

_HK = MarketProfile(
    market="hk",
    has_price_limit=False,
    adjustment="none",
    timezone="Asia/Shanghai",
    sessions=((time(9, 30), time(12, 0)), (time(13, 0), time(16, 0))),
)


def market_of(symbol: str) -> MarketProfile:
    """Return the market profile for a symbol.

    P1 only distinguishes HK from the A-share family. SH/SZ/BJ/ETF/INDEX keep
    the A-share profile so existing behavior remains the default.
    """
    return _HK if symbol.upper().endswith(".HK") else _A_SHARE


def any_market_open_at(now: datetime) -> bool:
    """Return whether any supported market is open at ``now``.

    Holiday calendars are intentionally out of scope for HK P1; weekend checks
    keep the previous safety behavior while the session union covers HK 15:00-
    16:00 trading.
    """
    if now.weekday() >= 5:
        return False
    current = now.time()
    for profile in (_A_SHARE, _HK):
        for start, end in profile.sessions:
            if start <= current <= end:
                return True
    return False
