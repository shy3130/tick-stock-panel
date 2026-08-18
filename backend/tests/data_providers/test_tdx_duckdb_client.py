"""TdxDuckDBClient 完整契约测试。"""
from __future__ import annotations

import os
import datetime

import pytest

from app.data_providers.fquant import catalog_resolver
from app.data_providers.fquant.lease import ConnectionSet
from app.data_providers.fquant.tdx_duckdb_client import (
    TdxDuckDBClient,
    _CatalogSource,
    _LeasedSource,
    _a_share_wide_volume,
    _prefixed_code,
)

TDX_PATH = "/Volumes/WD1/duckdb/tdx.duckdb"
CATALOG_CURRENT = os.path.join(
    os.getenv("FQUANT_SNAPSHOT_ROOT_CATALOG", "/Volumes/WD1/duckdb/snapshots/catalog"),
    "current.json",
)


def test_prefixed_code():
    assert _prefixed_code("600519") == "sh600519"
    assert _prefixed_code("000001") == "sz000001"
    assert _prefixed_code("300059") == "sz300059"
    assert _prefixed_code("830799") == "bj830799"
    assert _prefixed_code("000001", "index") == "sh000001"
    assert _prefixed_code("399001", "index") == "sz399001"
    assert _prefixed_code("000001", "stock") == "sz000001"



def test_leased_and_catalog_sources_use_duckdb_factory(monkeypatch):
    """两个 ConnectionSet 工厂都必须走 connect_duckdb，以继承全局内存/线程预算。"""
    seen: list[tuple] = []

    def fake_connect(path, *, read_only=False):
        seen.append((path, read_only))
        return _FakeConnection(path)

    import app.storage.duckdb_runtime as rt

    monkeypatch.setattr(rt, "connect_duckdb", fake_connect)
    monkeypatch.setattr(_LeasedSource, "_resolve", lambda self: self._raw_path)

    leased = _LeasedSource("tdx", "/tmp/tdx.duckdb")
    with leased.lease() as conn:
        assert conn is not None
    assert seen == [("/tmp/tdx.duckdb", True)]

    catalog = _CatalogSource("tdx_minutes", "a")
    catalog._set = None
    monkeypatch.setattr(catalog_resolver, "resolve_route", lambda *_a: "/snap/x.duckdb")
    assert catalog.query("SELECT 1", [], "20260710") == [("/snap/x.duckdb",)]
    assert seen[-1] == ("/snap/x.duckdb", True)


def test_tdx_client_close_is_idempotent():
    """close() 关闭所有子源且幂等（不抛、可重复调用）。"""
    client = TdxDuckDBClient()
    client.close()
    client.close()
    # 子源 ConnectionSet 均为 None（未打开），不应抛错。



def test_moneyflow_daily_snapshot_uses_strict_source_and_exact_date():
    client = TdxDuckDBClient()
    calls: list[tuple[str, list, str]] = []

    def query_dicts(sql: str, params: list, caller: str) -> list[dict]:
        calls.append((sql, params, caller))
        return [{"code": "sh600001", "trade_date": datetime.date(2026, 8, 14)}]

    client._moneyflow_strict.query_dicts = query_dicts
    rows = client.get_moneyflow_daily_snapshot("2026-08-14")

    assert rows == [{"code": "sh600001", "trade_date": datetime.date(2026, 8, 14)}]
    sql, params, caller = calls[0]
    assert "FROM moneyflow_daily_stock" in sql
    assert "WHERE trade_date = ?" in sql
    assert params == ["2026-08-14"]
    assert caller == "get_moneyflow_daily_snapshot"
    assert client._moneyflow_strict._strict_snapshot is True
    client.close()
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


def test_catalog_source_raises_on_stale_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """陈旧目录必须抛出去,不能吞成空结果。

    吞成 [] 的话,"快照还没发布出来" 和 "这天休市/这只票没成交" 在前端完全无法区分——
    而每天 publish.engine.a 到 publish.catalog 之间都会短暂命中这个状态。
    """
    def fail(*_args: object) -> str:
        raise catalog_resolver.StaleCatalogError("stale")

    monkeypatch.setattr(catalog_resolver, "resolve_route", fail)
    source = _CatalogSource("tdx_minutes", "a")
    source._set = ConnectionSet(lambda _path: pytest.fail("must not open a raw database"))

    with pytest.raises(catalog_resolver.StaleCatalogError):
        source.query("SELECT 1", [], "20260710")


def test_catalog_source_raises_on_route_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """目录 route not found (包括中间日缺失) 现在原样抛 RouteNotFoundError。
    这与 StaleCatalogError 一致, 满足分钟契约 fail-closed, 避免 silent empty。
    上层可按需 soft-fail (如 fetch_minute_single)。
    """
    def fail(*_args: object) -> str:
        raise catalog_resolver.RouteNotFoundError("no route")

    monkeypatch.setattr(catalog_resolver, "resolve_route", fail)
    source = _CatalogSource("tdx_minutes", "a")
    source._set = ConnectionSet(lambda _path: pytest.fail("must not open a raw database"))

    with pytest.raises(catalog_resolver.RouteNotFoundError):
        source.query("SELECT 1", [], "20260710")


def test_catalog_source_raises_on_duckdb_query_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingConnection(_FakeConnection):
        def execute(self, _sql: str, _params: list[object]) -> _FakeConnection:
            raise RuntimeError("broken duckdb")

    monkeypatch.setattr(catalog_resolver, "resolve_route", lambda *_args: "/snapshots/current.duckdb")
    source = _CatalogSource("tdx_minutes", "a")
    source._set = ConnectionSet(lambda path: FailingConnection(path))

    with pytest.raises(catalog_resolver.CatalogError, match="catalog query failed"):
        source.query("SELECT 1", [], "20260710")


def test_catalog_source_raises_when_duckdb_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _CatalogSource("tdx_minutes", "a")
    monkeypatch.setattr(source, "_ensure_set", lambda: None)

    with pytest.raises(catalog_resolver.CatalogError, match="duckdb module is unavailable"):
        source.query("SELECT 1", [], "20260710")


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
    assert not hasattr(client, "_minutes")
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
    try:
        rows = client.get_minutes("600519", "20260706", limit=5)
    except catalog_resolver.CatalogError as exc:
        pytest.skip(f"published minutes route unavailable: {exc}")
    assert len(rows) > 0
    assert set(rows[0].keys()) == {"price", "volume"}


def test_get_minutes_uses_index_exchange_prefix():
    client = TdxDuckDBClient()
    seen: list[tuple[list[object], str]] = []

    def query(_sql, params, date_yyyymmdd):
        seen.append((params, date_yyyymmdd))
        return [(3999.0, 12)]

    client._a_catalog_minutes.query = query

    assert client.get_minutes(
        "000001", "20260810", limit=5, asset_type="index",
    ) == [{"price": 3999.0, "volume": 12}]
    assert seen == [(["sh000001", "2026-08-10", "minutes", 5], "20260810")]


@pytest.mark.skipif(not os.path.exists(CATALOG_CURRENT), reason="本机没有已发布 route catalog")
def test_get_trans_returns_rows_with_expected_shape():
    client = TdxDuckDBClient()
    try:
        rows = client.get_trans("600519", "20260706", limit=10)
    except catalog_resolver.CatalogError as exc:
        pytest.skip(f"published trans route unavailable: {exc}")
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


TDX_HK_PATH = "/Volumes/WD1/duckdb/tdx-hk.duckdb"


@pytest.mark.skipif(not os.path.exists(TDX_HK_PATH), reason=f"本机没有 {TDX_HK_PATH}")
@pytest.mark.parametrize(
    ("code", "asset_type"),
    [("600519", None), ("00700", "hk")],
)
def test_get_wide_volume_is_in_shares_for_both_markets(code, asset_type):
    """volume 的对外口径统一是股数，A股和港股必须一致。

    A 股：market_wide_kline.volume 多数日是股数，但存在「部分导入」异常日（成交额
    正确、volume 仅为真实值若干成，sh600519 2026-07-14=31%、07-15=61%），直接透传
    会违反股数契约；get_wide 已改 LEFT JOIN market_day_kline(dataset='day')，以官方
    日线 volume 为权威股数（见 _a_share_wide_volume）。港股：market_day_kline.volume
    存「手」，_get_hk_day 做 ×10000 补成股数。两侧对外都是股数——下游 enriched 的
    量比/换手率才一致。

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


# --- A 股 wide volume 口径归一：纯单元测试（不依赖本机 DuckDB 挂载） ---


def test_a_share_wide_volume_prefers_authoritative_day_volume():
    """day_kline.volume 是官方日线权威股数；存在时必须用它，而非可能部分导入的 wide.volume。

    复现值取自实测：sh600519 2026-07-15 wide.volume=4,367,600（只有真实值的 61%），
    market_day_kline.volume=7,194,300（落在 amount/[high,low] 推出的股数区间内）。
    """
    assert _a_share_wide_volume(wide_volume=4_367_600, day_volume=7_194_300) == 7_194_300
    # day 为 0（真实零成交日）也是有效权威值，应采用而非回退到 wide
    assert _a_share_wide_volume(wide_volume=1_000, day_volume=0) == 0


def test_a_share_wide_volume_falls_back_to_wide_when_day_missing():
    """LEFT JOIN 未命中（该日 day_kline 无行）时回退到 wide.volume——仍是股，
    仅个别导入异常日不准，不能丢成 None。"""
    assert _a_share_wide_volume(wide_volume=4_199_200, day_volume=None) == 4_199_200
    # 两者都缺失才为 None
    assert _a_share_wide_volume(wide_volume=None, day_volume=None) is None


def test_get_wide_a_share_picks_authoritative_volume_without_mount(monkeypatch: pytest.MonkeyPatch):
    """无本机挂载也能校验：get_wide 把查询结果的 day_volume 列(r[21])作为 A 股 volume
    输出，而不是直接透传可能部分导入的 wide.volume(r[5])。同时确认 SQL 真的 JOIN 了
    market_day_kline(dataset='day')——归一的根因落在查询里。"""
    client = TdxDuckDBClient()
    captured: dict = {}

    def fake_query(sql, params, label=""):
        captured["sql"] = sql
        captured["params"] = params
        # 22 列：21 个 wide 字段 + day_volume(r[21])
        return [(
            datetime.date(2026, 7, 15),        # r[0] trade_date
            1203.66, 1251.06, 1256.6, 1198.66,  # r[1..4] open close high low
            4_367_600,                          # r[5] wide.volume（部分导入，仅 61%）
            8_922_861_568.0,                    # r[6] amount
            1, 2,                               # r[7..8] up down
            1214.88, 2.98,                      # r[9..10] last_close change_rate
            0.0, 0.0, 0.0,                      # r[11..13] open_volume open_turnz open_unmatched
            35_500.0, 0.0, 0.0,                 # r[14..16] close_volume close_turnz close_unmatched
            1_736_000.0, 2_631_600.0,           # r[17..18] inner_volume outer_volume
            2_166_640_698.0, 3_286_495_727.0,   # r[19..20] inner_amount outer_amount
            7_194_300,                          # r[21] day_volume（权威股数）
        )]

    monkeypatch.setattr(client._tdx, "query", fake_query)
    rows = client.get_wide("600519", limit=5)
    assert rows, "get_wide 应返回行"
    assert rows[0]["volume"] == 7_194_300, (
        "A 股 volume 必须取权威 day_volume(7,194,300)，而非部分导入的 wide.volume(4,367,600)"
    )
    assert rows[0]["amount"] == 8_922_861_568.0
    # 归一根因落在查询里：确认 JOIN 了权威日线源
    assert "market_day_kline" in captured["sql"]
    assert "dataset = 'day'" in captured["sql"]
    assert captured["params"][:2] == ["sh600519", "sh600519"]
