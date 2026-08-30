"""竞价数据源协议与 TickFlow 降级适配。"""

from __future__ import annotations

import logging
from datetime import date

from app.auction.contracts import (
    AuctionFinal,
    AuctionSnapshot,
    UnmatchedSide,
    datetime_to_ms,
    hhmmss_from_ms,
    is_open_auction_point,
    source_time_ms,
    time_label_to_hhmmss,
    unmatched_side_from_raw,
)
from app.market_time import cn_now

logger = logging.getLogger(__name__)

CAP_SERIES = "series"
CAP_FINALS = "finals"
CAP_MARKET_RANK = "market_rank"


def source_capabilities(source) -> frozenset[str]:
    """过程序列 / 正式撮合 / 全市场排行是独立能力。未声明时按方法名推断, 空 stub 必须显式声明。"""
    declared = getattr(source, "auction_capabilities", None)
    if declared is not None:
        return frozenset(declared)
    caps: set[str] = set()
    if callable(getattr(source, "get_auction_series", None)):
        caps.add(CAP_SERIES)
    if callable(getattr(source, "get_auction_finals", None)):
        caps.add(CAP_FINALS)
    if callable(getattr(source, "get_market_rank", None)):
        caps.add(CAP_MARKET_RANK)
    return frozenset(caps)


def shares_to_hands(volume: object) -> float | None:
    """Tushare stk_auction.vol 文档为股 → 内部手。"""
    if volume is None:
        return None
    try:
        return float(volume) / 100.0
    except (TypeError, ValueError):
        return None


def as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def snapshot_from_point(
    *,
    trade_date: date,
    symbol: str,
    source: str,
    time_label: str | None,
    price: object,
    matched_volume: object,
    unmatched_volume: object,
    unmatched_side: object,
    received_at_ms: int | None = None,
    sequence: int = 0,
    quality_flags: list[str] | None = None,
    pre_close: object = None,
    matched_amount: object = None,
    historical: bool = False,
) -> AuctionSnapshot | None:
    hhmmss = time_label_to_hhmmss(time_label)
    if hhmmss is None or not is_open_auction_point(hhmmss):
        return None
    flags = list(quality_flags or [])
    if historical:
        flags.append("historical_backfill")
    # 历史回填: 点在其源时刻即已可用, received_at 用源时刻, 使 as_of 重放与
    # default_as_of_ms(历史日=09:25:30) 能正确包含过程点 (否则恒被 as_of 过滤)。
    if received_at_ms is None:
        received_at_ms = source_time_ms(trade_date, hhmmss)
    return AuctionSnapshot(
        trade_date=trade_date,
        symbol=symbol,
        source=source,
        source_time_ms=source_time_ms(trade_date, hhmmss),
        received_at_ms=received_at_ms,
        indicative_price=as_float(price),
        matched_volume=as_float(matched_volume),
        unmatched_volume=as_float(unmatched_volume),
        unmatched_side=unmatched_side_from_raw(unmatched_side),
        sequence=sequence,
        quality_flags=flags,
        pre_close=as_float(pre_close),
        matched_amount=as_float(matched_amount),
    )


def discover_auction_sources() -> list:
    """声明 auction 的插件。没有过程序列源时才追加 TickFlow 降级, 不混进已有过程源。"""
    found = []
    try:
        from app.data_providers import custom as custom_sources

        for name in custom_sources.names():
            if not custom_sources.provider_has_dataset(name, "auction"):
                continue
            provider = custom_sources.get_provider(name)
            if source_capabilities(provider):
                found.append(provider)
    except Exception as exc:
        logger.warning("discover auction plugins failed: %s", exc)
    if not any(CAP_SERIES in source_capabilities(source) for source in found):
        found.append(TickFlowAuctionSource())
    return found


class TickFlowAuctionSource:
    """用 preopen quotes 当指示价。缺匹配/未匹配则打标, 不伪造。"""

    name = "tickflow"
    auction_capabilities = (CAP_SERIES,)
    # 降级源: 只有指示价、无真实匹配/未匹配, 不能算"原生过程序列"。
    series_degraded = True

    def available(self) -> tuple[bool, str]:
        try:
            from app.tickflow.client import get_paid_realtime_client

            get_paid_realtime_client()
            return True, "ok"
        except Exception as exc:
            return False, str(exc)

    def get_auction_series(
        self,
        symbols: list[str],
        trade_date: date,
    ) -> list[AuctionSnapshot]:
        if not symbols:
            return []
        try:
            from app.tickflow.client import get_paid_realtime_client

            tf = get_paid_realtime_client()
            rows = tf.quotes.get(symbols=symbols, as_dataframe=False) or []
        except Exception as exc:
            logger.warning("tickflow auction quotes failed: %s", exc)
            return []
        now_ms = datetime_to_ms(cn_now())
        out: list[AuctionSnapshot] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            ts = row.get("timestamp") or row.get("ts") or now_ms
            try:
                source_ms = int(ts) if int(ts) > 10_000_000_000 else int(float(ts) * 1000)
            except (TypeError, ValueError):
                source_ms = now_ms
            if not is_open_auction_point(hhmmss_from_ms(source_ms)):
                # 非竞价时段不把连续盘 last 当成竞价点
                continue
            out.append(
                AuctionSnapshot(
                    trade_date=trade_date,
                    symbol=symbol,
                    source=self.name,
                    source_time_ms=source_ms,
                    received_at_ms=now_ms,
                    indicative_price=as_float(row.get("last_price") or row.get("last")),
                    matched_volume=None,
                    unmatched_volume=None,
                    unmatched_side=UnmatchedSide.unknown,
                    sequence=index,
                    quality_flags=["missing_matched_volume", "missing_unmatched"],
                    pre_close=as_float(row.get("prev_close") or row.get("pre_close")),
                )
            )
        return out

    def get_auction_finals(
        self,
        symbols: list[str] | None,
        trade_date: date,
    ) -> list[AuctionFinal]:
        del symbols, trade_date
        return []
