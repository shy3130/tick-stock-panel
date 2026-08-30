"""TushareProvider 契约与单位标准化测试。

不依赖真实网络或 tushare SDK: 用假 pro_api 返回样例行, 验证字段映射、
股→手、均价/撮合价分离、小数制涨跌幅、能力声明、Key 优先级与设置页试拉。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.plugins.tushare import provider as tp
from app.plugins.tushare.provider import TushareProvider


class _FakeFrame:
    def __init__(self, rows: list[dict] | None):
        self._rows = rows

    @property
    def empty(self) -> bool:
        return not self._rows

    def to_dict(self, orient: str) -> list[dict]:
        assert orient == "records"
        return list(self._rows or [])


class _FakePro:
    def __init__(
        self,
        by_api: dict[str, list[dict] | None],
        error: Exception | None = None,
    ):
        self.by_api = by_api
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def stk_auction(self, trade_date: str):
        self.calls.append(("stk_auction", trade_date))
        if self.error:
            raise self.error
        rows = self.by_api.get("stk_auction")
        return None if rows is None else _FakeFrame(rows)

    def stk_auction_o(self, trade_date: str):
        self.calls.append(("stk_auction_o", trade_date))
        if self.error:
            raise self.error
        rows = self.by_api.get("stk_auction_o")
        return None if rows is None else _FakeFrame(rows)

    def stock_basic(self, **kwargs):
        self.calls.append(("stock_basic", kwargs.get("list_status", "")))
        if self.error:
            raise self.error
        rows = self.by_api.get("stock_basic")
        return None if rows is None else _FakeFrame(rows)


def _auction_row(**over) -> dict:
    row = {
        "ts_code": "000001.SZ",
        "vol": 45400,
        "amount": 505000.0,
        "price": 10.22,
        "pre_close": 10.0,
        "turnover_rate": 1.2,
        "volume_ratio": 3.0,
    }
    row.update(over)
    return row


def _open_row(**over) -> dict:
    row = {
        "ts_code": "000001.SZ",
        "close": 10.25,
        "vwap": 10.22,
        "vol": 45400,
        "amount": 505000.0,
        "pre_close": 10.0,
        "turnover_rate": 1.2,
        "volume_ratio": 3.0,
    }
    row.update(over)
    return row


def _provider_with(monkeypatch, by_api, error=None):
    fake = _FakePro(by_api, error=error)
    provider = TushareProvider()
    monkeypatch.setattr(provider, "_get_pro", lambda: fake)
    monkeypatch.setattr(tp, "get_api_key", lambda: "tok")
    return provider, fake


def test_datasets_declaration_auction_only():
    config = TushareProvider().config
    assert "auction" in config.datasets
    assert "daily" not in config.datasets
    assert "minute" not in config.datasets
    assert "realtime" not in config.datasets
    assert "financial" not in config.datasets
    assert TushareProvider.auction_capabilities == ("finals",)
    assert TushareProvider.auction_finals_universe is True


def test_secrets_field_matches_plugin_key_store():
    assert tp.SECRETS_FIELD == "tushare_api_key"
    assert tp.API_KEY_ENV == "TUSHARE_TOKEN"


def test_get_api_key_secrets_store_takes_priority(monkeypatch):
    from app import secrets_store

    monkeypatch.delenv(tp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {tp.SECRETS_FIELD: "tok-from-ui"})
    assert tp.get_api_key() == "tok-from-ui"


def test_get_api_key_falls_back_to_env(monkeypatch):
    from app import secrets_store

    monkeypatch.setenv(tp.API_KEY_ENV, "tok-from-env")
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    assert tp.get_api_key() == "tok-from-env"


def test_availability_accepts_secrets_store_key(monkeypatch):
    from app import secrets_store

    monkeypatch.setattr(tp, "_import_tushare", lambda: object())
    monkeypatch.delenv(tp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {tp.SECRETS_FIELD: "tok-from-ui"})
    assert tp.availability() == (True, "ok")


def test_availability_requires_token(monkeypatch):
    from app import secrets_store

    monkeypatch.setattr(tp, "_import_tushare", lambda: object())
    monkeypatch.delenv(tp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    ok, reason = tp.availability()
    assert ok is False and tp.API_KEY_ENV in reason


def test_availability_without_package(monkeypatch):
    def _boom():
        raise ImportError("no tushare")

    monkeypatch.setattr(tp, "_import_tushare", _boom)
    ok, reason = tp.availability()
    assert ok is False and "未安装" in reason


def test_vol_shares_to_hands_and_amount_yuan(monkeypatch):
    provider, _ = _provider_with(monkeypatch, {"stk_auction": [_auction_row()], "stk_auction_o": []})
    items = provider.get_auction_finals(None, date(2026, 8, 20))
    assert len(items) == 1
    item = items[0]
    assert item.open_volume == pytest.approx(454.0)
    assert item.open_amount == pytest.approx(505000.0)
    assert item.turnover_rate == pytest.approx(1.2)
    assert item.volume_ratio == pytest.approx(3.0)


def test_stk_auction_price_is_vwap_not_open(monkeypatch):
    provider, _ = _provider_with(monkeypatch, {"stk_auction": [_auction_row()], "stk_auction_o": []})
    item = provider.get_auction_finals(None, date(2026, 8, 20))[0]
    assert item.vwap == pytest.approx(10.22)
    assert item.open_price is None
    assert "tushare_stk_auction_vwap" in item.quality_flags


def test_stk_auction_o_close_is_open_price_decimal_change(monkeypatch):
    provider, _ = _provider_with(
        monkeypatch,
        {"stk_auction": [], "stk_auction_o": [_open_row()]},
    )
    item = provider.get_auction_finals(None, date(2026, 8, 20))[0]
    assert item.open_price == pytest.approx(10.25)
    assert item.vwap == pytest.approx(10.22)
    assert item.open_change_pct == pytest.approx(0.025)


def test_merge_prefers_open_match_over_vwap(monkeypatch):
    provider, _ = _provider_with(
        monkeypatch,
        {"stk_auction": [_auction_row()], "stk_auction_o": [_open_row()]},
    )
    item = provider.get_auction_finals(None, date(2026, 8, 20))[0]
    assert item.open_price == pytest.approx(10.25)
    assert item.vwap == pytest.approx(10.22)
    assert "vwap_open_price_distinct" in item.quality_flags
    assert item.open_change_pct == pytest.approx(0.025)


def test_missing_ts_code_dropped(monkeypatch):
    provider, _ = _provider_with(
        monkeypatch,
        {
            "stk_auction": [_auction_row(), {"vol": 1, "price": 1}],
            "stk_auction_o": [],
        },
    )
    items = provider.get_auction_finals(None, date(2026, 8, 20))
    assert [i.symbol for i in items] == ["000001.SZ"]


def test_symbol_filter(monkeypatch):
    provider, _ = _provider_with(
        monkeypatch,
        {
            "stk_auction": [_auction_row(), _auction_row(ts_code="600519.SH")],
            "stk_auction_o": [],
        },
    )
    items = provider.get_auction_finals(["600519.SH"], date(2026, 8, 20))
    assert [i.symbol for i in items] == ["600519.SH"]


def test_empty_or_error_returns_empty(monkeypatch):
    provider, _ = _provider_with(monkeypatch, {"stk_auction": [], "stk_auction_o": []})
    assert provider.get_auction_finals(None, date(2026, 8, 20)) == []
    provider, _ = _provider_with(
        monkeypatch,
        {"stk_auction": [_auction_row()], "stk_auction_o": []},
        error=RuntimeError("rate limited"),
    )
    assert provider.get_auction_finals(None, date(2026, 8, 20)) == []


def test_get_auction_series_is_empty():
    assert TushareProvider().get_auction_series(["000001.SZ"], date(2026, 8, 20)) == []


def test_test_dataset_undeclared_does_not_raise():
    result = TushareProvider().test_dataset("daily")
    assert result["rows"] == 0
    assert "未接入" in result["error"]


def test_test_dataset_auction_preview(monkeypatch):
    provider, _ = _provider_with(
        monkeypatch,
        {"stk_auction": [], "stk_auction_o": [_open_row()]},
    )
    monkeypatch.setattr("app.plugins.tushare.provider.cn_today", lambda: date(2026, 8, 20))
    result = provider.test_dataset("auction")
    assert result["rows"] == 1
    assert result["preview"][0]["open_price"] == pytest.approx(10.25)


def test_probe_api_key_ok(monkeypatch):
    fake = _FakePro({"stock_basic": [{"ts_code": "000001.SZ"}]})

    class _Ts:
        @staticmethod
        def pro_api(key):
            assert key == "candidate"
            return fake

    monkeypatch.setattr(tp, "_import_tushare", lambda: _Ts)
    ok, reason = tp.probe_api_key("candidate")
    assert ok is True and reason == "ok"


def test_probe_api_key_invalid(monkeypatch):
    class _Ts:
        @staticmethod
        def pro_api(key):
            raise RuntimeError("invalid token")

    monkeypatch.setattr(tp, "_import_tushare", lambda: _Ts)
    ok, reason = tp.probe_api_key("bad")
    assert ok is False and "无效" in reason


def test_loader_probe_plugin_key_dispatch(monkeypatch):
    from app.data_providers.custom import loader

    monkeypatch.setattr(
        tp,
        "probe_api_key",
        lambda key: (True, "ok") if key == "good" else (False, "bad"),
    )
    assert loader.probe_plugin_key("tushare", "good") == (True, "ok")
    assert loader.probe_plugin_key("tushare", "bad") == (False, "bad")
