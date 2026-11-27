from datetime import date, datetime
from pathlib import Path, PureWindowsPath

import polars as pl
import pytest

from app import config
from app.parquet import parquet_glob
from app.services.kline_sync import _write_minute_partition
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 11, 25)


def _daily(symbol: str, close: float, day: date = DAY) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol], "date": [day], "open": [close], "high": [close],
        "low": [close], "close": [close], "volume": [100.0], "amount": [close * 100],
    })


def _minute(symbol: str, close: float) -> pl.DataFrame:
    return _daily(symbol, close).drop("date").with_columns(
        pl.lit(datetime(2026, 11, 25, 9, 30)).alias("datetime"),
    )


@pytest.mark.parametrize("with_sibling", [False, True])
def test_incremental_market_writes_preserve_rows_in_the_literal_directory(tmp_path, with_sibling):
    target = tmp_path / "Cache [AB]"
    sibling = tmp_path / "Cache A"
    for path, symbol, close in ((target, "000001.SZ", 10.0), (sibling, "000002.SZ", 90.0)):
        if path == sibling and not with_sibling:
            continue
        store = DataStore(path, defer_database=True)
        KlineRepository(store).append_daily(_daily(symbol, close))
        _write_minute_partition(_minute(symbol, close), path / "kline_minute")
        store.close()

    store = DataStore(target, defer_database=True)
    KlineRepository(store).append_daily(_daily("000003.SZ", 20.0))
    _write_minute_partition(_minute("000003.SZ", 20.0), target / "kline_minute")
    store.close()

    for table in ("kline_daily", "kline_minute"):
        partition = Path(table) / f"date={DAY}" / "part.parquet"
        actual = pl.read_parquet(target / partition, glob=False).sort("symbol")
        assert actual.select("symbol", "close").rows() == [("000001.SZ", 10.0), ("000003.SZ", 20.0)]
        if with_sibling:
            assert pl.read_parquet(sibling / partition, glob=False)["symbol"].to_list() == ["000002.SZ"]


@pytest.mark.parametrize(("directory", "alternate_case"), [
    ("Cache [AB]", False),
    ("O'Brien 行情", False),
    ("Cache [行情]", False),
    ("O'Brien [AB]", True),
])
def test_chart_reads_and_duckdb_views_follow_the_configured_directory(
    tmp_path, directory, alternate_case,
):
    target = tmp_path / directory
    symbols = {"stock": "000001.SZ", "index": "000001.SH", "etf": "510300.SH"}
    store = DataStore(target, defer_database=True)
    repo = KlineRepository(store)
    for asset, table in (
        ("stock", "kline_daily_enriched"), ("index", "kline_index_enriched"), ("etf", "kline_etf_enriched"),
    ):
        frame = _daily(symbols[asset], 10.0)
        repo.flush_live_daily_asset(asset, frame)
        repo._write_daily_partition(frame.with_columns(pl.col("close").alias("raw_close")), table)
    _write_minute_partition(_minute(symbols["stock"], 10.0), target / "kline_minute")

    if alternate_case:
        store.close()
        alias = tmp_path / directory.lower()
        if not alias.is_dir() or not alias.samefile(target):
            pytest.skip("requires a case-insensitive filesystem")
        store = DataStore(alias)
        repo = KlineRepository(store)

    try:
        for asset, symbol in symbols.items():
            assert repo.get_daily_asset(asset, symbol, DAY, DAY, ["date", "close"])["close"].to_list() == [10.0]
        assert repo.get_minute(symbols["stock"], DAY)["close"].to_list() == [10.0]
        assert repo.get_minute_range([symbols["stock"]], DAY, DAY)["close"].to_list() == [10.0]
        assert repo.get_minute_by_dates([symbols["stock"]], [DAY])["close"].to_list() == [10.0]
        assert repo.execute_one("SELECT min(close) FROM kline_daily") == (10.0,)
        repo.rebuild_views()
        assert repo.execute_one("SELECT min(close) FROM kline_index_daily") == (10.0,)
        assert repo.execute_one("SELECT min(close) FROM kline_etf_daily") == (10.0,)
        repo.flush_live_daily_asset("stock", _daily(symbols["stock"], 11.0, date(2026, 11, 26)))
        assert repo.execute_one("SELECT count(*) FROM kline_daily") == (2,)
    finally:
        store.close()


@pytest.mark.parametrize(("path_text", "expected"), [
    (r'"D:\tsp\new data"', r"D:\tsp\new data"),
    (r'"D:\\tsp\\data"', r"D:\tsp\data"),
    (r'"D:\tsp\"', r"D:\tsp"),
    (r'"\\server\share\tsp"', r"\\server\share\tsp"),
    (r'D:\tsp\data', r"D:\tsp\data"),
    (r'"D:/tsp/data"', r"D:\tsp\data"),
])
def test_windows_dotenv_data_dir_preserves_path_separators(tmp_path, monkeypatch, path_text, expected):
    env = tmp_path / ".env"
    env.write_text(f'export DATA_DIR={path_text} # data directory\nAUTH_PASSWORD="first\\tsecond"\n', encoding="utf-8")
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setattr(config, "_resolve_data_dir", lambda path: path)
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)

    settings = config.Settings(_env_file=env)
    assert PureWindowsPath(str(settings.data_dir)) == PureWindowsPath(expected)
    assert settings.auth_password == "first\tsecond"

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "override"))
    assert config.Settings(_env_file=env).data_dir == tmp_path / "override"


def test_market_glob_preserves_windows_drive_and_escapes_the_directory():
    directory = PureWindowsPath(r"D:\TSP [AB]\data")
    assert parquet_glob(directory) == "D:/TSP [[]AB]/data/**/*.parquet"
