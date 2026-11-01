"""Replay native responses through the installed SDK without loading its DLL.

The SDK turns both a valid empty result and a server error into empty pandas
frames. Exercise the actual SDK boundary, then provider math and persistence.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.plugins.tdxquant import client as tc
from app.plugins.tdxquant import provider as tp
from app.services import kline_sync

sdk = pytest.importorskip("tdxaidata.tdxaidata")

EVENT = {"ErrorId": 0, "Date": ["20240619"], "Type": ["1"],
         "Value": [["2", "5", "1", "2"]]}
AUTH_ERROR = {"ErrorId": -1, "Error": "TokenKey Insufficient Or Key Error TDX-test-secret"}


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setattr(tc.time, "sleep", lambda _: None)
    monkeypatch.setattr(tp, "_HIST_INTERVAL_S", 0)

    def make(responses, *, close_response=None):
        responses = list(responses)
        calls = []

        def events(*args):
            calls.append(args)
            value = responses.pop(0) if len(responses) > 1 else responses[0]
            return json.dumps(value)

        closes = close_response if close_response is not None else {
            "ErrorId": 0, "Date": ["20240618", "20240619"], "Close": ["10", "8.35"],
        }
        native = SimpleNamespace(get_cw_data=events, get_his_dats=lambda *a: json.dumps(closes))

        class ReplaySDK(sdk.tqs):
            @classmethod
            def _tdx(cls):
                return native

        client = object.__new__(tc.TdxQuantClient)
        client.api_key = "TDX-test-secret"
        client._tqs = ReplaySDK
        client._native_cls = SimpleNamespace(get_instance=lambda: native)
        return client, calls

    return make


def test_event_values_and_dates_match_sdk_dataframe(make_client):
    client, calls = make_client([EVENT])
    expected = client._tqs.get_divid_factors("600000.SH", "2024-01-01", "2024-12-31")
    assert not expected.empty  # The normal nonempty DataFrame was already supported.
    actual = client.divid_factors("600000.SH", date(2024, 1, 1), date(2024, 12, 31))
    row = expected.reset_index().to_dict("records")[0]
    row["Date"] = tp._compact_date(row["Date"])
    actual[0]["Date"] = tp._compact_date(actual[0]["Date"])
    assert actual == [row]
    assert calls[-1] == ("600000.SH", "2024-01-01 00:00:00", "2024-12-31 00:00:00")


@pytest.mark.parametrize("payload", [{"ErrorId": 0}, {"ErrorId": 0, "Date": [], "Type": [], "Value": []}])
def test_valid_empty_sdk_frame_means_no_events(make_client, payload):
    client, _ = make_client([payload])
    assert client._tqs.get_divid_factors("000002.SZ").empty
    assert client.divid_factors("000002.SZ") == []


def test_auth_failure_is_preserved_without_retry_or_secret_output(make_client, capsys):
    client, calls = make_client([AUTH_ERROR])
    with pytest.raises(tc.TdxQuantAuthError, match=r"凭据|额度") as caught:
        client.divid_factors("000002.SZ")
    assert len(calls) == 1
    assert "TDX-test-secret" not in str(caught.value)
    assert "TDX-test-secret" not in capsys.readouterr().out


def test_transient_server_error_retries_and_recovers(make_client):
    client, calls = make_client([{"ErrorId": 500, "Error": "server busy"}, EVENT])
    assert len(client.divid_factors("600000.SH")) == 1
    assert len(calls) == 2


@pytest.mark.parametrize("payload", [None, [], {"unexpected": 1},
    {"Date": ["20240619"], "Type": ["1"], "Value": []},
    {"Date": ["20240619"], "Type": ["1"], "Value": [["invalid", 0, 0, 0]]},
    {"Date": ["20240619"], "Type": ["1"], "Value": [["NaN", 0, 0, 0]]},
    {"Date": ["20240619"], "Type": ["1"], "Value": [["Infinity", 0, 0, 0]]},
])
def test_malformed_response_remains_a_failure(make_client, payload):
    client, _ = make_client([payload])
    with pytest.raises(tc.TdxQuantError):
        client.divid_factors("600000.SH")


def test_date_window_is_inclusive_and_empty_window_is_valid(make_client):
    client, _ = make_client([EVENT])
    assert len(client.divid_factors("600000.SH", date(2024, 6, 19), date(2024, 6, 19))) == 1
    assert client.divid_factors("600000.SH", date(2024, 6, 20)) == []


def test_normalization_matches_sdk_without_pandas(make_client, monkeypatch):
    monkeypatch.setattr(sdk, "pd", None)
    client, _ = make_client([EVENT])
    assert client.divid_factors("600000.SH") == client._tqs.get_divid_factors("600000.SH")


def route_to(client, monkeypatch):
    from app.data_providers import custom

    provider = tp.TdxQuantProvider()
    provider._client = client
    monkeypatch.setattr(kline_sync.preferences, "get_adj_factor_provider", lambda: "tdxquant")
    monkeypatch.setattr(custom, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom, "get_provider", lambda name: provider)
    monkeypatch.setattr(kline_sync, "get_client", lambda: pytest.fail("不得跨源请求 TickFlow"))
    return provider


def test_events_flow_to_parquet_with_existing_per_share_formula(make_client, monkeypatch, tmp_path):
    client, _ = make_client([EVENT])
    route_to(client, monkeypatch)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    added, affected = kline_sync.sync_adj_factor(
        ["600000.SH"], repo, SimpleNamespace(has=lambda _: False),
        start_time=datetime(2024, 1, 1), end_time=datetime(2024, 12, 31),
    )
    assert added == 1 and affected == ["600000.SH"]
    row = pl.read_parquet(tmp_path / "adj_factor/all.parquet").row(0, named=True)
    assert row["trade_date"] == date(2024, 6, 19)
    assert row["dividend"] == pytest.approx(0.2)
    assert row["bonus"] == pytest.approx(0.1)
    assert row["allot"] == pytest.approx(0.2)
    assert row["allot_price"] == 5.0
    assert row["prev_close"] == 10.0
    assert row["ex_factor"] == pytest.approx(10.0 / 8.31)


def test_partial_failure_keeps_existing_factors_and_stops_remaining_symbols(make_client, monkeypatch, tmp_path):
    client, calls = make_client([EVENT, AUTH_ERROR])
    route_to(client, monkeypatch)
    path = tmp_path / "adj_factor/all.parquet"
    path.parent.mkdir()
    pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [date(2023, 1, 1)], "ex_factor": [1.02]}).write_parquet(path)
    before = path.read_bytes()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(tc.TdxQuantAuthError):
        kline_sync.sync_adj_factor(["600000.SH", "000002.SZ", "600519.SH"], repo, None,
                                   end_time=datetime(2024, 12, 31))
    assert len(calls) == 2
    assert path.read_bytes() == before


def test_adj_dataset_probe_exposes_server_failure(make_client, monkeypatch):
    client, calls = make_client([AUTH_ERROR])
    provider = route_to(client, monkeypatch)
    result = provider.test_dataset("adj_factor", ["000002.SZ"])
    assert result["rows"] == 0 and "凭据" in result["error"]
    assert len(calls) == 1


def test_price_lookup_failure_is_not_reported_as_successful_zero_rows(make_client, monkeypatch):
    client, _ = make_client([EVENT], close_response=AUTH_ERROR)
    provider = route_to(client, monkeypatch)
    with pytest.raises(tc.TdxQuantAuthError):
        provider.get_adj_factors(["600000.SH"], None, datetime(2024, 12, 31))
