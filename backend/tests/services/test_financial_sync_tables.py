from datetime import date

import polars as pl

from app.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.services import financial_sync


def test_financial_sync_includes_quick_and_forecast_sources() -> None:
    assert "quick" in financial_sync.FINANCIAL_TABLES
    assert "forecast" in financial_sync.FINANCIAL_TABLES
    assert financial_sync._PROVIDER_TABLE_MAP["quick"] == "quick"
    assert financial_sync._PROVIDER_TABLE_MAP["forecast"] == "forecast"


def test_recent_report_dates_prefers_latest_closed_quarter() -> None:
    assert financial_sync._recent_report_dates(date(2026, 7, 3))[0] == "2026-06-30"


def test_sync_forecast_falls_back_to_eastmoney(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(financial_sync, "_sync_table", lambda *args, **kwargs: 0)
    monkeypatch.setattr(financial_sync, "_forecast_fstore_has_rows", lambda: False)
    monkeypatch.setattr(
        financial_sync,
        "_recent_report_dates",
        lambda today=None: ["2026-06-30", "2026-03-31"],
    )

    def fake_get_datacenter_paged(url, params, max_pages=20):
        assert url == financial_sync._EASTMONEY_DATACENTER
        assert params["reportName"] == "RPT_PUBLIC_OP_NEWPREDICT"
        report_date = "2026-06-30" if "REPORT_DATE='2026-06-30'" in params["filter"] else "2026-03-31"
        return [{
            "SECUCODE": "603822.SH",
            "SECURITY_CODE": "603822",
            "NOTICE_DATE": "2026-07-04 00:00:00",
            "REPORT_DATE": f"{report_date} 00:00:00",
            "PREDICT_TYPE": "扭亏",
            "PREDICT_CONTENT": "预计盈利",
            "CHANGE_REASON_EXPLAIN": "主营改善",
            "FORECAST_JZ": 75000000,
            "PREDICT_AMT_LOWER": 60000000,
            "PREDICT_AMT_UPPER": 90000000,
        }]

    from app.services import eastmoney_client

    monkeypatch.setattr(eastmoney_client, "get_datacenter_paged", fake_get_datacenter_paged)

    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    assert financial_sync.sync_forecast(tmp_path, capset) == 2

    df = pl.read_parquet(tmp_path / "financials" / "forecast" / "part.parquet")
    assert set(df["t_date"].to_list()) == {"2026-06-30", "2026-03-31"}
    row = df.filter(pl.col("t_date") == "2026-06-30").to_dicts()[0]
    assert row["symbol"] == "603822.SH"
    assert row["t_date"] == "2026-06-30"
    assert row["notice_date"] == "2026-07-04"
    assert row["source"] == "eastmoney:forecast"


def test_sync_quick_merges_fstore_and_eastmoney(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        financial_sync,
        "_recent_report_dates",
        lambda today=None: ["2026-06-30", "2026-03-31"],
    )

    def fake_sync_table(table, symbols, data_dir, capset, latest_only=True):
        assert table == "quick"
        out_dir = data_dir / "financials" / "quick"
        out_dir.mkdir(parents=True, exist_ok=True)
        pl.DataFrame([{
            "symbol": "688806.SH",
            "t_date": "2026-03-31",
            "source": "fstore",
            "net_profit": 100,
        }]).write_parquet(out_dir / "part.parquet")
        return 1

    def fake_get_datacenter_paged(url, params, max_pages=20):
        assert url == financial_sync._EASTMONEY_DATACENTER
        assert params["reportName"] == "RPT_FCI_PERFORMANCEE"
        if "REPORT_DATE='2026-06-30'" in params["filter"]:
            return [{
                "SECURITY_CODE": "603822",
                "TRADE_MARKET_CODE": "069001001001",
                "UPDATE_DATE": "2026-07-04 00:00:00",
                "REPORT_DATE": "2026-06-30 00:00:00",
                "BASIC_EPS": 0.2,
                "TOTAL_OPERATE_INCOME": 2000,
                "PARENT_NETPROFIT": 300,
                "PARENT_BVPS": 4.5,
                "WEIGHTAVG_ROE": 6.7,
                "YSTZ": 8.9,
                "JLRTBZCL": 10.1,
                "DJDYSHZ": 12.3,
                "DJDJLHZ": 14.5,
            }]
        return [{
            "SECURITY_CODE": "688806",
            "TRADE_MARKET_CODE": "069001001006",
            "UPDATE_DATE": "2026-05-22 00:00:00",
            "REPORT_DATE": "2026-03-31 00:00:00",
            "PARENT_NETPROFIT": 999,
        }]

    from app.services import eastmoney_client

    monkeypatch.setattr(financial_sync, "_sync_table", fake_sync_table)
    monkeypatch.setattr(eastmoney_client, "get_datacenter_paged", fake_get_datacenter_paged)

    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    assert financial_sync.sync_quick(tmp_path, capset) == 2

    df = pl.read_parquet(tmp_path / "financials" / "quick" / "part.parquet")
    assert set(df["symbol"].to_list()) == {"688806.SH", "603822.SH"}
    assert set(df["t_date"].to_list()) == {"2026-03-31", "2026-06-30"}
    fstore_row = df.filter(pl.col("symbol") == "688806.SH").to_dicts()[0]
    eastmoney_row = df.filter(pl.col("symbol") == "603822.SH").to_dicts()[0]
    assert fstore_row["source"] == "fstore"
    assert eastmoney_row["source"] == "eastmoney:quick"
    assert eastmoney_row["notice_date"] == "2026-07-04"
    assert eastmoney_row["net_profit"] == 300


def test_sync_quick_keeps_fstore_when_eastmoney_fails(tmp_path, monkeypatch) -> None:
    def fake_sync_table(table, symbols, data_dir, capset, latest_only=True):
        out_dir = data_dir / "financials" / "quick"
        out_dir.mkdir(parents=True, exist_ok=True)
        pl.DataFrame([{
            "symbol": "688806.SH",
            "t_date": "2026-03-31",
            "source": "fstore",
        }]).write_parquet(out_dir / "part.parquet")
        return 1

    monkeypatch.setattr(financial_sync, "_sync_table", fake_sync_table)
    monkeypatch.setattr(
        financial_sync,
        "_sync_quick_from_eastmoney",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("eastmoney down")),
    )

    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    assert financial_sync.sync_quick(tmp_path, capset) == 1
    df = pl.read_parquet(tmp_path / "financials" / "quick" / "part.parquet")
    assert df.to_dicts() == [{"symbol": "688806.SH", "t_date": "2026-03-31", "source": "fstore"}]


def test_sync_all_uses_table_specific_sync_functions(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    def make_sync(table: str):
        def _sync(data_dir, capset):
            calls.append(table)
            return len(calls)
        return _sync

    monkeypatch.setattr(
        financial_sync,
        "_financial_sync_functions",
        lambda: {table: make_sync(table) for table in financial_sync.FINANCIAL_TABLES},
    )

    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    result = financial_sync.sync_all(tmp_path, capset)
    assert calls == list(financial_sync.FINANCIAL_TABLES)
    assert result["quick"] == 5
    assert result["forecast"] == 6
