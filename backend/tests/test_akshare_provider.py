from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest


def test_akshare_daily_normalizes_documented_eastmoney_columns():
    try:
        from app.plugins.akshare.provider import AkShareProvider
    except ImportError:
        pytest.fail("AkShareProvider is not implemented")

    class AkClient:
        def __init__(self):
            self.calls: list[dict] = []

        def stock_zh_a_hist(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame(
                [
                    {
                        "日期": "2026-07-24",
                        "股票代码": "600000",
                        "开盘": 10.0,
                        "收盘": 10.2,
                        "最高": 10.5,
                        "最低": 9.9,
                        "成交量": 1_000.0,
                        "成交额": 1_020_000.0,
                        "振幅": 6.0,
                        "涨跌幅": 2.5,
                        "涨跌额": 0.25,
                        "换手率": 0.8,
                    }
                ]
            )

    client = AkClient()
    provider = AkShareProvider(client=client)

    result = provider.get_daily(
        ["600000.SH"],
        datetime(2026, 7, 24),
        datetime(2026, 7, 24, 23, 59),
    )

    assert client.calls == [
        {
            "symbol": "600000",
            "period": "daily",
            "start_date": "20260724",
            "end_date": "20260724",
            "adjust": "",
        }
    ]
    assert result.columns == [
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "quote_ts",
    ]
    assert result["symbol"].to_list() == ["600000.SH"]
    assert result["date"].to_list() == [date(2026, 7, 24)]
    assert result["amount"].to_list() == [1_020_000.0]


def test_akshare_hfq_factor_is_converted_to_event_multipliers():
    from app.plugins.akshare.provider import AkShareProvider

    class AkClient:
        def __init__(self):
            self.calls: list[dict] = []

        def stock_zh_a_daily(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame(
                [
                    {"date": "2024-01-02", "hfq_factor": 1.0},
                    {"date": "2024-01-03", "hfq_factor": 1.0},
                    {"date": "2024-06-10", "hfq_factor": 1.1},
                ]
            )

    client = AkClient()
    result = AkShareProvider(client=client).get_adj_factors(
        ["600000.SH"],
        datetime(2024, 1, 1),
        datetime(2024, 12, 31),
    )

    assert client.calls == [
        {
            "symbol": "sh600000",
            "adjust": "hfq-factor",
        }
    ]
    assert result["trade_date"].to_list() == [
        date(2024, 1, 2),
        date(2024, 6, 10),
    ]
    assert result["ex_factor"].to_list() == pytest.approx([1.0, 1.1])


def test_akshare_instruments_are_explicitly_marked_current_only():
    from app.plugins.akshare.provider import AkShareProvider

    class AkClient:
        def stock_zh_a_spot_em(self):
            return pd.DataFrame(
                [
                    {"代码": "600000", "名称": "浦发银行"},
                    {"代码": "000001", "名称": "平安银行"},
                    {"代码": "430047", "名称": "北交样本"},
                ]
            )

    result = AkShareProvider(client=AkClient()).get_instruments("stock")

    assert [row["symbol"] for row in result] == [
        "000001.SZ",
        "430047.BJ",
        "600000.SH",
    ]
    assert all(row["ext"]["list_status"] == "L" for row in result)
    assert all(row["ext"]["universe_history"] == "current_only" for row in result)


def test_akshare_current_only_warning_survives_instrument_storage():
    from app.plugins.akshare.provider import AkShareProvider
    from app.services.instrument_sync import _flatten_instruments

    class AkClient:
        def stock_zh_a_spot_em(self):
            return pd.DataFrame([{"代码": "600000", "名称": "浦发银行"}])

    [row] = _flatten_instruments(
        AkShareProvider(client=AkClient()).get_instruments("stock"),
    )

    assert row["universe_history"] == "current_only"


def test_akshare_instruments_skip_rows_without_a_real_code():
    from app.plugins.akshare.provider import AkShareProvider

    class AkClient:
        def stock_zh_a_spot_em(self):
            return pd.DataFrame(
                [
                    {"代码": None, "名称": "缺失代码"},
                    {"代码": "", "名称": "空代码"},
                    {"代码": "1", "名称": "有效代码"},
                ]
            )

    result = AkShareProvider(client=AkClient()).get_instruments("stock")

    assert [row["symbol"] for row in result] == ["000001.SZ"]


def test_akshare_plugin_reports_missing_dependency(monkeypatch):
    try:
        from app.plugins.akshare.check import availability
    except ImportError:
        pytest.fail("AKShare plugin availability check is not implemented")

    monkeypatch.setattr(
        "app.plugins.akshare.check.importlib.util.find_spec",
        lambda package: None,
    )

    available, reason = availability()

    assert available is False
    assert reason == "未安装 akshare Python 包"


def test_akshare_plugin_manifest_labels_it_as_explicit_fallback():
    from app.data_providers.custom.loader import plugin_manifest

    manifest = plugin_manifest("akshare")

    assert manifest is not None
    assert manifest["entry"] == "app.plugins.akshare.provider:AkShareProvider"
    assert manifest["datasets"] == ["instruments", "daily", "adj_factor"]
    assert "显式备用" in manifest["display_name"]
