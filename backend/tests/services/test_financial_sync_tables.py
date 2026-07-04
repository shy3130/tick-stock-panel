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
