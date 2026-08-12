from datetime import date

import polars as pl

from app.api.data import (
    _safe_aggregate_adj_factor,
    _safe_aggregate_daily,
    _safe_aggregate_enriched,
    _safe_aggregate_etf_daily,
    _safe_aggregate_etf_enriched,
    _safe_aggregate_hk_daily,
    _safe_aggregate_index_daily,
    _safe_aggregate_minute,
)


class FakeStore:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class FakeRepo:
    def __init__(self, data_dir):
        self.store = FakeStore(data_dir)

    def execute_one(self, sql):
        if "count(DISTINCT symbol)" in sql:
            return (2,)
        return None


def test_daily_status_uses_enriched_when_local_raw_mirror_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    for ds in ("2026-06-30", "2026-07-01"):
        out = tmp_path / "kline_daily_enriched" / f"date={ds}" / "part.parquet"
        out.parent.mkdir(parents=True)
        pl.DataFrame({"symbol": ["600519.SH"], "date": [ds]}).write_parquet(out)

    stats = _safe_aggregate_daily(FakeRepo(tmp_path))
    assert stats["rows"] == 0
    assert stats["row_count_exact"] is False
    assert stats["earliest_date"] == "2026-06-30"
    assert stats["latest_date"] == "2026-07-01"
    assert stats["trading_days"] == 2
    assert stats["raw_mirror_disabled"] is True


def test_local_daily_status_ignores_existing_raw_mirror(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    (tmp_path / "kline_daily" / "date=2026-08-09").mkdir(parents=True)
    (tmp_path / "kline_daily_enriched" / "date=2026-08-10").mkdir(parents=True)

    stats = _safe_aggregate_daily(FakeRepo(tmp_path))

    assert stats["latest_date"] == "2026-08-10"
    assert stats["source"] == "fquant_local_enriched"


def test_enriched_status_honors_provider_confirmed_read_ceiling(tmp_path):
    for ds in ("2026-08-10", "2026-08-11"):
        (tmp_path / "kline_daily_enriched" / f"date={ds}").mkdir(parents=True)
    repo = FakeRepo(tmp_path)
    repo.enriched_read_ceiling = date(2026, 8, 10)

    stats = _safe_aggregate_enriched(repo)

    assert stats["latest_date"] == "2026-08-10"
    assert stats["trading_days"] == 1


def test_adj_factor_status_reads_single_all_parquet(tmp_path):
    out = tmp_path / "adj_factor" / "all.parquet"
    out.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["000001.SZ", "000001.SZ", "600519.SH"],
            "trade_date": [
                date(2026, 6, 30),
                date(2026, 7, 1),
                date(2026, 7, 1),
            ],
            "factor": [1.0, 1.1, 1.0],
        }
    ).write_parquet(out)

    assert _safe_aggregate_adj_factor(FakeRepo(tmp_path)) == {
        "rows": 3,
        "row_count_exact": True,
        "earliest_date": "2026-06-30",
        "latest_date": "2026-07-01",
        "symbols_covered": 2,
        "trading_days": 2,
    }



def test_minute_status_falls_back_to_provider_catalog(tmp_path, monkeypatch):
    class Provider:
        @staticmethod
        def get_minute_coverage():
            return {
                "latest_date": "2026-08-10",
                "stage": "final",
                "generation": "20260810T120615",
                "logical": "tdx_minutes_from_2023",
            }

    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )
    monkeypatch.setattr(
        "app.data_providers.registry.get_provider", lambda name: Provider(),
    )

    stats = _safe_aggregate_minute(FakeRepo(tmp_path))

    assert stats == {
        "rows": 0,
        "row_count_exact": False,
        "earliest_date": None,
        "latest_date": "2026-08-10",
        "symbols_covered": 0,
        "trading_days": 0,
        "available": True,
        "source": "catalog_tdx_minutes",
        "stage": "final",
        "generation": "20260810T120615",
        "logical": "tdx_minutes_from_2023",
    }


def test_minute_status_prefers_local_cache(tmp_path, monkeypatch):
    (tmp_path / "kline_minute" / "date=2026-08-08").mkdir(parents=True)
    monkeypatch.setattr(
        "app.data_providers.registry.get_provider",
        lambda name: (_ for _ in ()).throw(AssertionError("provider must not run")),
    )

    stats = _safe_aggregate_minute(FakeRepo(tmp_path))

    assert stats["latest_date"] == "2026-08-08"
    assert stats["source"] == "local_cache"
    assert stats["available"] is True


def test_partition_stats_filter_invalid_dates_without_scanning_kline(tmp_path):
    cases = (
        ("kline_index_daily", _safe_aggregate_index_daily),
        ("kline_etf_daily", _safe_aggregate_etf_daily),
        ("kline_hk_daily", _safe_aggregate_hk_daily),
    )
    for directory, fetch in cases:
        for ds in ("2026-06-30", "2026-07-01", "2026-02-31", "not-a-date"):
            (tmp_path / directory / f"date={ds}").mkdir(parents=True)
        stats = fetch(FakeRepo(tmp_path))
        assert stats == {
            "rows": 0,
            "row_count_exact": False,
            "earliest_date": "2026-06-30",
            "latest_date": "2026-07-01",
            "symbols_covered": 2,
            "trading_days": 2,
        }



# ----------------------------------------------------------------------
# ETF 日K / enriched 兼容回退：新独立目录优先，空时只读回退旧 index 存储。
# 历史契约见 repository.get_etf_daily / _refresh_etf_instruments：
#   旧版 ETF 曾与指数混存于 kline_index_daily / kline_index_enriched。
# ----------------------------------------------------------------------


def _mkpart(root, directory, dates):
    """在 directory 下创建严格 ISO 日期分区目录（不含 parquet）。"""
    for ds in dates:
        (root / directory / f"date={ds}").mkdir(parents=True, exist_ok=True)


def test_etf_daily_new_storage_takes_priority_over_legacy(tmp_path):
    """新独立 kline_etf_daily 有数据时永远优先，不读旧 kline_index_daily。"""
    _mkpart(tmp_path, "kline_etf_daily", ("2026-07-01",))
    _mkpart(tmp_path, "kline_index_daily", ("2026-06-30",))

    stats = _safe_aggregate_etf_daily(FakeRepo(tmp_path))

    assert stats["latest_date"] == "2026-07-01"
    assert stats["trading_days"] == 1


def test_etf_daily_falls_back_to_legacy_index_daily_when_new_absent(tmp_path):
    """新目录不存在时，只读回退旧 kline_index_daily，老用户不误报无数据。"""
    _mkpart(tmp_path, "kline_index_daily", ("2026-06-29", "2026-06-30"))

    stats = _safe_aggregate_etf_daily(FakeRepo(tmp_path))

    assert stats is not None
    assert stats["earliest_date"] == "2026-06-29"
    assert stats["latest_date"] == "2026-06-30"
    assert stats["trading_days"] == 2


def test_etf_daily_returns_none_when_both_new_and_legacy_absent(tmp_path):
    """新旧存储都没有时返回 None。"""
    assert _safe_aggregate_etf_daily(FakeRepo(tmp_path)) is None


def test_etf_daily_legacy_fallback_excludes_future_partitions_via_ceiling(tmp_path):
    """兼容回退遵守 provider-confirmed read ceiling，不计入未来/未确认分区。"""
    _mkpart(tmp_path, "kline_index_daily", ("2026-08-10", "2026-08-11"))
    repo = FakeRepo(tmp_path)
    repo.enriched_read_ceiling = date(2026, 8, 10)

    stats = _safe_aggregate_etf_daily(repo)

    assert stats["latest_date"] == "2026-08-10"
    assert stats["trading_days"] == 1


def test_etf_enriched_new_storage_takes_priority_over_legacy(tmp_path):
    """新独立 kline_etf_enriched 有数据时永远优先，不读旧 kline_index_enriched。"""
    _mkpart(tmp_path, "kline_etf_enriched", ("2026-07-01",))
    _mkpart(tmp_path, "kline_index_enriched", ("2026-06-30",))

    stats = _safe_aggregate_etf_enriched(FakeRepo(tmp_path))

    assert stats["latest_date"] == "2026-07-01"
    assert stats["trading_days"] == 1


def test_etf_enriched_falls_back_to_legacy_index_enriched_when_new_absent(tmp_path):
    """新目录不存在时，只读回退旧 kline_index_enriched，老用户不误报无数据。"""
    _mkpart(tmp_path, "kline_index_enriched", ("2026-06-29", "2026-06-30"))

    stats = _safe_aggregate_etf_enriched(FakeRepo(tmp_path))

    assert stats is not None
    assert stats["earliest_date"] == "2026-06-29"
    assert stats["latest_date"] == "2026-06-30"
    assert stats["trading_days"] == 2


def test_etf_enriched_returns_none_when_both_new_and_legacy_absent(tmp_path):
    """新旧存储都没有时返回 None。"""
    assert _safe_aggregate_etf_enriched(FakeRepo(tmp_path)) is None


def test_etf_enriched_legacy_fallback_excludes_future_partitions_via_ceiling(tmp_path):
    """兼容回退遵守 provider-confirmed read ceiling，不计入未来/未确认分区。"""
    _mkpart(tmp_path, "kline_index_enriched", ("2026-08-10", "2026-08-11"))
    repo = FakeRepo(tmp_path)
    repo.enriched_read_ceiling = date(2026, 8, 10)

    stats = _safe_aggregate_etf_enriched(repo)

    assert stats["latest_date"] == "2026-08-10"
    assert stats["trading_days"] == 1