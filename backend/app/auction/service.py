"""竞价中枢: 独立于实时行情开关的过程轮询 + 回放。"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime

from app.auction.contracts import (
    AuctionFinal,
    AuctionSnapshot,
    AuctionStage,
    MarketRankItem,
    auction_stage,
    default_as_of_ms,
    parse_trade_date,
)
from app.auction.features import build_finals_only_features, build_latest_features
from app.auction.ranking import rank_features
from app.auction.repository import AuctionRepository
from app.auction.sources import CAP_FINALS, CAP_MARKET_RANK, CAP_SERIES, source_capabilities
from app.market_time import cn_today

logger = logging.getLogger(__name__)

_POLL_SECONDS = 3.0

# 数据源可用性探活 (eltdx availability 会真实建连 TdxClient 并 codes.count) 属昂贵
# 网络操作, 不能进入 3s 轮询与 /status /rankings 热路径。探活结果按源名缓存, TTL 内复用。
_AVAIL_TTL_SECONDS = 30.0

# Tier 1 全市场初筛结果缓存 TTL (秒)。0x054B 取 top N 只需数页, 缓存几秒即可。
_MARKET_RANK_TTL_SECONDS = 5.0

# 全市场初筛默认排序字段与数量。change_pct 为实时涨跌幅 (非竞价缺口)。
DEFAULT_MARKET_SORT = "涨幅"
DEFAULT_MARKET_COUNT = 200


class AuctionHubService:
    def __init__(
        self,
        repo: AuctionRepository,
        sources: list | None = None,
        *,
        market_universe_count: int = DEFAULT_MARKET_COUNT,
    ) -> None:
        self.repo = repo
        self.sources = list(sources or [])
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._app_state = None
        self._avail_cache: dict[str, tuple[float, tuple[bool, str]]] = {}
        # Tier 1 初筛结果缓存: (monotonic_ts, rows)。并发读改写由 _universe 侧用快照。
        self._market_rank_cache: tuple[float, list[MarketRankItem]] | None = None
        self._market_rank_lock = threading.Lock()
        self.market_universe_count = max(0, int(market_universe_count))
        self.last_error: str | None = None
        self.last_poll_ms: int | None = None
        self.degraded = not self.live_sources()

    def set_app_state(self, state) -> None:
        self._app_state = state

    def _available(self, source) -> tuple[bool, str]:
        """TTL 缓存的可用性探活。避免每次轮询/请求都真实建连数据源。"""
        name = str(getattr(source, "name", id(source)))
        now = time.monotonic()
        cached = self._avail_cache.get(name)
        if cached is not None and now - cached[0] < _AVAIL_TTL_SECONDS:
            return cached[1]
        ok, reason = source.available()
        self._avail_cache[name] = (now, (ok, reason))
        return ok, reason

    def live_sources(self) -> list:
        live = []
        for source in self.sources:
            ok, reason = self._available(source)
            if ok:
                live.append(source)
            else:
                logger.info("auction source %s unavailable: %s", source.name, reason)
        return live

    def _capability_flags(self, live: list | None = None) -> tuple[bool, bool, bool]:
        sources = live if live is not None else self.live_sources()
        has_series = False
        has_native_series = False
        has_finals = False
        for source in sources:
            caps = source_capabilities(source)
            if CAP_SERIES in caps:
                has_series = True
                if not getattr(source, "series_degraded", False):
                    has_native_series = True
            if CAP_FINALS in caps:
                has_finals = True
        return has_series, has_native_series, has_finals

    def source_status(self) -> list[dict]:
        rows = []
        for source in self.sources:
            ok, reason = self._available(source)
            caps = source_capabilities(source)
            rows.append(
                {
                    "name": source.name,
                    "available": ok,
                    "reason": reason,
                    "series": CAP_SERIES in caps,
                    "finals": CAP_FINALS in caps,
                }
            )
        return rows

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="auction-hub", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                stage = auction_stage()
                if stage in {
                    AuctionStage.cancellable,
                    AuctionStage.locked,
                    AuctionStage.final,
                }:
                    self.poll_once()
            except Exception as exc:
                self.last_error = str(exc)
                logger.warning("auction hub poll failed: %s", exc)
            self._stop.wait(_POLL_SECONDS)

    def _watchlist_symbols(self) -> list[str]:
        symbols: list[str] = []
        try:
            from app.services import watchlist

            for row in watchlist.list_symbols() or []:
                symbol = str((row or {}).get("symbol") or "").strip().upper()
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
        except Exception as exc:
            logger.warning("auction universe watchlist failed: %s", exc)
        return symbols

    def market_rank(
        self,
        *,
        sort_by: str = DEFAULT_MARKET_SORT,
        count: int | None = None,
        ascending: bool = False,
    ) -> dict:
        """Tier 1 全市场实时排行初筛。缓存 TTL 内复用, 不落盘。"""
        rows = self._market_rank_rows(sort_by=sort_by, count=count, ascending=ascending)
        return {
            "sort_by": sort_by,
            "rows": [item.to_row() for item in rows],
            "degraded": self.degraded,
        }

    def _market_rank_rows(
        self,
        *,
        sort_by: str = DEFAULT_MARKET_SORT,
        count: int | None = None,
        ascending: bool = False,
    ) -> list[MarketRankItem]:
        now = time.monotonic()
        cached = self._market_rank_cache
        if (
            cached is not None
            and now - cached[0] < _MARKET_RANK_TTL_SECONDS
            and cached[1]
        ):
            return list(cached[1])
        rows: list[MarketRankItem] = []
        for source in self.live_sources():
            if CAP_MARKET_RANK not in source_capabilities(source):
                continue
            try:
                rows = source.get_market_rank(
                    sort_by=sort_by,
                    count=count or DEFAULT_MARKET_COUNT,
                    ascending=ascending,
                )
            except Exception as exc:
                logger.warning("auction market rank %s failed: %s", source.name, exc)
                continue
            if rows:
                break
        with self._market_rank_lock:
            self._market_rank_cache = (time.monotonic(), rows)
        return list(rows)

    def _universe(self) -> list[str]:
        """自选 U Tier 1 全市场初筛 top N。初筛仅当存在原生过程序列源时并入,
        避免降级源 (TickFlow) 或无过程源时盲目扩大轮询池导致漏点。"""
        _, has_native_series, _ = self._capability_flags()
        symbols = self._watchlist_symbols()
        if has_native_series and self.market_universe_count > 0:
            for item in self._market_rank_rows(count=self.market_universe_count):
                symbol = item.symbol
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
        return symbols

    def poll_once(self, trade_date: date | None = None) -> dict:
        day = trade_date or cn_today()
        live = self.live_sources()
        _, has_native_series, _ = self._capability_flags(live)
        self.degraded = not has_native_series
        symbols = self._universe()
        snapshots: list[AuctionSnapshot] = []
        finals: list[AuctionFinal] = []
        for source in live:
            caps = source_capabilities(source)
            try:
                if CAP_SERIES in caps and symbols:
                    snapshots.extend(source.get_auction_series(symbols, day))
                if CAP_FINALS in caps:
                    universe = bool(getattr(source, "auction_finals_universe", False))
                    targets = None if universe else symbols
                    if universe or targets:
                        finals.extend(source.get_auction_finals(targets, day))
            except Exception as exc:
                logger.warning("auction source %s poll failed: %s", source.name, exc)
        written_s = self.repo.append_snapshots(snapshots)
        written_f = self.repo.upsert_finals(finals)
        as_of = default_as_of_ms(day)
        features = build_latest_features(
            _snapshots_from_frame(self.repo.load_snapshots(day)),
            as_of_ms=as_of,
            trade_date=day,
        )
        self.last_poll_ms = as_of
        self.last_error = None
        self._notify()
        return {
            "snapshots": written_s,
            "finals": written_f,
            "features": len(features),
            "degraded": self.degraded,
        }
    def _notify(self) -> None:
        qs = getattr(self._app_state, "quote_service", None) if self._app_state is not None else None
        if qs is not None:
            qs.notify_auction_updated()

    def status(self, trade_date: str | date | None = None) -> dict:
        day = parse_trade_date(trade_date) if trade_date else cn_today()
        has_series, has_native_series, has_finals = self._capability_flags()
        self.degraded = not has_native_series
        return {
            "trade_date": day.isoformat(),
            "stage": str(auction_stage()),
            "degraded": self.degraded,
            "has_series": has_series,
            "has_finals": has_finals,
            "sources": self.source_status(),
            "last_poll_ms": self.last_poll_ms,
            "last_error": self.last_error,
            "coverage": self.repo.coverage(),
        }

    def rankings(
        self,
        *,
        trade_date: str | date | None = None,
        as_of_ms: int | None = None,
        style: str = "momentum",
        limit: int = 50,
    ) -> dict:
        day = parse_trade_date(trade_date) if trade_date else cn_today()
        as_of = int(as_of_ms) if as_of_ms else default_as_of_ms(day)
        snaps = _snapshots_from_frame(self.repo.load_snapshots(day))
        features = build_latest_features(snaps, as_of_ms=as_of, trade_date=day)
        if not features:
            finals = _finals_from_frame(self.repo.load_finals(day))
            features = build_finals_only_features(finals, as_of_ms=as_of, trade_date=day)
        _, has_native_series, _ = self._capability_flags()
        self.degraded = not has_native_series
        rows = rank_features(features, style=style, limit=limit)
        names = _name_map()
        for row in rows:
            row["name"] = names.get(row["symbol"], row["symbol"])
        return {
            "trade_date": day.isoformat(),
            "as_of_ms": as_of,
            "style": style,
            "degraded": self.degraded,
            "rows": rows,
        }

    def series(self, symbol: str, trade_date: str | date | None = None, as_of_ms: int | None = None) -> dict:
        day = parse_trade_date(trade_date) if trade_date else cn_today()
        as_of = int(as_of_ms) if as_of_ms else default_as_of_ms(day)
        symbol = symbol.strip().upper()
        points = [
            s.to_row()
            for s in _snapshots_from_frame(self.repo.load_snapshots(day))
            if s.symbol == symbol and s.received_at_ms <= as_of
        ]
        for point in points:
            point["trade_date"] = day.isoformat()
            point["quality_flags"] = (
                point["quality_flags"].split(",") if point.get("quality_flags") else []
            )
        finals = [
            f.to_row()
            for f in _finals_from_frame(self.repo.load_finals(day))
            if f.symbol == symbol and f.available_at_ms <= as_of
        ]
        for item in finals:
            item["trade_date"] = day.isoformat()
            item["quality_flags"] = (
                item["quality_flags"].split(",") if item.get("quality_flags") else []
            )
        return {
            "symbol": symbol,
            "trade_date": day.isoformat(),
            "as_of_ms": as_of,
            "points": points,
            "finals": finals,
        }

    def refresh(self, trade_date: str | date | None = None) -> dict:
        day = parse_trade_date(trade_date) if trade_date else cn_today()
        return self.poll_once(day)


def _as_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if hasattr(value, "date"):
        return value.date()
    return parse_trade_date(str(value)[:10])


def _snapshots_from_frame(frame) -> list[AuctionSnapshot]:
    if frame is None or frame.is_empty():
        return []
    from app.auction.contracts import UnmatchedSide

    out: list[AuctionSnapshot] = []
    for row in frame.to_dicts():
        flags = row.get("quality_flags") or ""
        out.append(
            AuctionSnapshot(
                trade_date=_as_date(row["trade_date"]),
                symbol=row["symbol"],
                source=row["source"],
                source_time_ms=int(row["source_time_ms"]),
                received_at_ms=int(row["received_at_ms"]),
                indicative_price=row.get("indicative_price"),
                matched_volume=row.get("matched_volume"),
                unmatched_volume=row.get("unmatched_volume"),
                unmatched_side=UnmatchedSide(row.get("unmatched_side") or "unknown"),
                sequence=int(row.get("sequence") or 0),
                quality_flags=[f for f in str(flags).split(",") if f],
                pre_close=row.get("pre_close"),
                matched_amount=row.get("matched_amount"),
            )
        )
    return out


def _finals_from_frame(frame) -> list[AuctionFinal]:
    if frame is None or frame.is_empty():
        return []
    out: list[AuctionFinal] = []
    for row in frame.to_dicts():
        flags = row.get("quality_flags") or ""
        out.append(
            AuctionFinal(
                trade_date=_as_date(row["trade_date"]),
                symbol=row["symbol"],
                source=row["source"],
                available_at_ms=int(row["available_at_ms"]),
                open_price=row.get("open_price"),
                vwap=row.get("vwap"),
                open_volume=row.get("open_volume"),
                open_amount=row.get("open_amount"),
                pre_close=row.get("pre_close"),
                turnover_rate=row.get("turnover_rate"),
                volume_ratio=row.get("volume_ratio"),
                open_change_pct=row.get("open_change_pct"),
                quality_flags=[f for f in str(flags).split(",") if f],
            )
        )
    return out


def _name_map() -> dict[str, str]:
    try:
        from app.services import watchlist

        return {
            str(row.get("symbol") or "").upper(): str(row.get("name") or "")
            for row in watchlist.list_symbols() or []
        }
    except Exception:
        return {}
