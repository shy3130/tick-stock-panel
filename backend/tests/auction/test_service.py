from datetime import date
from pathlib import Path

from app.auction.contracts import (
    AuctionFinal,
    AuctionSnapshot,
    MarketRankItem,
    UnmatchedSide,
    source_time_ms,
)
from app.auction.repository import AuctionRepository
from app.auction.service import AuctionHubService
from app.services.quote_service import QuoteSubscriber


class FakeAuctionSource:
    name = "eltdx"
    auction_capabilities = ("series", "finals")

    def __init__(self, snapshots, finals=None):
        self._snapshots = snapshots
        self._finals = finals or []

    def available(self):
        return True, "ok"

    def get_auction_series(self, symbols, trade_date):
        return [s for s in self._snapshots if s.symbol in symbols and s.trade_date == trade_date]

    def get_auction_finals(self, symbols, trade_date):
        items = [f for f in self._finals if f.trade_date == trade_date]
        if symbols:
            items = [f for f in items if f.symbol in symbols]
        return items


def test_poll_and_ranking_as_of(tmp_path: Path, monkeypatch):
    day = date(2026, 8, 20)
    monkeypatch.setattr("app.auction.service.cn_today", lambda: day)
    snaps = [
        AuctionSnapshot(
            trade_date=day,
            symbol="000001.SZ",
            source="eltdx",
            source_time_ms=source_time_ms(day, 91800),
            received_at_ms=source_time_ms(day, 91801),
            indicative_price=10.4,
            matched_volume=300,
            unmatched_volume=80,
            unmatched_side=UnmatchedSide.buy,
            pre_close=10.0,
        )
    ]
    hub = AuctionHubService(AuctionRepository(tmp_path), [FakeAuctionSource(snaps)])
    monkeypatch.setattr(hub, "_universe", lambda: ["000001.SZ"])
    result = hub.poll_once(day)
    assert result["snapshots"] == 1
    ranked = hub.rankings(trade_date=day, as_of_ms=source_time_ms(day, 91801), style="momentum")
    assert ranked["rows"][0]["symbol"] == "000001.SZ"
    late = hub.rankings(trade_date=day, as_of_ms=source_time_ms(day, 91500), style="momentum")
    assert late["rows"] == []


def test_clear_alerts_keeps_auction_flags():
    sub = QuoteSubscriber()
    sub.notify_auction_updated()
    sub.clear_alerts()
    data = sub.pop()
    assert data["auction_updated"] is True


def test_final_not_visible_before_available(tmp_path: Path):
    day = date(2026, 8, 20)
    finals = [
        AuctionFinal(
            trade_date=day,
            symbol="000001.SZ",
            source="tushare",
            available_at_ms=source_time_ms(day, 92530),
            open_price=10.5,
            vwap=10.4,
            open_volume=100,
            open_amount=105000,
            pre_close=10.0,
            turnover_rate=1.0,
            volume_ratio=2.0,
            open_change_pct=0.05,
        )
    ]
    hub = AuctionHubService(AuctionRepository(tmp_path), [FakeAuctionSource([], finals)])
    hub.repo.upsert_finals(finals)
    series = hub.series("000001.SZ", day, as_of_ms=source_time_ms(day, 92000))
    assert series["finals"] == []
    series2 = hub.series("000001.SZ", day, as_of_ms=source_time_ms(day, 92530))
    assert series2["finals"]


class FakeFinalsOnlySource:
    name = "tushare"
    auction_capabilities = ("finals",)
    auction_finals_universe = True

    def __init__(self, finals):
        self._finals = finals
        self.series_calls = 0

    def available(self):
        return True, "ok"

    def get_auction_series(self, symbols, trade_date):
        self.series_calls += 1
        return []

    def get_auction_finals(self, symbols, trade_date):
        assert symbols is None
        return [f for f in self._finals if f.trade_date == trade_date]


def test_finals_only_source_skips_series_and_ranks_open(tmp_path: Path):
    day = date(2026, 8, 20)
    finals = [
        AuctionFinal(
            trade_date=day,
            symbol="000001.SZ",
            source="tushare",
            available_at_ms=source_time_ms(day, 92530),
            open_price=10.5,
            vwap=10.4,
            open_volume=100,
            open_amount=105000,
            pre_close=10.0,
            turnover_rate=1.0,
            volume_ratio=2.0,
            open_change_pct=0.05,
        )
    ]
    src = FakeFinalsOnlySource(finals)
    hub = AuctionHubService(AuctionRepository(tmp_path), [src])
    result = hub.poll_once(day)
    assert src.series_calls == 0
    assert result["finals"] == 1
    assert result["degraded"] is True
    ranked = hub.rankings(trade_date=day, as_of_ms=source_time_ms(day, 92530), style="momentum")
    assert ranked["rows"][0]["symbol"] == "000001.SZ"
    assert "finals_only" in (ranked["rows"][0].get("quality_flags") or [])


def test_discover_skips_tickflow_when_series_plugin_exists(monkeypatch):
    from app.auction.sources import TickFlowAuctionSource, discover_auction_sources

    class _Eltdx:
        name = "eltdx"
        auction_capabilities = ("series", "finals")

    class _Custom:
        @staticmethod
        def names():
            return ["eltdx"]

        @staticmethod
        def provider_has_dataset(name, dataset):
            return name == "eltdx" and dataset == "auction"

        @staticmethod
        def get_provider(name):
            return _Eltdx()

    import app.data_providers.custom as custom

    monkeypatch.setattr(custom, "names", _Custom.names)
    monkeypatch.setattr(custom, "provider_has_dataset", _Custom.provider_has_dataset)
    monkeypatch.setattr(custom, "get_provider", _Custom.get_provider)
    found = discover_auction_sources()
    assert [s.name for s in found] == ["eltdx"]
    assert not any(isinstance(s, TickFlowAuctionSource) for s in found)


def test_discover_adds_tickflow_when_only_finals_plugin(monkeypatch):
    from app.auction.sources import TickFlowAuctionSource, discover_auction_sources

    class _Tushare:
        name = "tushare"
        auction_capabilities = ("finals",)

    class _Custom:
        @staticmethod
        def names():
            return ["tushare"]

        @staticmethod
        def provider_has_dataset(name, dataset):
            return name == "tushare" and dataset == "auction"

        @staticmethod
        def get_provider(name):
            return _Tushare()

    import app.data_providers.custom as custom

    monkeypatch.setattr(custom, "names", _Custom.names)
    monkeypatch.setattr(custom, "provider_has_dataset", _Custom.provider_has_dataset)
    monkeypatch.setattr(custom, "get_provider", _Custom.get_provider)
    found = discover_auction_sources()
    names = [s.name for s in found]
    assert "tushare" in names
    assert any(isinstance(s, TickFlowAuctionSource) for s in found)


def test_available_probe_is_cached_within_ttl(tmp_path: Path):
    """available() 属昂贵网络探活, 轮询/请求热路径不应每次重放。"""
    calls = []

    class _ProbingSource:
        name = "eltdx"
        auction_capabilities = ("series", "finals")

        def available(self):
            calls.append(1)
            return True, "ok"

        def get_auction_series(self, symbols, trade_date):
            return []

        def get_auction_finals(self, symbols, trade_date):
            return []

    hub = AuctionHubService(AuctionRepository(tmp_path), [_ProbingSource()])
    hub.live_sources()
    hub.live_sources()
    hub.source_status()
    hub._capability_flags()
    assert len(calls) == 1


def test_historical_series_ranked_end_to_end(tmp_path: Path, monkeypatch):
    """历史回填过程点 (received_at=源时刻) 应进入排行, 不再退化到 finals-only。"""
    from app.auction.contracts import AuctionSnapshot, UnmatchedSide, source_time_ms
    from app.auction.service import AuctionHubService

    day = date(2026, 8, 20)
    monkeypatch.setattr("app.auction.service.cn_today", lambda: day)
    snaps = [
        AuctionSnapshot(
            trade_date=day, symbol="000001.SZ", source="eltdx",
            source_time_ms=source_time_ms(day, 91800),
            received_at_ms=source_time_ms(day, 91800),  # 历史回填: 源时刻
            indicative_price=10.2, matched_volume=300, unmatched_volume=80,
            unmatched_side=UnmatchedSide.buy, pre_close=10.0,
            quality_flags=["historical_backfill"],
        ),
        AuctionSnapshot(
            trade_date=day, symbol="000001.SZ", source="eltdx",
            source_time_ms=source_time_ms(day, 92200),
            received_at_ms=source_time_ms(day, 92200),
            indicative_price=10.4, matched_volume=600, unmatched_volume=50,
            unmatched_side=UnmatchedSide.buy, pre_close=10.0,
            quality_flags=["historical_backfill"],
        ),
    ]
    hub = AuctionHubService(AuctionRepository(tmp_path), [FakeAuctionSource(snaps)])
    monkeypatch.setattr(hub, "_universe", lambda: ["000001.SZ"])
    hub.poll_once(day)
    ranked = hub.rankings(trade_date=day, as_of_ms=source_time_ms(day, 92530))
    assert ranked["rows"]
    assert ranked["rows"][0]["symbol"] == "000001.SZ"
    assert ranked["rows"][0]["point_count"] == 2


class FakeMarketRankSource:
    name = "eltdx"
    auction_capabilities = ("series", "finals", "market_rank")

    def __init__(self, rows):
        self._rows = rows

    def available(self):
        return True, "ok"

    def get_auction_series(self, symbols, trade_date):
        return []

    def get_auction_finals(self, symbols, trade_date):
        return []

    def get_market_rank(self, *, sort_by="涨幅", count=200, ascending=False):
        return list(self._rows[:count])


def test_market_rank_changepct_decimal_and_endpoint(tmp_path: Path):
    rows = [
        MarketRankItem(
            symbol="688152.SH", name="麒麟信安", source="eltdx",
            change_pct=0.20, amount=266716624.0, volume_hand=73329.0,
            opening_rush=0.0, seal_amount=50094876.0,
        )
    ]
    hub = AuctionHubService(AuctionRepository(tmp_path), [FakeMarketRankSource(rows)])
    result = hub.market_rank(sort_by="涨幅", count=200)
    assert result["rows"][0]["symbol"] == "688152.SH"
    assert result["rows"][0]["change_pct"] == 0.20


def test_universe_merges_market_when_native_series(tmp_path: Path, monkeypatch):
    market_rows = [
        MarketRankItem(
            symbol="600519.SH", name="贵州茅台", source="eltdx",
            change_pct=0.02, amount=1.0, volume_hand=1.0,
            opening_rush=None, seal_amount=None,
        )
    ]
    hub = AuctionHubService(
        AuctionRepository(tmp_path), [FakeMarketRankSource(market_rows)],
        market_universe_count=200,
    )
    monkeypatch.setattr(hub, "_watchlist_symbols", lambda: ["000001.SZ"])
    universe = hub._universe()
    assert "000001.SZ" in universe
    assert "600519.SH" in universe


def test_universe_skips_market_when_count_zero(tmp_path: Path):
    hub = AuctionHubService(
        AuctionRepository(tmp_path), [FakeMarketRankSource([])],
        market_universe_count=0,
    )
    assert hub.market_universe_count == 0
