"""策略指数K线访问模块 — 测试。"""
import datetime

import polars as pl
import pytest

from app.strategy import market_data
from app.strategy.ai_generator import AIStrategyGenerator


def test_whitelist_allows_market_data_import():
    AIStrategyGenerator._validate_safety(
        "from app.strategy.market_data import get_index_daily, get_daily, get_ext_timeseries"
    )


def test_whitelist_still_blocks_dangerous():
    with pytest.raises(ValueError):
        AIStrategyGenerator._validate_safety("import os")
    with pytest.raises(ValueError):
        AIStrategyGenerator._validate_safety("from os import path")
    with pytest.raises(ValueError):
        AIStrategyGenerator._validate_safety("getattr(obj, '__globals__')")


class _FakeRepo:
    """最小 fake: 只实现 market_data 用到的接口。"""
    def __init__(self, index_df=None, data_dir=None):
        self.calls: list[tuple] = []
        self.data_dir = data_dir
        self._asset = {"000001.SH": "index", "510300.SH": "etf", "600000.SH": "stock"}
        self._index_df = index_df if index_df is not None else pl.DataFrame(
            {"date": ["2026-01-02"], "close": [3000.0], "macd_dif": [1.0], "macd_dea": [2.0]}
        )
        self._empty = pl.DataFrame()

    def resolve_asset_type(self, symbol):
        self.calls.append(("resolve", symbol))
        return self._asset.get(symbol, "stock")

    def get_index_daily(self, symbol, start=None, end=None, columns=None):
        self.calls.append(("index", symbol, start, end, columns))
        return self._index_df if symbol == "000001.SH" else self._empty

    def get_etf_daily(self, symbol, start=None, end=None, columns=None):
        self.calls.append(("etf", symbol, start, end, columns))
        return self._empty

    def get_daily(self, symbol, start=None, end=None, columns=None):
        self.calls.append(("stock", symbol, start, end, columns))
        return self._empty

    def get_instruments_asset(self, asset_type):
        return pl.DataFrame({"symbol": ["000001.SH"], "name": ["上证指数"]})


@pytest.fixture()
def fake_repo():
    fake = _FakeRepo()
    market_data._set_repo(fake)
    yield fake
    market_data._reset_repo()


def test_get_index_daily_delegates_and_normalizes_dates(fake_repo):
    df = market_data.get_index_daily(
        "000001.SH", start="2026-01-01", end="2026-01-31", columns=["date", "close"]
    )
    assert df.height == 1 and df["close"][0] == 3000.0
    _, sym, s, e, cols = fake_repo.calls[-1]
    assert sym == "000001.SH"
    assert s == datetime.date(2026, 1, 1)
    assert e == datetime.date(2026, 1, 31)
    assert cols == ["date", "close"]


@pytest.mark.parametrize("symbol,expected_kind", [
    ("000001.SH", "index"),
    ("510300.SH", "etf"),
    ("600000.SH", "stock"),
])
def test_get_daily_dispatch_by_asset_type(fake_repo, symbol, expected_kind):
    market_data.get_daily(symbol)
    last = fake_repo.calls[-1]
    assert last[0] == expected_kind
    assert last[1] == symbol


def test_bad_symbol_returns_empty_without_calling_repo(fake_repo):
    assert market_data.get_index_daily("").is_empty()
    assert market_data.get_index_daily(None).is_empty()
    assert market_data.get_etf_daily("").is_empty()
    assert market_data.get_daily(None).is_empty()
    assert fake_repo.calls == []


def test_missing_symbol_returns_empty_no_raise(fake_repo):
    assert market_data.get_index_daily("999999.SH").is_empty()


def test_list_index_symbols(fake_repo):
    assert market_data.list_index_symbols() == [{"symbol": "000001.SH", "name": "上证指数"}]


BJ = datetime.date(2026, 3, 2)  # 钉死的北京日期, 不会碰巧等于跑测试那天的 date.today()


@pytest.mark.parametrize("fn,symbol,kind", [
    ("get_index_daily", "000001.SH", "index"),
    ("get_etf_daily", "510300.SH", "etf"),
    ("get_daily", "600000.SH", "stock"),
])
def test_default_end_is_beijing_today(fake_repo, monkeypatch, fn, symbol, kind):
    """未传 end 时日K窗口右端必须是北京今天, 不能用服务器本地 date.today()。

    CONTRIBUTING §3.3: A 股交易时段按北京时间, 服务器时区不能成为隐式输入。
    自定义/AI 策略在策略页读指数/ETF/个股日K 时常省略 end, 缺省走本模块。

    美西主机整个 A 股交易时段、UTC 主机北京 00:00-08:00, 本地日历日比北京
    早一天: 管道已写入的当日官方 K 被排除, 相对强弱/对照指数停在昨天。

    raising=False: 未修复代码没有调用 cn_today, 钉了也不会被用到,
    仍走 date.today() → 与 2026-03-02 不等。
    """
    monkeypatch.setattr(market_data, "cn_today", lambda: BJ, raising=False)
    getattr(market_data, fn)(symbol)
    last = fake_repo.calls[-1]
    assert last[0] == kind
    end = last[3]
    assert end == BJ, f"窗口右端必须是北京日期 {BJ}, 实际 {end} (服务器本地 {datetime.date.today()})"
    assert end != datetime.date.today()


# ── get_ext_timeseries: 扩展时序表原始行 (日内多盘不收敛) ──────────

def _write_ext_partition(base, day: str, rows: dict) -> None:
    d = base / "ext_data" / "ext_auc_test" / "timeseries" / f"date={day}"
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(d / "part.parquet")


@pytest.fixture()
def ext_repo(tmp_path):
    market_data._set_repo(_FakeRepo(data_dir=tmp_path))
    yield tmp_path
    market_data._reset_repo()


def test_get_ext_timeseries_keeps_intraday_rows(ext_repo):
    """日内多盘不收敛: 同 symbol 同日 3 盘原样返回 (因子通道只给末盘)。"""
    _write_ext_partition(
        ext_repo, "2026-01-02",
        {
            "symbol": ["000001.SZ"] * 3,
            "ts": ["2026-01-02T09:20:01+08:00", "2026-01-02T09:20:11+08:00", "2026-01-02T09:25:01+08:00"],
            "auction_price": [10.0, 10.0, 10.5],
            "date": ["2026-01-02"] * 3,  # 行内 date 应被分区名覆盖 (同值, 防漂移)
        },
    )
    df = market_data.get_ext_timeseries("ext_auc_test", columns=["ts", "auction_price"])
    assert df.height == 3
    assert set(df.columns) == {"symbol", "date", "ts", "auction_price"}
    assert df["date"].unique().to_list() == ["2026-01-02"]
    assert df["ts"].to_list() == sorted(df["ts"].to_list())


def test_get_ext_timeseries_date_range_and_multi_partition(ext_repo):
    _write_ext_partition(
        ext_repo, "2026-01-02", {"symbol": ["000001.SZ"], "ts": ["t1"], "auction_price": [1.0]},
    )
    _write_ext_partition(
        ext_repo, "2026-01-05", {"symbol": ["000001.SZ"], "ts": ["t2"], "auction_price": [1.1]},
    )
    df = market_data.get_ext_timeseries("ext_auc_test", start="2026-01-03", end="2026-01-10")
    assert df["date"].to_list() == ["2026-01-05"]
    full = market_data.get_ext_timeseries("ext_auc_test")
    assert sorted(full["date"].to_list()) == ["2026-01-02", "2026-01-05"]


def test_get_ext_timeseries_skips_corrupt_partition(ext_repo):
    _write_ext_partition(
        ext_repo, "2026-01-02", {"symbol": ["000001.SZ"], "ts": ["t1"], "auction_price": [1.0]},
    )
    bad = ext_repo / "ext_data" / "ext_auc_test" / "timeseries" / "date=2026-01-03"
    bad.mkdir(parents=True)
    (bad / "part.parquet").write_bytes(b"not-a-parquet")
    df = market_data.get_ext_timeseries("ext_auc_test")
    assert df["date"].to_list() == ["2026-01-02"]


def test_get_ext_timeseries_missing_or_bad_config(ext_repo):
    assert market_data.get_ext_timeseries("no_such_table").is_empty()
    assert market_data.get_ext_timeseries("../secrets").is_empty()
    assert market_data.get_ext_timeseries("").is_empty()
    assert market_data.get_ext_timeseries(None).is_empty()
