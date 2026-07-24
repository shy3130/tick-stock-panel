from types import SimpleNamespace

import polars as pl

from app.api import intraday


class _EmptyQuoteService:
    def get_index_quotes(self, symbols):
        return pl.DataFrame()


class _EmptyRepo:
    def execute_all(self, query, params):
        return []


class _UsDailyFallbackRepo:
    def execute_all(self, query, params):
        return [(".SPX.US", "2026-07-17", 7457.69, 7533.77)]


class _RealtimeProvider:
    def get_realtime(self, universes=None, symbols=None):
        rows = [
            {
                "symbol": "HSI.HK",
                "last_price": 25143.05,
                "prev_close": 24562.24,
                "change_amount": None,
                "change_pct": 0.0236,
                "timestamp": 1784530200000,
            },
            {
                "symbol": ".SPX.US",
                "last_price": 7457.69,
                "prev_close": 7533.77,
                "change_amount": -76.08,
                "change_pct": -0.0101,
                "timestamp": 1784494800000,
            },
            {"symbol": "000001.SH", "last_price": 3510.0, "change_pct": 0.01},
        ]
        requested = set(symbols or [])
        return [row for row in rows if not requested or row["symbol"] in requested]


def test_index_quotes_reads_requested_hk_us_symbols_from_realtime_provider(monkeypatch) -> None:
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "clickhouse")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: _RealtimeProvider())
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(quote_service=_EmptyQuoteService(), repo=_EmptyRepo())
        )
    )

    payload = intraday.index_quotes(request, "HSI.HK,.SPX.US")

    assert payload["source"] == "realtime"
    assert [row["symbol"] for row in payload["rows"]] == ["HSI.HK", ".SPX.US"]
    assert payload["rows"][0]["change_pct"] == 2.36
    assert payload["rows"][1]["change_pct"] == -1.01
    assert payload["rows"][0]["change_amount"] == 580.81
    assert payload["rows"][0]["timestamp"] == 1784530200000
    assert all(row["source"] == "realtime" for row in payload["rows"])


def test_index_quotes_marks_partial_realtime_and_daily_fallback_sources(monkeypatch) -> None:
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    provider = _RealtimeProvider()
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "clickhouse")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(
        custom_sources,
        "get_provider",
        lambda name: SimpleNamespace(
            get_realtime=lambda universes=None, symbols=None: [provider.get_realtime(symbols=["HSI.HK"])[0]]
        ),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(quote_service=_EmptyQuoteService(), repo=_UsDailyFallbackRepo())
        )
    )

    payload = intraday.index_quotes(request, "HSI.HK,.SPX.US")

    assert payload["source"] == "mixed"
    assert [row["symbol"] for row in payload["rows"]] == ["HSI.HK", ".SPX.US"]
    assert [row["source"] for row in payload["rows"]] == ["realtime", "index_daily"]
