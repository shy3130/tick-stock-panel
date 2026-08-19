import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import polars as pl

import app.api.data as data_api
from app.api.data import (
    _daily_freshness,
    _last_pipeline,
    _safe_aggregate_adj_factor,
    _safe_aggregate_daily,
    _safe_aggregate_enriched,
    _safe_aggregate_etf_daily,
    _safe_aggregate_etf_enriched,
    _safe_aggregate_financials,
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
        self.enriched_read_ceiling = None

    def execute_one(self, sql):
        if "count(DISTINCT symbol)" in sql:
            return (2,)
        return None

def _isolate_status_env(monkeypatch, tmp_path):
    """隔离外部依赖：canonical 根指向不存在路径、水位读取不触 provider。"""
    monkeypatch.setenv("TICKFLOW_CANONICAL_HISTORY_ROOT", str(tmp_path / "no-canonical"))
    monkeypatch.setattr(data_api, "_daily_watermark", lambda repo: None)


def _publish_canonical(root, *, start, end, rows, symbols, trading_days):
    """在指定根目录发布一个最小合法 canonical history generation。"""
    generation = "20260815T120000-abc12345"
    gen_dir = root / "generations" / generation
    gen_dir.mkdir(parents=True)
    manifest = {
        "generation": generation,
        "path": f"generations/{generation}",
        "start_date": start,
        "end_date": end,
        "rows": rows,
        "symbols": symbols,
        "trading_days": trading_days,
    }
    (gen_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "current.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_daily_status_uses_enriched_when_local_raw_mirror_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    _isolate_status_env(monkeypatch, tmp_path)
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
    _isolate_status_env(monkeypatch, tmp_path)
    (tmp_path / "kline_daily" / "date=2026-08-09").mkdir(parents=True)
    (tmp_path / "kline_daily_enriched" / "date=2026-08-10").mkdir(parents=True)

    stats = _safe_aggregate_daily(FakeRepo(tmp_path))

    assert stats["latest_date"] == "2026-08-10"
    assert stats["source"] == "fquant_local_enriched"


def test_enriched_status_honors_provider_confirmed_read_ceiling(tmp_path, monkeypatch):
    _isolate_status_env(monkeypatch, tmp_path)
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
        "available": True,
        "storage_mode": "persisted",
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


# ----------------------------------------------------------------------
# canonical 全历史 + 本地 overlay 合并：可查询范围、manifest 权威统计、
# 最新分区精确标的数、universe、freshness、storage_mode。
# ----------------------------------------------------------------------


def _mk_enriched_part(root, ds, symbols):
    out = root / "kline_daily_enriched" / f"date={ds}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {"symbol": list(symbols), "date": [ds] * len(symbols)}
    ).write_parquet(out)


def test_enriched_status_merges_canonical_history_with_local_overlay(tmp_path, monkeypatch):
    """已发布 canonical 全历史时,统计表示可查询范围而非仅本地 overlay 起点。"""
    _isolate_status_env(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "TICKFLOW_CANONICAL_HISTORY_ROOT", str(tmp_path / "canonical")
    )
    _publish_canonical(
        tmp_path / "canonical",
        start="1990-01-02",
        end="2026-08-10",
        rows=17_000_000,
        symbols=5400,
        trading_days=8900,
    )
    _mk_enriched_part(tmp_path, "2024-10-09", ("600519.SH",))
    _mk_enriched_part(tmp_path, "2026-08-14", ("600519.SH", "000001.SZ", "300750.SZ"))
    repo = FakeRepo(tmp_path)
    repo.enriched_read_ceiling = date(2026, 8, 14)

    stats = _safe_aggregate_enriched(repo)

    assert stats["earliest_date"] == "1990-01-02"  # 不再只显示 2024-10-09
    assert stats["latest_date"] == "2026-08-14"
    # canonical 8900 天 + 本地在 canonical 窗口外新增的 2026-08-14
    assert stats["trading_days"] == 8901
    assert stats["rows"] == 17_000_000  # manifest 权威统计,已知下界
    assert stats["row_count_exact"] is False
    assert stats["symbols_covered"] == 5400
    assert stats["universe_symbols"] == 2
    assert stats["latest_partition_symbols"] == 3  # 最新本地分区 symbol 列精确计数
    assert stats["local_overlay"] == {
        "earliest_date": "2024-10-09",
        "latest_date": "2026-08-14",
        "trading_days": 2,
    }
    assert stats["canonical_history"] == {
        "generation": "20260815T120000-abc12345",
        "earliest_date": "1990-01-02",
        "latest_date": "2026-08-10",
        "rows": 17_000_000,
        "symbols": 5400,
        "trading_days": 8900,
    }
    assert stats["storage_mode"] == "persisted"
    assert "canonical" in stats["status_message"]
    assert stats["freshness"]["status"] in ("current", "awaiting_publish", "unknown")


def test_daily_local_mode_status_also_merges_canonical(tmp_path, monkeypatch):
    """本地 fquant 模式的 daily 卡片同样展示 canonical+overlay 合并范围。"""
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    _isolate_status_env(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "TICKFLOW_CANONICAL_HISTORY_ROOT", str(tmp_path / "canonical")
    )
    _publish_canonical(
        tmp_path / "canonical",
        start="1990-01-02",
        end="2026-08-10",
        rows=100,
        symbols=5000,
        trading_days=100,
    )
    _mk_enriched_part(tmp_path, "2026-08-11", ("600519.SH",))

    stats = _safe_aggregate_daily(FakeRepo(tmp_path))

    assert stats["earliest_date"] == "1990-01-02"
    assert stats["latest_date"] == "2026-08-11"
    assert stats["trading_days"] == 101  # canonical 100 + 窗口外本地 1 天
    assert stats["source"] == "fquant_local_enriched"
    assert stats["raw_mirror_disabled"] is True
    assert stats["canonical_history"]["rows"] == 100


def test_enriched_status_canonical_only_without_local_overlay(tmp_path, monkeypatch):
    """无本地 overlay 分区但 canonical 已发布时,仍返回 canonical 可查询统计。"""
    _isolate_status_env(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "TICKFLOW_CANONICAL_HISTORY_ROOT", str(tmp_path / "canonical")
    )
    _publish_canonical(
        tmp_path / "canonical",
        start="1990-01-02",
        end="2026-08-10",
        rows=10,
        symbols=10,
        trading_days=10,
    )

    stats = _safe_aggregate_enriched(FakeRepo(tmp_path))

    assert stats is not None
    assert stats["earliest_date"] == "1990-01-02"
    assert stats["latest_date"] == "2026-08-10"
    assert stats["trading_days"] == 10
    assert "local_overlay" not in stats
    assert "latest_partition_symbols" not in stats


def test_enriched_status_without_canonical_keeps_local_behavior(tmp_path, monkeypatch):
    """canonical manifest 缺失时保持本地行为,仅追加轻量可选字段。"""
    _isolate_status_env(monkeypatch, tmp_path)
    _mk_enriched_part(tmp_path, "2026-08-10", ("600519.SH", "000001.SZ"))

    stats = _safe_aggregate_enriched(FakeRepo(tmp_path))

    assert stats["rows"] == 0
    assert stats["row_count_exact"] is False
    assert stats["earliest_date"] == "2026-08-10"
    assert stats["latest_date"] == "2026-08-10"
    assert stats["trading_days"] == 1
    assert "canonical_history" not in stats
    assert stats["universe_symbols"] == 2
    assert stats["latest_partition_symbols"] == 2
    assert stats["storage_mode"] == "persisted"
    assert "未发布 canonical" in stats["status_message"]
    assert set(stats["freshness"]) == {"status", "age_days", "reference_date", "reason"}


def test_merged_stats_clamp_canonical_latest_by_read_ceiling(tmp_path, monkeypatch):
    """manifest end_date 领先 read ceiling 时,可查询 latest 按水位夹逼。"""
    _isolate_status_env(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "TICKFLOW_CANONICAL_HISTORY_ROOT", str(tmp_path / "canonical")
    )
    _publish_canonical(
        tmp_path / "canonical",
        start="2020-01-01",
        end="2026-08-12",
        rows=1000,
        symbols=4000,
        trading_days=1600,
    )
    _mk_enriched_part(tmp_path, "2026-08-11", ("600519.SH",))
    repo = FakeRepo(tmp_path)
    repo.enriched_read_ceiling = date(2026, 8, 11)

    stats = _safe_aggregate_enriched(repo)

    assert stats["latest_date"] == "2026-08-11"
    # canonical_history 块仍报告 manifest 原始值(权威历史统计)
    assert stats["canonical_history"]["latest_date"] == "2026-08-12"


# ----------------------------------------------------------------------
# adj_factor provider_on_demand：无本地文件 ≠ 无数据。
# ----------------------------------------------------------------------


def test_adj_factor_without_file_reports_provider_on_demand(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)

    class Caps:
        adj_factor = True

    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )
    monkeypatch.setattr(
        "app.data_providers.registry.get_provider",
        lambda name: SimpleNamespace(capabilities=Caps()),
    )

    stats = _safe_aggregate_adj_factor(FakeRepo(tmp_path))

    assert stats is not None
    assert stats["available"] is True
    assert stats["storage_mode"] == "provider_on_demand"
    assert stats["rows"] == 0
    assert stats["row_count_exact"] is False
    assert "provider" in stats["status_message"]


def test_adj_factor_without_file_and_capability_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)

    class Caps:
        adj_factor = False

    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: "fquant_local",
    )
    monkeypatch.setattr(
        "app.data_providers.registry.get_provider",
        lambda name: SimpleNamespace(capabilities=Caps()),
    )

    assert _safe_aggregate_adj_factor(FakeRepo(tmp_path)) is None


def test_adj_factor_on_demand_requires_local_fquant_mode(tmp_path, monkeypatch):
    """非本地 fquant 模式（如 fquant 付费档）无文件时保持 None。"""
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    monkeypatch.setattr(
        "app.data_providers.registry.get_provider",
        lambda name: (_ for _ in ()).throw(AssertionError("provider must not run")),
    )

    assert _safe_aggregate_adj_factor(FakeRepo(tmp_path)) is None


# ----------------------------------------------------------------------
# financials：纳入 forecast/quick，按 schema 自适应读取日期列。
# ----------------------------------------------------------------------


def test_financials_stats_include_forecast_and_adaptive_date_columns(tmp_path):
    metrics = tmp_path / "financials" / "metrics"
    metrics.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600519.SH", "000001.SZ"],
            "t_date": [date(2025, 12, 31), date(2026, 3, 31)],
        }
    ).write_parquet(metrics / "part.parquet")

    income = tmp_path / "financials" / "income"
    income.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600519.SH"],
            "report_date": [date(2026, 4, 30)],  # 旧布局:无 t_date,用 report_date
        }
    ).write_parquet(income / "part.parquet")

    forecast = tmp_path / "financials" / "forecast"
    forecast.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["000001.SZ", "300750.SZ"],
            "notice_date": [date(2026, 7, 10), date(2026, 7, 15)],
        }
    ).write_parquet(forecast / "part.parquet")

    stats = _safe_aggregate_financials(FakeRepo(tmp_path))

    assert stats["rows"] == 5
    assert stats["tables"]["metrics"] == {
        "rows": 2,
        "symbols": 2,
        "earliest_date": "2025-12-31",
        "latest_date": "2026-03-31",
    }
    assert stats["tables"]["income"] == {
        "rows": 1,
        "symbols": 1,
        "earliest_date": "2026-04-30",
        "latest_date": "2026-04-30",
    }
    assert stats["tables"]["forecast"]["rows"] == 2
    assert stats["tables"]["forecast"]["earliest_date"] == "2026-07-10"
    assert stats["tables"]["forecast"]["latest_date"] == "2026-07-15"
    assert stats["tables"]["balance_sheet"] == {"rows": 0, "symbols": 0}
    assert stats["tables"]["quick"] == {"rows": 0, "symbols": 0}


# ----------------------------------------------------------------------
# freshness：保守、可解释；周末不误报；区分 provider 对齐与等待发布。
# ----------------------------------------------------------------------

_CN_TZ = timezone(timedelta(hours=8))


def test_freshness_weekend_does_not_misreport():
    """周六/周日已覆盖周五数据 → current,不得误报 awaiting_publish。"""
    saturday_morning = datetime(2026, 8, 15, 10, 0, tzinfo=_CN_TZ)  # 周六
    result = _daily_freshness(date(2026, 8, 14), None, now=saturday_morning)
    assert result == {
        "status": "current",
        "age_days": 0,
        "reference_date": "2026-08-14",
        "reason": "已覆盖最近收盘交易日 2026-08-14（按周一~周五日历推断）",
    }


def test_freshness_monday_before_publish_time_uses_friday_reference():
    """周一盘前(未到发布时间)期望基准是上周五。"""
    monday_morning = datetime(2026, 8, 17, 9, 0, tzinfo=_CN_TZ)  # 周一 09:00
    result = _daily_freshness(date(2026, 8, 14), None, now=monday_morning)
    assert result["status"] == "current"
    assert result["reference_date"] == "2026-08-14"


def test_freshness_monday_evening_awaits_publish_by_calendar():
    monday_evening = datetime(2026, 8, 17, 17, 0, tzinfo=_CN_TZ)  # 周一 17:00
    result = _daily_freshness(date(2026, 8, 14), None, now=monday_evening)
    assert result["status"] == "awaiting_publish"
    assert result["age_days"] == 3
    assert result["reference_date"] == "2026-08-17"
    assert "日历" in result["reason"]


def test_freshness_provider_watermark_aligned_is_current():
    sunday = datetime(2026, 8, 16, 12, 0, tzinfo=_CN_TZ)  # 周日
    result = _daily_freshness(date(2026, 8, 14), date(2026, 8, 14), now=sunday)
    assert result["status"] == "current"
    assert result["reference_date"] == "2026-08-14"
    assert "provider 水位对齐" in result["reason"]


def test_freshness_exposes_stale_upstream_watermark_after_close():
    """本地虽与 provider 对齐，但 provider 自身落后最近收盘日时仍须告警。"""
    tuesday_evening = datetime(2026, 8, 18, 21, 0, tzinfo=_CN_TZ)

    result = _daily_freshness(
        date(2026, 8, 17),
        date(2026, 8, 17),
        now=tuesday_evening,
    )

    assert result["status"] == "awaiting_publish"
    assert result["age_days"] == 1
    assert result["reference_date"] == "2026-08-18"
    assert "上游快照尚未发布" in result["reason"]


def test_freshness_provider_watermark_ahead_awaits_publish():
    result = _daily_freshness(date(2026, 8, 12), date(2026, 8, 14))
    assert result["status"] == "awaiting_publish"
    assert result["age_days"] == 2
    assert result["reference_date"] == "2026-08-14"
    assert "provider" in result["reason"]


def test_freshness_unknown_without_local_data():
    result = _daily_freshness(None, None)
    assert result["status"] == "unknown"
    assert result["age_days"] is None


def test_daily_watermark_does_not_treat_canonical_ceiling_as_provider(monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.jobs.daily_pipeline._provider_freshness_date", lambda: None)
    repo = SimpleNamespace(enriched_read_ceiling=date(2026, 8, 17))

    assert data_api._daily_watermark(repo) is None


# ----------------------------------------------------------------------
# last_pipeline：最近管道终态 + failed_stages/error。
# ----------------------------------------------------------------------


def _job(
    job_id,
    status,
    *,
    result=None,
    error=None,
    stage="done",
    kind=None,
    started="2026-08-17T07:00:00Z",
):
    return {
        "id": job_id,
        "kind": kind,
        "status": status,
        "stage": stage,
        "progress": 100,
        "stage_pct": 100,
        "started_at": started,
        "finished_at": f"{started[:-2]}1Z",
        "duration_s": 60.0,
        "result": result,
        "error": error,
    }


def _stub_jobs(monkeypatch, jobs):
    from app.services.pipeline_jobs import job_store

    monkeypatch.setattr(job_store, "list_recent", lambda limit=20: jobs)
    data_api._last_finished_cache = None
    data_api._last_pipeline_cache = None


def test_last_pipeline_returns_latest_terminal_pipeline(monkeypatch):
    _stub_jobs(
        monkeypatch,
        [
            _job(
                "new",
                "succeeded",
                result={"daily_days": 1, "enriched_rows": 123},
                started="2026-08-18T07:00:00Z",
            ),
            _job(
                "old",
                "degraded",
                result={"daily_days": 1, "failed_stages": [{"stage": "sync_minute", "error": "x"}]},
                started="2026-08-17T07:00:00Z",
            ),
        ],
    )

    payload = _last_pipeline()

    assert payload == {
        "status": "succeeded",
        "finished_at": "2026-08-18T07:00:01Z",
        "error": None,
        "failed_stages": [],
    }


def test_last_pipeline_carries_degraded_failed_stages(monkeypatch):
    _stub_jobs(
        monkeypatch,
        [
            _job(
                "deg",
                "degraded",
                result={
                    "daily_days": 1,
                    "failed_stages": [{"stage": "sync_minute", "error": "broken catalog"}],
                },
            )
        ],
    )

    payload = _last_pipeline()

    assert payload["status"] == "degraded"
    assert payload["failed_stages"] == [{"stage": "sync_minute", "error": "broken catalog"}]
    assert payload["error"] is None


def test_last_pipeline_carries_scheduled_failure_error(monkeypatch):
    """失败的分钟同步 job 不得遮蔽更早的调度管道失败。"""
    _stub_jobs(
        monkeypatch,
        [
            _job("min", "failed", error="broken", stage="sync_minute", started="2026-08-18T09:00:00Z"),
            _job(
                "pipe",
                "failed",
                error="scheduled daily_pipeline failed",
                stage="compute_enriched",
                started="2026-08-17T07:00:00Z",
            ),
        ],
    )

    payload = _last_pipeline()

    assert payload["status"] == "failed"
    assert payload["error"] == "scheduled daily_pipeline failed"
    assert payload["failed_stages"] == []


def test_last_pipeline_recognizes_failure_before_first_progress_event(monkeypatch):
    _stub_jobs(
        monkeypatch,
        [
            _job(
                "pipe",
                "failed",
                kind="daily_pipeline",
                stage="init",
                error="instrument sync failed",
            )
        ],
    )

    payload = _last_pipeline()

    assert payload["status"] == "failed"
    assert payload["error"] == "instrument sync failed"


def test_last_pipeline_returns_none_without_terminal_jobs(monkeypatch):
    _stub_jobs(monkeypatch, [_job("run", "running", result=None, started="2026-08-18T07:00:00Z")])
    assert _last_pipeline() is None