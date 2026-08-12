from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest


def test_tushare_daily_normalizes_official_units_and_columns():
    try:
        from app.plugins.tushare.provider import TushareProvider
    except ImportError:
        pytest.fail("TushareProvider is not implemented")

    class ProClient:
        def __init__(self):
            self.calls: list[dict] = []

        def daily(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame(
                [
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20260724",
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.9,
                        "close": 10.2,
                        "pre_close": 9.95,
                        "change": 0.25,
                        "pct_chg": 2.5126,
                        "vol": 1_000.0,
                        "amount": 1_020.0,
                    }
                ]
            )

    client = ProClient()
    provider = TushareProvider(client=client)

    result = provider.get_daily(
        ["600000.SH"],
        datetime(2026, 7, 24),
        datetime(2026, 7, 24, 23, 59),
    )

    assert client.calls == [
        {
            "ts_code": "600000.SH",
            "start_date": "20260724",
            "end_date": "20260724",
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
    assert result["date"].to_list() == [date(2026, 7, 24)]
    assert result["volume"].to_list() == [1_000.0]
    assert result["amount"].to_list() == [1_020_000.0]


def test_tushare_instruments_include_delisted_history_for_point_in_time_universe():
    from app.plugins.tushare.provider import TushareProvider

    class ProClient:
        def __init__(self):
            self.statuses: list[str] = []

        def stock_basic(self, **kwargs):
            status = kwargs["list_status"]
            self.statuses.append(status)
            rows = {
                "L": [
                    {
                        "ts_code": "600000.SH",
                        "symbol": "600000",
                        "name": "浦发银行",
                        "exchange": "SSE",
                        "market": "主板",
                        "list_status": "L",
                        "list_date": "19991110",
                        "delist_date": None,
                    }
                ],
                "D": [
                    {
                        "ts_code": "600001.SH",
                        "symbol": "600001",
                        "name": "退市样本",
                        "exchange": "SSE",
                        "market": "主板",
                        "list_status": "D",
                        "list_date": "19900101",
                        "delist_date": "20200101",
                    }
                ],
                "P": [],
                "G": [],
            }[status]
            return pd.DataFrame(rows)

    client = ProClient()
    result = TushareProvider(client=client).get_instruments("stock")

    assert client.statuses == ["L", "D", "P", "G"]
    assert [row["symbol"] for row in result] == ["600000.SH", "600001.SH"]
    assert result[0]["exchange"] == "SH"
    assert result[0]["ext"] == {
        "listing_date": "19991110",
        "delist_date": None,
        "list_status": "L",
        "market": "主板",
    }
    assert result[1]["ext"]["delist_date"] == "20200101"


def test_instrument_storage_preserves_listing_and_delisting_boundaries():
    from app.services.instrument_sync import _flatten_instruments

    [row] = _flatten_instruments(
        [
            {
                "symbol": "600001.SH",
                "name": "退市样本",
                "code": "600001",
                "exchange": "SH",
                "region": "CN",
                "type": "stock",
                "ext": {
                    "listing_date": "19900101",
                    "delist_date": "20200101",
                    "list_status": "D",
                    "market": "主板",
                },
            }
        ]
    )

    assert row["listing_date"] == "19900101"
    assert row["delist_date"] == "20200101"
    assert row["list_status"] == "D"
    assert row["market"] == "主板"


def test_tushare_adj_factor_converts_cumulative_series_to_event_multipliers():
    from app.plugins.tushare.provider import TushareProvider

    class ProClient:
        def __init__(self):
            self.calls: list[dict] = []

        def adj_factor(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame(
                [
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20240102",
                        "adj_factor": 1.0,
                    },
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20240103",
                        "adj_factor": 1.0,
                    },
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20240610",
                        "adj_factor": 1.1,
                    },
                ]
            )

    client = ProClient()
    result = TushareProvider(client=client).get_adj_factors(
        ["600000.SH"],
        datetime(2024, 1, 1),
        datetime(2024, 12, 31),
    )

    assert client.calls == [
        {
            "ts_code": "600000.SH",
            "start_date": "20240101",
            "end_date": "20241231",
        }
    ]
    assert result["trade_date"].to_list() == [
        date(2024, 1, 2),
        date(2024, 6, 10),
    ]
    assert result["ex_factor"].to_list() == pytest.approx([1.0, 1.1])


def test_tushare_large_universe_fetches_daily_by_open_trade_date():
    from app.plugins.tushare.provider import TushareProvider

    requested = [f"{code:06d}.SZ" for code in range(1, 102)]

    class ProClient:
        def __init__(self):
            self.calendar_calls: list[dict] = []
            self.daily_calls: list[dict] = []

        def trade_cal(self, **kwargs):
            self.calendar_calls.append(kwargs)
            return pd.DataFrame(
                [
                    {"cal_date": "20260723", "is_open": 1},
                    {"cal_date": "20260724", "is_open": 1},
                ]
            )

        def daily(self, **kwargs):
            self.daily_calls.append(kwargs)
            return pd.DataFrame(
                [
                    {
                        "ts_code": requested[0],
                        "trade_date": kwargs["trade_date"],
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.9,
                        "close": 10.2,
                        "vol": 1_000.0,
                        "amount": 1_020.0,
                    },
                    {
                        "ts_code": "999999.SZ",
                        "trade_date": kwargs["trade_date"],
                        "open": 1.0,
                        "high": 1.0,
                        "low": 1.0,
                        "close": 1.0,
                        "vol": 1.0,
                        "amount": 1.0,
                    },
                ]
            )

    client = ProClient()
    progress: list[tuple[int, int]] = []
    result = TushareProvider(client=client).get_daily(
        requested,
        datetime(2026, 7, 23),
        datetime(2026, 7, 24),
        on_chunk_done=lambda current, total: progress.append((current, total)),
    )

    assert client.calendar_calls == [
        {
            "exchange": "",
            "start_date": "20260723",
            "end_date": "20260724",
            "is_open": "1",
            "fields": "cal_date,is_open",
        }
    ]
    assert client.daily_calls == [
        {"trade_date": "20260723"},
        {"trade_date": "20260724"},
    ]
    assert progress == [(1, 2), (2, 2)]
    assert result.height == 2
    assert result["symbol"].unique().to_list() == [requested[0]]


def test_tushare_large_universe_fetches_factors_by_open_trade_date():
    from app.plugins.tushare.provider import TushareProvider

    requested = [f"{code:06d}.SZ" for code in range(1, 102)]

    class ProClient:
        def trade_cal(self, **kwargs):
            return pd.DataFrame(
                [
                    {"cal_date": "20240102", "is_open": 1},
                    {"cal_date": "20240610", "is_open": 1},
                ]
            )

        def adj_factor(self, **kwargs):
            factor = 1.0 if kwargs["trade_date"] == "20240102" else 1.1
            return pd.DataFrame(
                [
                    {
                        "ts_code": requested[0],
                        "trade_date": kwargs["trade_date"],
                        "adj_factor": factor,
                    },
                    {
                        "ts_code": "999999.SZ",
                        "trade_date": kwargs["trade_date"],
                        "adj_factor": factor,
                    },
                ]
            )

    result = TushareProvider(client=ProClient()).get_adj_factors(
        requested,
        datetime(2024, 1, 1),
        datetime(2024, 12, 31),
    )

    assert result["symbol"].unique().to_list() == [requested[0]]
    assert result["trade_date"].to_list() == [
        date(2024, 1, 2),
        date(2024, 6, 10),
    ]
    assert result["ex_factor"].to_list() == pytest.approx([1.0, 1.1])


def test_tushare_financial_metrics_use_vip_cross_section_and_actual_announce_date():
    from app.plugins.tushare.provider import TushareProvider

    requested = [f"{code:06d}.SZ" for code in range(1, 102)]

    class ProClient:
        def __init__(self):
            self.calls: list[dict] = []

        def fina_indicator_vip(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["period"] == "20260630":
                return pd.DataFrame(
                    [
                        {
                            "ts_code": requested[0],
                            "ann_date": "20260720",
                            "end_date": "20260630",
                            "eps": 0.62,
                            "dt_eps": 0.61,
                            "bps": 12.3,
                            "ocfps": 0.9,
                            "roe": 8.2,
                            "roe_waa": 8.1,
                            "roa": 1.2,
                            "grossprofit_margin": 31.0,
                            "netprofit_margin": 12.0,
                            "debt_to_assets": 55.0,
                            "q_sales_yoy": 7.5,
                            "q_netprofit_yoy": 9.5,
                            "q_ocf_to_sales": 10.5,
                            "inv_turn": 2.1,
                        },
                        {
                            "ts_code": "999999.SZ",
                            "ann_date": "20260720",
                            "end_date": "20260630",
                            "eps": 99.0,
                        },
                    ]
                )
            return pd.DataFrame()

    client = ProClient()
    provider = TushareProvider(
        client=client,
        today_fn=lambda: date(2026, 7, 28),
    )
    result = provider.get_financials("metrics", requested, latest_only=True)

    assert [call["period"] for call in client.calls] == [
        "20260630",
        "20260331",
        "20251231",
        "20250930",
        "20250630",
    ]
    assert result.height == 1
    assert result["symbol"].to_list() == [requested[0]]
    assert result["period_end"].to_list() == [date(2026, 6, 30)]
    assert result["announce_date"].to_list() == [date(2026, 7, 20)]
    assert result.select(
        "eps_basic",
        "eps_diluted",
        "bps",
        "ocfps",
        "roe",
        "roe_diluted",
        "roa",
        "gross_margin",
        "net_margin",
        "debt_to_asset_ratio",
        "revenue_yoy",
        "net_income_yoy",
        "operating_cash_to_revenue",
        "inventory_turnover",
    ).row(0) == pytest.approx(
        (0.62, 0.61, 12.3, 0.9, 8.2, 8.1, 1.2, 31.0, 12.0, 55.0, 7.5, 9.5, 10.5, 2.1)
    )


def test_tushare_income_maps_standard_endpoint_without_losing_point_in_time_fields():
    from app.plugins.tushare.provider import TushareProvider

    class ProClient:
        def __init__(self):
            self.calls: list[dict] = []

        def income(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame(
                [
                    {
                        "ts_code": "600000.SH",
                        "ann_date": "20260420",
                        "f_ann_date": "20260422",
                        "end_date": "20260331",
                        "revenue": 100.0,
                        "oper_cost": 60.0,
                        "operate_profit": 20.0,
                        "total_profit": 18.0,
                        "n_income": 15.0,
                        "n_income_attr_p": 14.0,
                        "basic_eps": 0.3,
                        "diluted_eps": 0.29,
                    }
                ]
            )

    client = ProClient()
    result = TushareProvider(client=client).get_financials(
        "income",
        ["600000.SH"],
        latest_only=True,
    )

    assert len(client.calls) == 1
    assert client.calls[0]["ts_code"] == "600000.SH"
    assert result.select(
        "symbol",
        "period_end",
        "announce_date",
        "revenue",
        "operating_cost",
        "operating_profit",
        "total_profit",
        "net_income",
        "net_income_attributable",
        "basic_eps",
        "diluted_eps",
    ).row(0) == (
        "600000.SH",
        date(2026, 3, 31),
        date(2026, 4, 22),
        100.0,
        60.0,
        20.0,
        18.0,
        15.0,
        14.0,
        0.3,
        0.29,
    )


def test_tushare_share_history_converts_units_and_keeps_only_change_events():
    from app.plugins.tushare.provider import TushareProvider

    class ProClient:
        def __init__(self):
            self.calls: list[dict] = []

        def daily_basic(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame(
                [
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20250729",
                        "total_share": 10_000.0,
                        "float_share": 8_000.0,
                    },
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20260102",
                        "total_share": 10_000.0,
                        "float_share": 8_000.0,
                    },
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20260724",
                        "total_share": 12_000.0,
                        "float_share": 9_000.0,
                    },
                ]
            )

    client = ProClient()
    result = TushareProvider(
        client=client,
        today_fn=lambda: date(2026, 7, 28),
        share_history_years=1,
    ).get_financials("shares", ["600000.SH"], latest_only=False)

    assert client.calls[0]["ts_code"] == "600000.SH"
    assert client.calls[0]["start_date"] == "20250728"
    assert client.calls[0]["end_date"] == "20260728"
    assert result.select(
        "symbol",
        "period_end",
        "announce_date",
        "total_shares",
        "float_shares",
    ).rows() == [
        (
            "600000.SH",
            date(2025, 7, 29),
            date(2025, 7, 29),
            100_000_000.0,
            80_000_000.0,
        ),
        (
            "600000.SH",
            date(2026, 7, 24),
            date(2026, 7, 24),
            120_000_000.0,
            90_000_000.0,
        ),
    ]


def test_tushare_instruments_include_latest_share_capital_for_market_cap_filters():
    from app.plugins.tushare.provider import TushareProvider

    class ProClient:
        def stock_basic(self, **kwargs):
            if kwargs["list_status"] != "L":
                return pd.DataFrame()
            return pd.DataFrame(
                [
                    {
                        "ts_code": "600000.SH",
                        "symbol": "600000",
                        "name": "浦发银行",
                        "exchange": "SSE",
                        "market": "主板",
                        "list_status": "L",
                        "list_date": "19991110",
                        "delist_date": None,
                    }
                ]
            )

        def daily_basic(self, **kwargs):
            return pd.DataFrame(
                [
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20260724",
                        "total_share": 2_935_208.0397,
                        "float_share": 1_940_591.8198,
                    }
                ]
            )

    [instrument] = TushareProvider(
        client=ProClient(),
        today_fn=lambda: date(2026, 7, 28),
    ).get_instruments("stock")

    assert instrument["ext"]["total_shares"] == pytest.approx(29_352_080_397.0)
    assert instrument["ext"]["float_shares"] == pytest.approx(19_405_918_198.0)


def test_tushare_plugin_stays_unavailable_until_token_is_configured(
    monkeypatch,
):
    try:
        from app.plugins.tushare.check import availability
    except ImportError:
        pytest.fail("Tushare plugin availability check is not implemented")

    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setattr(
        "app.plugins.tushare.check.importlib.util.find_spec",
        lambda package: object(),
    )

    available, reason = availability()

    assert available is False
    assert reason == "TUSHARE_TOKEN 未配置"


def test_tushare_plugin_manifest_exposes_audited_datasets():
    from app.data_providers.custom.loader import plugin_manifest

    manifest = plugin_manifest("tushare")

    assert manifest is not None
    assert manifest["entry"] == "app.plugins.tushare.provider:TushareProvider"
    assert manifest["check"] == "app.plugins.tushare.check:availability"
    assert manifest["datasets"] == ["instruments", "daily", "adj_factor", "financial"]
