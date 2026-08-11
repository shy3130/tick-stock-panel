"""指数资产路由 — repository 层测试。"""
import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def _write_index_instruments(repo, rows):
    pl.DataFrame(rows).write_parquet(
        repo.store.data_dir / "instruments_index" / "part.parquet"
    )
    repo._refresh_index_instruments()


def test_name_map_includes_index(repo):
    _write_index_instruments(repo, {
        "symbol": ["000001.SH"], "name": ["上证指数"],
        "code": ["000001"], "asset_type": ["index"],
    })
    names = repo.get_name_map(["000001.SH", "600000.SH"])
    assert names.get("000001.SH") == "上证指数"
    assert "600000.SH" not in names  # 未收录不造名


def test_name_map_stock_beats_index(repo):
    """同名 symbol 同时出现在股票/指数维表时, 股票名称优先。"""
    _write_index_instruments(repo, {
        "symbol": ["600000.SH"], "name": ["某指数"],
        "code": ["600000"], "asset_type": ["index"],
    })
    pl.DataFrame({
        "symbol": ["600000.SH"], "name": ["浦发银行"], "code": ["600000"],
        "exchange": ["SH"], "region": ["CN"], "type": ["stock"],
        "listing_date": [None], "total_shares": [None], "float_shares": [None],
        "tick_size": [None], "limit_up": [None], "limit_down": [None],
        "as_of": ["2026-07-25"],
    }).write_parquet(repo.store.data_dir / "instruments" / "instruments.parquet")
    repo._refresh_instruments()
    assert repo.get_name_map(["600000.SH"]).get("600000.SH") == "浦发银行"


import datetime as _dt


def _write_index_enriched(repo, dates_rows):
    for ds, rows in dates_rows.items():
        d = repo.store.data_dir / "kline_index_enriched" / f"date={ds}"
        d.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(rows).write_parquet(d / "part.parquet")


def _index_rows(ds, close=3000.0):
    return [{
        "symbol": "000001.SH", "date": _dt.date.fromisoformat(ds),
        "open": close - 10, "high": close + 20, "low": close - 20, "close": close,
        "volume": 1_000_000, "amount": 1e9,
    }]


def test_get_enriched_latest_asset_index(repo):
    _write_index_enriched(repo, {
        "2026-07-23": _index_rows("2026-07-23", 2990.0),
        "2026-07-24": _index_rows("2026-07-24", 3000.0),
    })
    df, dt = repo.get_enriched_latest_asset("index")
    assert str(dt) == "2026-07-24"
    assert df["symbol"].to_list() == ["000001.SH"]
    assert "ma5" in df.columns or "rsi_14" in df.columns  # 重算产出指标列


def test_get_enriched_latest_asset_index_cold_no_refresh(repo):
    df, dt = repo.get_enriched_latest_asset("index", refresh=False)
    assert df.is_empty() and dt is None


def test_flush_live_enriched_asset_index_updates_cache(repo):
    df = pl.DataFrame([{
        "symbol": "000001.SH", "date": _dt.date(2026, 7, 25),
        "open": 3000.0, "high": 3010.0, "low": 2990.0, "close": 3005.0,
        "volume": 1_000_000, "amount": 1e9, "ma5": 3001.0, "rsi_14": 55.0,
    }])
    repo.flush_live_enriched_asset("index", df)
    cached, dt = repo.get_enriched_latest_asset("index", refresh=False)
    assert str(dt) == "2026-07-25"
    assert cached["close"].to_list() == [3005.0]
    assert (repo.store.data_dir / "kline_index_enriched" / "date=2026-07-25" / "part.parquet").exists()


def _merge_row(symbol, close):
    return {
        "symbol": symbol, "date": _dt.date(2026, 7, 25),
        "open": close - 5, "high": close + 5, "low": close - 6, "close": close,
        "volume": 1_000, "amount": 1e6,
    }


def test_merge_live_enriched_asset_index_merges_cache(repo):
    """merge 路径: 两次合并缓存取并集 (不 NameError, 不丢已有缓存)。"""
    repo.merge_live_enriched_asset("index", pl.DataFrame([_merge_row("000001.SH", 3000.0)]))
    repo.merge_live_enriched_asset("index", pl.DataFrame([_merge_row("000300.SH", 4000.0)]))
    cached, dt = repo.get_enriched_latest_asset("index", refresh=False)
    assert str(dt) == "2026-07-25"
    assert set(cached["symbol"].to_list()) == {"000001.SH", "000300.SH"}
