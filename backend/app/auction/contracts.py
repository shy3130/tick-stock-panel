"""集合竞价内部契约。

单位 (禁止启发式换算):
  - 价格: 元
  - matched_volume / unmatched_volume / open_volume: 手
  - open_amount: 元
  - open_change_pct: 小数 (0.0366 = 3.66%)
  - turnover_rate: 百分数值 (5 = 5%)
  - volume_ratio: 倍数
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from app.market_time import CN_TZ, cn_now, cn_today

AUCTION_FEATURE_VERSION = "auction-v1"

_PRE_OPEN = 91500
_CANCEL_END = 92000
_LOCK_END = 92500
_CONTINUOUS = 93000
_CLOSE = 150000


class AuctionStage(StrEnum):
    pre_open = "pre_open"
    cancellable = "cancellable"
    locked = "locked"
    final = "final"
    post_open = "post_open"
    closed = "closed"


class UnmatchedSide(StrEnum):
    buy = "buy"
    sell = "sell"
    neutral = "neutral"
    unknown = "unknown"


class AuctionStyle(StrEnum):
    momentum = "momentum"
    limit_up = "limit_up"
    swing = "swing"
    volume_price = "volume_price"


def _hhmmss(dt: datetime) -> int:
    local = dt.astimezone(CN_TZ) if dt.tzinfo is not None else dt
    return local.hour * 10000 + local.minute * 100 + local.second


def auction_stage(dt: datetime | None = None) -> AuctionStage:
    """按北京时间判断开盘竞价阶段。收盘竞价过程不在 v1 阶段机里展开。"""
    clock = dt if dt is not None else cn_now()
    stamp = _hhmmss(clock)
    if stamp < _PRE_OPEN:
        return AuctionStage.pre_open
    if stamp < _CANCEL_END:
        return AuctionStage.cancellable
    if stamp < _LOCK_END:
        return AuctionStage.locked
    if stamp < _CONTINUOUS:
        return AuctionStage.final
    if stamp < _CLOSE:
        return AuctionStage.post_open
    return AuctionStage.closed


def is_open_auction_point(hhmmss: int) -> bool:
    """开盘过程窗口: 09:15 <= t < 09:30。09:25 虚拟点仍算过程, 不是正式撮合。"""
    return _PRE_OPEN <= hhmmss < _CONTINUOUS


def cn_datetime(trade_date: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(
        trade_date.year,
        trade_date.month,
        trade_date.day,
        hour,
        minute,
        second,
        tzinfo=CN_TZ,
    )


def datetime_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)
    return int(dt.timestamp() * 1000)


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=CN_TZ)


def hhmmss_from_ms(ms: int) -> int:
    return _hhmmss(ms_to_datetime(ms))


def default_as_of_ms(trade_date: date) -> int:
    """今日用当前时刻; 历史日默认 09:25:30 (正式撮合应已到达)。"""
    if trade_date == cn_today():
        return datetime_to_ms(cn_now())
    return datetime_to_ms(cn_datetime(trade_date, 9, 25, 30))


def parse_trade_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def time_label_to_hhmmss(label: str | None) -> int | None:
    if not label:
        return None
    text = str(label).strip()
    parts = text.replace(".", ":").split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        second = int(float(parts[2])) if len(parts) > 2 else 0
    except (TypeError, ValueError, IndexError):
        return None
    return hour * 10000 + minute * 100 + second


def source_time_ms(trade_date: date, hhmmss: int) -> int:
    hour, rest = divmod(hhmmss, 10000)
    minute, second = divmod(rest, 100)
    return datetime_to_ms(cn_datetime(trade_date, hour, minute, second))


def unmatched_side_from_raw(value: object) -> UnmatchedSide:
    if value is None:
        return UnmatchedSide.unknown
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"buy", "bid", "b", "1", "+"}:
            return UnmatchedSide.buy
        if lowered in {"sell", "ask", "s", "-1", "-"}:
            return UnmatchedSide.sell
        if lowered in {"0", "neutral", "none"}:
            return UnmatchedSide.neutral
        return UnmatchedSide.unknown
    try:
        number = float(value)
    except (TypeError, ValueError):
        return UnmatchedSide.unknown
    if number > 0:
        return UnmatchedSide.buy
    if number < 0:
        return UnmatchedSide.sell
    return UnmatchedSide.neutral


@dataclass(slots=True)
class AuctionSnapshot:
    trade_date: date
    symbol: str
    source: str
    source_time_ms: int
    received_at_ms: int
    indicative_price: float | None
    matched_volume: float | None
    unmatched_volume: float | None
    unmatched_side: UnmatchedSide
    sequence: int = 0
    quality_flags: list[str] = field(default_factory=list)
    pre_close: float | None = None
    matched_amount: float | None = None

    def to_row(self) -> dict:
        return {
            "trade_date": self.trade_date,
            "symbol": self.symbol,
            "source": self.source,
            "source_time_ms": self.source_time_ms,
            "received_at_ms": self.received_at_ms,
            "indicative_price": self.indicative_price,
            "matched_volume": self.matched_volume,
            "unmatched_volume": self.unmatched_volume,
            "unmatched_side": str(self.unmatched_side),
            "sequence": self.sequence,
            "quality_flags": ",".join(self.quality_flags),
            "pre_close": self.pre_close,
            "matched_amount": self.matched_amount,
        }


@dataclass(slots=True)
class MarketRankItem:
    """全市场实时排行初筛行 (Tier 1, 0x054B 分类行情, 非竞价过程口径)。

    单位:
      - change_pct: 小数 (0.0366 = 3.66%), 由 provider 从百分数 /100 归一
      - amount: 元
      - volume_hand: 手
      - opening_rush: 百分数 (开盘冲, provider 原样透传, 仅展示)
      - seal_amount: 元 (封单额)
    """

    symbol: str
    name: str | None
    source: str
    change_pct: float | None
    amount: float | None
    volume_hand: float | None
    opening_rush: float | None
    seal_amount: float | None

    def to_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "source": self.source,
            "change_pct": self.change_pct,
            "amount": self.amount,
            "volume_hand": self.volume_hand,
            "opening_rush": self.opening_rush,
            "seal_amount": self.seal_amount,
        }


@dataclass(slots=True)
class AuctionFinal:
    trade_date: date
    symbol: str
    source: str
    available_at_ms: int
    open_price: float | None
    vwap: float | None
    open_volume: float | None
    open_amount: float | None
    pre_close: float | None
    turnover_rate: float | None
    volume_ratio: float | None
    open_change_pct: float | None
    quality_flags: list[str] = field(default_factory=list)

    def to_row(self) -> dict:
        return {
            "trade_date": self.trade_date,
            "symbol": self.symbol,
            "source": self.source,
            "available_at_ms": self.available_at_ms,
            "open_price": self.open_price,
            "vwap": self.vwap,
            "open_volume": self.open_volume,
            "open_amount": self.open_amount,
            "pre_close": self.pre_close,
            "turnover_rate": self.turnover_rate,
            "volume_ratio": self.volume_ratio,
            "open_change_pct": self.open_change_pct,
            "quality_flags": ",".join(self.quality_flags),
        }
