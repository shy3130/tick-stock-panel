"""Fuyao 股票批量历史日K: 原价契约、按需路由与旧接口兜底。"""
from __future__ import annotations

import itertools
from datetime import date, datetime

import httpx
import polars as pl
import pytest

from app.data_providers.base import ProviderRequestError
from app.plugins.fuyao import client as fc
from app.plugins.fuyao import provider as fp
from app.services import kline_sync
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 11, 6)
START = fp._ms_of_date(DAY)


def _bar(day=DAY, close=11.63):
    return {
        "date_ms": fp._ms_of_date(day), "open_price": close - 0.1,
        "high_price": close + 0.2, "low_price": close - 0.2,
        "close_price": close, "volume": 1612611, "turnover": 18754665.93,
    }


def _payload(groups, **changes):
    return {"code": 0, "data": {"interval": "1d", "adjust": "none", "item": groups, **changes}}


def _group(symbol, bars=None):
    return {"thscode": symbol, "timestamp": START, "item": [_bar()] if bars is None else bars}


def _http_client(monkeypatch, payload, status=200):
    calls = []
    real_client = httpx.Client

    def respond(request):
        calls.append(request)
        if callable(payload):
            return payload(request)
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(fc.httpx, "Client", lambda **kw: real_client(
        **kw, transport=httpx.MockTransport(respond),
    ))
    return fc.FuyaoClient(api_key="test-key"), calls


def test_client_batch_preserves_group_identity_and_empty_vs_missing(monkeypatch):
    payload = _payload([_group("600519.SH", []), _group("000001.SZ")])
    client, calls = _http_client(monkeypatch, payload)
    try:
        result = client.historical_kline_batch(["000001.SZ", "600519.SH", "000002.SZ"], START, START)
    finally:
        client.close()
    assert result == {"600519.SH": [], "000001.SZ": [_bar()]}
    assert "000002.SZ" not in result
    assert calls[0].url.path == "/api/a-share/prices/historical-batch"
    assert dict(calls[0].url.params) == {
        "thscodes": "000001.SZ,600519.SH,000002.SZ", "interval": "1d",
        "adjust": "none", "start": str(START), "end": str(START),
    }
    assert calls[0].headers["X-api-key"] == "test-key"


def test_client_batch_enforces_ten_symbol_limit_without_truncating(monkeypatch):
    client, calls = _http_client(monkeypatch, _payload([]))
    try:
        assert client.historical_kline_batch([], START, START) == {}
        client.historical_kline_batch([f"{i:06d}.SZ" for i in range(10)], START, START)
        with pytest.raises(fc.FuyaoError, match="10"):
            client.historical_kline_batch([f"{i:06d}.SZ" for i in range(11)], START, START)
    finally:
        client.close()
    assert len(calls) == 1
    assert len(calls[0].url.params["thscodes"].split(",")) == 10


@pytest.mark.parametrize("changes", [
    {"adjust": "forward"}, {"adjust": "backward"}, {"adjust": None},
    {"interval": "1w"}, {"interval": None}, {"item": None}, {"item": {}},
])
def test_client_batch_rejects_wrong_price_contract(monkeypatch, changes):
    client, _ = _http_client(monkeypatch, _payload([_group("000001.SZ")], **changes))
    try:
        with pytest.raises(fc.FuyaoError):
            client.historical_kline_batch(["000001.SZ"], START, START)
    finally:
        client.close()


@pytest.mark.parametrize("groups", [
    [None], [_group("600519.SH")], [_group("000001.SZ"), _group("000001.SZ")],
    [{"thscode": "000001.SZ", "item": None}],
    [_group("000001.SZ", [None])],
    [_group("000001.SZ", [{"date_ms": START}])],
    [_group("000001.SZ", [_bar(date(2026, 11, 5))])],
])
def test_client_batch_rejects_malformed_or_unrequested_rows(monkeypatch, groups):
    client, _ = _http_client(monkeypatch, _payload(groups))
    try:
        with pytest.raises(fc.FuyaoError):
            client.historical_kline_batch(["000001.SZ"], START, START)
    finally:
        client.close()


@pytest.mark.parametrize(("status", "payload"), [
    (403, {"code": 2002}), (404, {}), (200, {"code": 4001, "message": "限频", "data": None}),
    (200, {"code": 0, "data": None}),
])
def test_client_batch_errors_are_not_successful_empty_data(monkeypatch, status, payload):
    client, _ = _http_client(monkeypatch, payload, status)
    try:
        with pytest.raises(fc.FuyaoError):
            client.historical_kline_batch(["000001.SZ"], START, START)
    finally:
        client.close()


def test_client_batch_network_failure_is_explicit(monkeypatch):
    def respond(request):
        raise httpx.ConnectError("connection lost", request=request)

    client, _ = _http_client(monkeypatch, respond)
    try:
        with pytest.raises(fc.FuyaoError, match="网络请求失败"):
            client.historical_kline_batch(["000001.SZ"], START, START)
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["wrong_adjust", "malformed", "not_found"])
def test_provider_falls_back_from_rejected_http_batch_without_changing_price(monkeypatch, failure):
    def respond(request):
        if request.url.path.endswith("historical-batch"):
            if failure == "not_found":
                return httpx.Response(404, json={})
            groups = [_group("000001.SZ", [{"date_ms": START}])] if failure == "malformed" else [_group("000001.SZ")]
            return httpx.Response(200, json=_payload(groups, adjust="forward" if failure == "wrong_adjust" else "none"))
        return httpx.Response(200, json={"code": 0, "data": {"item": [_bar()]}})

    client, calls = _http_client(monkeypatch, respond)
    try:
        result = _provider(monkeypatch, client).get_daily(["000001.SZ"], datetime(2026, 11, 6), datetime(2026, 11, 6))
    finally:
        client.close()
    assert result["close"].to_list() == [11.63]
    assert result["volume"].to_list() == [16126.0]
    assert [r.url.path for r in calls] == ["/api/a-share/prices/historical-batch", "/api/a-share/prices/historical"]
    assert all(r.url.params["adjust"] == "none" for r in calls)


class _BatchClient:
    def __init__(self, bars, *, missing=(), errors=(), batch_error=False):
        self.bars = bars
        self.missing = set(missing)
        self.errors = set(errors)
        self.batch_error = batch_error
        self.batch_calls = []
        self.single_calls = []

    def historical_kline_batch(self, symbols, start_ms, end_ms):
        assert len(symbols) <= 10
        self.batch_calls.append((list(symbols), start_ms, end_ms))
        if self.batch_error:
            raise fc.FuyaoError("批量端点不可用")
        return {symbol: [b for b in self.bars.get(symbol, []) if start_ms <= b["date_ms"] <= end_ms]
                for symbol in reversed(symbols) if symbol not in self.missing}

    def historical_kline(self, symbol, start_ms, end_ms, adjust="none"):
        self.single_calls.append((symbol, start_ms, end_ms, adjust))
        if symbol in self.errors:
            raise fc.FuyaoError("旧接口无权限")
        return [b for b in self.bars.get(symbol, []) if start_ms <= b["date_ms"] <= end_ms]


def _provider(monkeypatch, client):
    provider = fp.FuyaoProvider()
    provider._client = client
    monkeypatch.setattr(fp, "_HIST_INTERVAL_S", 0)
    return provider


def _no_dump(*args):
    raise AssertionError("少量标的不得触发整包下载")


def test_small_query_prefers_batch_and_preserves_raw_price_units_and_date(monkeypatch):
    bars = {"000001.SZ": [_bar(close=11.63)], "600519.SH": [_bar(close=1480.12)]}
    client = _BatchClient(bars)
    provider = _provider(monkeypatch, client)
    provider._daily10_for_window = _no_dump
    provider._daily_dump_info = _no_dump
    progress = []
    df = provider.get_daily(list(bars), datetime(2026, 11, 6), datetime(2026, 11, 6),
                            on_chunk_done=lambda cur, total: progress.append((cur, total)))
    assert df.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert df.schema["date"] == pl.Date
    assert dict(zip(df["symbol"], df["close"], strict=True)) == {"000001.SZ": 11.63, "600519.SH": 1480.12}
    assert df["volume"].to_list() == [16126.0, 16126.0]
    assert df["amount"].to_list() == [18754665.93, 18754665.93]
    assert df["date"].to_list() == [DAY, DAY]
    assert client.single_calls == []
    assert client.batch_calls == [(list(bars), START, START)]
    assert progress == [(1, 1)]


def test_duplicate_symbols_are_fetched_and_emitted_once(monkeypatch):
    client = _BatchClient({"000001.SZ": [_bar()], "600519.SH": [_bar()]})
    provider = _provider(monkeypatch, client)
    df = provider.get_daily(["600519.SH", "000001.SZ", "600519.SH"], datetime(2026, 11, 6), datetime(2026, 11, 6))
    assert client.batch_calls[0][0] == ["600519.SH", "000001.SZ"]
    assert df.height == 2


@pytest.mark.parametrize("count", [10, 11, 51])
def test_api_fallback_is_batched_and_frames_remain_bounded(monkeypatch, count):
    symbols = [f"{i + 1:06d}.SZ" for i in range(count)]
    client = _BatchClient({symbol: [_bar()] for symbol in symbols})
    provider = _provider(monkeypatch, client)
    provider._daily_dump_info = lambda: None
    progress = []
    frames = list(provider.iter_daily(symbols, datetime(2026, 10, 1), datetime(2026, 11, 6),
                                     on_chunk_done=lambda cur, total: progress.append((cur, total))))
    assert max(len(call[0]) for call in client.batch_calls) <= 10
    assert list(itertools.chain.from_iterable(call[0] for call in client.batch_calls)) == symbols
    assert max(frame.height for frame in frames) <= fp._HIST_SYMBOL_BATCH
    assert sum(frame.height for frame in frames) == count
    assert progress[-1] == (len(frames), len(frames))
    assert client.single_calls == []


@pytest.mark.parametrize("batch_error", [False, True])
def test_batch_missing_groups_or_failure_use_old_endpoint_only_when_needed(monkeypatch, batch_error):
    client = _BatchClient({"000001.SZ": [_bar()], "600519.SH": [_bar(close=1480.12)]},
                          missing=["600519.SH"], batch_error=batch_error)
    df = _provider(monkeypatch, client).get_daily(
        ["000001.SZ", "600519.SH"], datetime(2026, 11, 6), datetime(2026, 11, 6))
    assert df.height == 2
    assert [c[0] for c in client.single_calls] == (["000001.SZ", "600519.SH"] if batch_error else ["600519.SH"])
    assert all(c[3] == "none" for c in client.single_calls)


def test_valid_empty_group_does_not_trigger_fallback(monkeypatch):
    client = _BatchClient({"000001.SZ": []})
    result = _provider(monkeypatch, client).get_daily(["000001.SZ"], datetime(2026, 11, 6), datetime(2026, 11, 6))
    assert result.is_empty()
    assert client.single_calls == []


def test_settings_daily_preview_surfaces_terminal_fallback_error(monkeypatch):
    client = _BatchClient({}, missing=["000001.SZ"], errors=["000001.SZ"])
    result = _provider(monkeypatch, client).test_dataset("daily", ["000001.SZ"])
    assert result["rows"] == 0
    assert "000001.SZ" in result["error"]
    assert "旧接口无权限" in result["error"]


def test_deep_batch_window_splits_without_overlap_or_duplicate_days(monkeypatch):
    client = _BatchClient({"000001.SZ": [_bar(date(2001, 1, 1)), _bar()]})
    provider = _provider(monkeypatch, client)
    df = provider.get_daily(["000001.SZ"], datetime(2001, 1, 1), datetime(2026, 11, 6))
    assert len(client.batch_calls) == 3
    assert all(end - start < fp._HIST_MAX_SPAN_MS for _, start, end in client.batch_calls)
    assert all(a[2] + 1 == b[1] for a, b in itertools.pairwise(client.batch_calls))
    assert df["date"].to_list() == [date(2001, 1, 1), DAY]


@pytest.mark.parametrize("asset_type", ["index", "etf"])
def test_non_stock_daily_routing_is_unchanged(monkeypatch, asset_type):
    client = _BatchClient({})
    provider = _provider(monkeypatch, client)
    provider._daily10_for_window = _no_dump
    provider._daily_dump_info = _no_dump
    assert provider.get_daily(["000001.SH"], None, None, asset_type=asset_type).is_empty()
    assert client.batch_calls == client.single_calls == []


def test_bulk_recent_query_keeps_dump_route(monkeypatch):
    symbols = [f"{i + 1:06d}.SZ" for i in range(11)]
    client = _BatchClient({})
    provider = _provider(monkeypatch, client)
    dump = pl.DataFrame([{"thscode": s, "adjusted": "none", **_bar()} for s in symbols])
    provider._daily10_for_window = lambda *_: dump
    provider._daily_dump_info = _no_dump
    result = provider.get_daily(symbols, datetime(2026, 11, 6), datetime(2026, 11, 6))
    assert result.height == len(symbols)
    assert client.batch_calls == client.single_calls == []


def test_terminal_fallback_failure_rolls_back_staging_and_keeps_existing_data(monkeypatch, tmp_path):
    symbols = [f"{i + 1:06d}.SZ" for i in range(51)]
    client = _BatchClient({symbol: [_bar()] for symbol in symbols}, missing=[symbols[-1]], errors=[symbols[-1]])
    provider = _provider(monkeypatch, client)
    provider._daily_dump_info = lambda: None
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "fuyao")
    from app.data_providers import custom
    monkeypatch.setattr(custom, "provider_has_dataset", lambda *_: True)
    monkeypatch.setattr(custom, "get_provider", lambda _: provider)
    repo = KlineRepository(DataStore(tmp_path))
    target = tmp_path / "kline_daily" / f"date={DAY}" / "part.parquet"
    target.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600519.SH"], "date": [DAY], "close": [1480.12]}).write_parquet(target)
    before = target.read_bytes()
    with pytest.raises(ProviderRequestError, match=symbols[-1]):
        kline_sync.sync_and_persist_daily_batch(symbols, repo, object(),
                                               start_date=datetime(2026, 10, 1), end_date=datetime(2026, 11, 6))
    assert len(client.batch_calls) == 6
    assert target.read_bytes() == before
    assert not (tmp_path / ".daily_sync_staging").exists()


def _unknown_symbol_response(request):
    unknown = "000004.SZ"
    if request.url.path.endswith("historical-batch"):
        symbols = request.url.params["thscodes"].split(",")
        if unknown not in symbols:
            bar = {**_bar(), "date_ms": int(request.url.params["start"])}
            return httpx.Response(200, json=_payload([_group(symbol, [bar]) for symbol in symbols]))
    elif request.url.params["thscode"] != unknown:
        bar = {**_bar(), "date_ms": int(request.url.params["start"])}
        return httpx.Response(200, json={"code": 0, "data": {"item": [bar]}})
    return httpx.Response(200, json={"code": 1002, "message": f"Unknown thscode: {unknown}"})


def test_unknown_symbol_does_not_discard_other_stocks(monkeypatch, caplog):
    client, calls = _http_client(monkeypatch, _unknown_symbol_response)
    try:
        df = _provider(monkeypatch, client).get_daily(
            ["000004.SZ", "000001.SZ"], datetime(2026, 11, 6), datetime(2026, 11, 6),
        )
    finally:
        client.close()
    assert df["symbol"].to_list() == ["000001.SZ"]
    assert df["close"].to_list() == [11.63]
    assert df["volume"].to_list() == [16126.0]
    assert "000004.SZ" in caplog.text and "未覆盖" in caplog.text
    assert len(calls) == 3  # 整批拒绝后仅在源内单票兜底,不跨数据源。


def test_unknown_symbol_is_not_retried_in_later_history_windows(monkeypatch):
    client, calls = _http_client(monkeypatch, _unknown_symbol_response)
    try:
        df = _provider(monkeypatch, client).get_daily(
            ["000004.SZ", "000001.SZ"], datetime(1990, 1, 1), datetime(2026, 11, 6),
        )
    finally:
        client.close()
    assert set(df["symbol"]) == {"000001.SZ"}
    unknown_calls = [r for r in calls if r.url.params.get("thscode") == "000004.SZ"]
    batches = [r for r in calls if r.url.path.endswith("historical-batch")]
    assert len(unknown_calls) == 1 and len(batches) > 1
    assert all(r.url.params["thscodes"] == "000001.SZ" for r in batches[1:])


@pytest.mark.parametrize(("code", "message"), [
    (1002, "invalid start parameter"),
    (1002, "Unknown thscode: 600519.SH"),
    (1001, "Unknown thscode: 000004.SZ"),
    (4001, "频率超限"),
])
def test_other_errors_are_not_misclassified_as_unsupported_symbols(monkeypatch, code, message):
    client, _ = _http_client(monkeypatch, {"code": code, "message": message})
    try:
        with pytest.raises(ProviderRequestError, match="补查失败"):
            _provider(monkeypatch, client).get_daily(
                ["000004.SZ"], datetime(2026, 11, 6), datetime(2026, 11, 6),
            )
    finally:
        client.close()


@pytest.mark.parametrize("code", [1002, "1002"])
def test_single_history_retains_structured_unknown_symbol_error(monkeypatch, code):
    client, _ = _http_client(monkeypatch, {"code": code, "message": "Unknown thscode: 000004.SZ"})
    try:
        with pytest.raises(fc.FuyaoUnknownSymbolError) as caught:
            client.historical_kline("000004.SZ", START, START)
    finally:
        client.close()
    assert caught.value.symbol == "000004.SZ"
    assert "Unknown thscode: 000004.SZ" in str(caught.value)


def test_unknown_symbol_preserves_local_history_and_reports_zero_rows(monkeypatch, tmp_path):
    from polars.testing import assert_frame_equal

    client, _ = _http_client(monkeypatch, _unknown_symbol_response)
    repo = KlineRepository(DataStore(tmp_path))
    provider = _provider(monkeypatch, client)
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "fuyao")
    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", lambda *_: True)
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda _: provider)
    target = tmp_path / "kline_daily" / f"date={DAY}" / "part.parquet"
    target.parent.mkdir(parents=True)
    old = pl.DataFrame({"symbol": ["000004.SZ"], "date": [DAY], "close": [9.0]})
    old.write_parquet(target)
    zero, failed = [], []
    try:
        written = kline_sync.sync_and_persist_daily_batch(
            ["000004.SZ", "000001.SZ"], repo, object(),
            start_date=datetime(2026, 11, 6), end_date=datetime(2026, 11, 6),
            zero_row_out=zero, failed_out=failed,
        )
    finally:
        client.close()
    assert written == 1 and zero == ["000004.SZ"] and failed == []
    stored = pl.concat([pl.read_parquet(p) for p in target.parent.glob("*.parquet")], how="diagonal_relaxed")
    assert set(stored["symbol"]) == {"000004.SZ", "000001.SZ"}
    assert_frame_equal(stored.filter(pl.col("symbol") == "000004.SZ").select(old.columns), old)
