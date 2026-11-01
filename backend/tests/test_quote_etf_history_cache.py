from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.services import quote_service

TODAY = date(2026, 10, 29)


@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.setattr(quote_service, "cn_today", lambda: TODAY)
    days = [TODAY - timedelta(days=i) for i in range(1, 145)]
    days = sorted(d for d in days if d.weekday() < 5)
    for i, day in enumerate(days):
        frame = pl.DataFrame({
            "symbol": ["510300.SH", "159915.SZ"], "date": [day] * 2,
            "open": [10.0, 20.0], "high": [11.0+i/100, 21.0+i/100],
            "low": [9.0, 19.0], "close": [10.0+i/100, 20.0+i/100],
            "volume": [10000.0, 20000.0], "amount": [100000.0, 400000.0],
        })
        path = tmp_path / "kline_etf_daily" / f"date={day}" / "part.parquet"
        path.parent.mkdir(parents=True)
        frame.write_parquet(path)
    today = frame.with_columns(pl.lit(TODAY).alias("date"))
    output = []
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_latest_asset=lambda _: (today, TODAY),
        flush_live_enriched_asset=lambda asset, df: output.append(df),
        merge_live_enriched_asset=lambda asset, df: output.append(df),
    )
    service = quote_service.QuoteService()
    service.set_repo(repo)
    return service, repo, today, output, days


def test_warm_etf_updates_do_not_rescan_history(sample, monkeypatch):
    service, _repo, today, output, _ = sample
    original = quote_service.guarded_collect
    scans = []

    def collect(*args, **kwargs):
        scans.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(quote_service, "guarded_collect", collect)
    service._flush_live_enriched(today, asset_type="etf")
    service._flush_live_enriched(today, asset_type="etf", merge=True)
    assert len(output) == 2
    assert_frame_equal(output[0], output[1], check_exact=True)
    assert len(scans) == 1


@pytest.mark.parametrize("change", ["history", "factor", "rollover", "new_symbol", "delete"])
def test_warm_etf_output_matches_cold_recompute_after_changes(sample, monkeypatch, change):
    service, repo, today, output, days = sample
    service._flush_live_enriched(today, asset_type="etf")
    root = repo.store.data_dir
    if change == "history":
        path = root / "kline_etf_daily" / f"date={days[-1]}" / "part.parquet"
        pl.read_parquet(path).with_columns(pl.lit(15.0).alias("close")).write_parquet(path)
    elif change == "delete":
        (root / "kline_etf_daily" / f"date={days[-1]}" / "part.parquet").unlink()
    elif change == "factor":
        path = root / "adj_factor_etf/all.parquet"
        path.parent.mkdir(parents=True)
        pl.DataFrame({"symbol": ["510300.SH"], "trade_date": [days[-5]], "ex_factor": [1.1]}).write_parquet(path)
    elif change == "rollover":
        path = root / "kline_etf_daily" / f"date={TODAY}" / "part.parquet"
        path.parent.mkdir(parents=True)
        today.write_parquet(path)
        monkeypatch.setattr(quote_service, "cn_today", lambda: TODAY + timedelta(days=1))
        today = today.with_columns(pl.lit(TODAY + timedelta(days=1)).alias("date"))
    else:
        today = pl.concat([today, today.head(1).with_columns(pl.lit("510500.SH").alias("symbol"))])
    today = today.with_columns((pl.col("close") + 0.25).alias("close"))
    service._flush_live_enriched(today, asset_type="etf")
    cold = quote_service.QuoteService()
    cold.set_repo(repo)
    # Compare with the original uncached glob-read path, including all numeric
    # columns, nulls, adjusted prices and indicator warmup boundaries.
    monkeypatch.setattr(cold, "_etf_live_history", lambda *args: None, raising=False)
    cold._flush_live_enriched(today, asset_type="etf")
    assert len(output) == 3
    assert_frame_equal(output[-2], output[-1], check_exact=True)


def test_today_disk_writes_do_not_invalidate_history(sample, monkeypatch):
    service, repo, today, output, _ = sample
    original = quote_service.guarded_collect
    scans = []

    def collect(*args, **kwargs):
        scans.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(quote_service, "guarded_collect", collect)
    service._flush_live_enriched(today, asset_type="etf")
    path = repo.store.data_dir / "kline_etf_daily" / f"date={TODAY}" / "part.parquet"
    path.parent.mkdir(parents=True)
    today = today.with_columns((pl.col("close") + 0.5).alias("close"))
    today.write_parquet(path)
    service._flush_live_enriched(today, asset_type="etf")
    assert len(scans) == 1
    assert_frame_equal(output[-1].select("symbol", "raw_close").sort("symbol"),
                       today.select("symbol", pl.col("close").alias("raw_close")).sort("symbol"))


def test_oversized_history_is_not_retained_and_repo_change_clears_cache(sample, monkeypatch):
    service, repo, today, output, _ = sample
    service._flush_live_enriched(today, asset_type="etf")
    assert service._etf_history_cache is not None
    service.set_repo(repo)
    assert service._etf_history_cache is None
    monkeypatch.setattr(pl.DataFrame, "estimated_size", lambda *args: 65 * 1024**2)
    service._flush_live_enriched(today, asset_type="etf")
    assert service._etf_history_cache is None
    assert_frame_equal(output[-2], output[-1], check_exact=True)
