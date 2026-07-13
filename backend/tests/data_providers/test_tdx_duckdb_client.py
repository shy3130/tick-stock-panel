"""TdxDuckDBClient 完整契约测试。"""
from __future__ import annotations

import os

import pytest

from app.data_providers.fquant import catalog_resolver
from app.data_providers.fquant.lease import ConnectionSet
from app.data_providers.fquant.tdx_duckdb_client import (
    TdxDuckDBClient,
    _CatalogSource,
    _prefixed_code,
)

TDX_PATH = "/Volumes/WD1/tdx.duckdb"
TDX_MINUTES_PATH = "/Volumes/WD1/tdx-minutes.duckdb"
CATALOG_CURRENT = os.path.join(
    os.getenv("FQUANT_SNAPSHOT_ROOT_CATALOG", "/Volumes/WD1/snapshots/catalog"),
    "current.json",
)


def test_prefixed_code():
    assert _prefixed_code("600519") == "sh600519"
    assert _prefixed_code("000001") == "sz000001"
    assert _prefixed_code("300059") == "sz300059"
    assert _prefixed_code("830799") == "bj830799"


class _FakeConnection:
    def __init__(self, path: str) -> None:
        self.path = path
        self.closed = False

    def cursor(self) -> _FakeConnection:
        return self

    def execute(self, _sql: str, _params: list[object]) -> _FakeConnection:
        return self

    def fetchall(self) -> list[tuple[str]]:
        return [(self.path,)]

    def close(self) -> None:
        self.closed = True


def test_catalog_source_routes_each_date_and_closes_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = {
        "20190710": "/snapshots/archive/2019/tdx-trans.duckdb",
        "20260710": "/snapshots/current/2026/tdx-trans.duckdb",
    }

    def resolve_route(route_key: str, market: str, trade_date) -> str:
        assert (route_key, market) == ("tdx_trans", "a")
        return paths[trade_date.strftime("%Y%m%d")]

    monkeypatch.setattr(catalog_resolver, "resolve_route", resolve_route)
    opened: list[_FakeConnection] = []
    source = _CatalogSource("tdx_trans", "a")
    source._set = ConnectionSet(
        lambda path: opened.append(_FakeConnection(path)) or opened[-1]
    )

    assert source.query("SELECT 1", [], "20190710") == [(paths["20190710"],)]
    assert source.query("SELECT 1", [], "20260710") == [(paths["20260710"],)]
    assert [connection.path for connection in opened] == list(paths.values())
    assert opened[0].closed is True
    assert opened[1].closed is False

    source.close()
    assert opened[1].closed is True


def test_catalog_source_fails_closed_on_catalog_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object) -> str:
        raise catalog_resolver.StaleCatalogError("stale")

    monkeypatch.setattr(catalog_resolver, "resolve_route", fail)
    source = _CatalogSource("tdx_minutes", "a")
    source._set = ConnectionSet(lambda _path: pytest.fail("must not open a raw database"))

    assert source.query("SELECT 1", [], "20260710") == []


@pytest.mark.parametrize("date_yyyymmdd", ["bad", "20260230", "20260706junk"])
def test_catalog_source_rejects_invalid_date_before_resolve_or_open(
    monkeypatch: pytest.MonkeyPatch, date_yyyymmdd: str
) -> None:
    source = _CatalogSource("tdx_minutes", "a")
    monkeypatch.setattr(
        source,
        "_ensure_set",
        lambda: pytest.fail("invalid date must not initialize connections"),
    )
    monkeypatch.setattr(
        catalog_resolver,
        "resolve_route",
        lambda *_args: pytest.fail("invalid date must not resolve a route"),
    )

    assert source.query("SELECT 1", [], date_yyyymmdd) == []


def test_a_share_minutes_and_trans_use_catalog_sources() -> None:
    client = TdxDuckDBClient()
    assert client._a_minutes_source("20221231") is client._a_minutes_source("20260710")
    assert client._a_trans_source("20190710") is client._a_trans_source("20260710")


@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_day_returns_rows():
    client = TdxDuckDBClient()
    rows = client.get_day("600519", limit=5)
    assert len(rows) > 0
    for key in ("date", "open", "close", "high", "low", "volume", "amount"):
        assert key in rows[0]


@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_wide_returns_rows():
    client = TdxDuckDBClient()
    rows = client.get_wide("600519", limit=5)
    assert len(rows) > 0
    for key in ("open", "last_close", "change_rate", "inner_volume", "outer_volume"):
        assert key in rows[0]


@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_xdxr_returns_rows_with_aliased_column():
    client = TdxDuckDBClient()
    rows = client.get_xdxr("600519", limit=5)
    assert len(rows) > 0
    assert "xingquanjia" in rows[0]  # 键名是 xingquanjia 不是 xingquanjiya
    # 已知限制：market_xdxr.xingquanjiya 全表都是 NULL（engine 侧写入 bug，
    # 见任务背景），这里断言值为 None 是记录现状，不是期望值——等 engine 那边
    # 修好后这一行断言要改成非 None，否则会一直"假装通过"掩盖数据已经修复的事实。
    assert rows[0]["xingquanjia"] is None


@pytest.mark.skipif(not os.path.exists(CATALOG_CURRENT), reason="本机没有已发布 route catalog")
def test_get_minutes_returns_price_volume_shape():
    client = TdxDuckDBClient()
    rows = client.get_minutes("600519", "20260706", limit=5)
    assert len(rows) > 0
    assert set(rows[0].keys()) == {"price", "volume"}


@pytest.mark.skipif(not os.path.exists(CATALOG_CURRENT), reason="本机没有已发布 route catalog")
def test_get_trans_returns_rows_with_expected_shape():
    client = TdxDuckDBClient()
    rows = client.get_trans("600519", "20260706", limit=10)
    assert len(rows) > 0
    for key in ("time", "price", "volume", "amount", "order_count", "direction"):
        assert key in rows[0]


@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_fund_daily_returns_dict_with_expected_keys():
    """get_fund_daily 契约：返回含 main_net/total_net/... 的 dict，对齐 EngineDataDiskClient。"""
    client = TdxDuckDBClient()
    result = client.get_fund_daily("600519", "2026-07-02")
    # 如果当天没有数据（市场休市或数据覆盖不含该日），返回 {} 也可以
    assert isinstance(result, dict)
    if result:
        for key in ("main_net", "total_net", "super_large_net", "large_net", "medium_net", "small_net"):
            assert key in result, f"缺少字段 {key}"


@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_fund_range_returns_dataframe_with_date_and_main_net_inflow():
    """get_fund_range 契约：返回含 ['date', 'main_net_inflow'] 两列的 DataFrame，
    对齐 EngineDataDiskClient.get_fund_range 的最小契约。
    """
    import polars as pl

    client = TdxDuckDBClient()
    df = client.get_fund_range("600519", "2026-06-01", "2026-07-02")
    assert isinstance(df, pl.DataFrame)
    if df.height > 0:
        assert "date" in df.columns
        assert "main_net_inflow" in df.columns


@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_fund_daily_missing_code_returns_empty_dict():
    """不存在的代码（或当天无数据）应返回 {}，不应抛异常。"""
    client = TdxDuckDBClient()
    result = client.get_fund_daily("999999", "2026-07-02")
    assert result == {}


TDX_HK_PATH = "/Volumes/WD1/tdx-hk-web.duckdb"


@pytest.mark.skipif(not os.path.exists(TDX_HK_PATH), reason=f"本机没有 {TDX_HK_PATH}")
@pytest.mark.parametrize(
    ("code", "asset_type"),
    [("600519", None), ("00700", "hk")],
)
def test_get_wide_volume_is_in_shares_for_both_markets(code, asset_type):
    """volume 的对外口径统一是股数，A股和港股必须一致。

    港股走 _get_hk_day -> market_day_kline.volume，而那一列存的是「手」
    (hk00700 2025-10-20 = 1,496，真实股数 ≈ 1,494 万)，A股走
    market_wide_kline.volume 存的是股数。港股必须 ×10000 补回来，否则港股
    成交量比真实值小 1 万倍，且与 A股口径不一致(下游 enriched 的量比/换手率
    等全部算错)。

    判据用 amount/[high, low] 这个数学上严格成立的区间(VWAP 必落在当日最高
    最低价之间)，不依赖任何 volume 列——那一列本身就是不可信的那个。
    """
    client = TdxDuckDBClient()
    rows = client.get_wide(code, limit=30, asset_type=asset_type)
    if not rows:
        pytest.skip(f"{code} 无日线数据")

    checked = 0
    for r in rows:
        amount, high, low, volume = r.get("amount"), r.get("high"), r.get("low"), r.get("volume")
        if not all(isinstance(x, (int, float)) and x for x in (amount, high, low, volume)):
            continue
        min_shares = amount / high   # 全按最高价成交 -> 股数下界
        max_shares = amount / low    # 全按最低价成交 -> 股数上界
        assert min_shares <= volume <= max_shares, (
            f"{code} {r.get('date')}: volume={volume:,.0f} 超出 amount/[high,low] "
            f"推出的股数区间 [{min_shares:,.0f}, {max_shares:,.0f}] —— volume 单位错了"
        )
        checked += 1

    if checked == 0:
        pytest.skip(f"{code} 无可校验的完整行")
