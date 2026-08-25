"""验证 get_realtime 全 universe 快路径与精确 symbols 路径。

纯单元测试：注入伪造的 _fstore_markets 记录 SQL/参数，不依赖 /Volumes 实体数据。
- 全 universe 请求（A股/ETF/指数）先取 daily_markets 全局最新 trade_date，
  再按 asset_type 查该日全表，不构造数千个 code 的 IN 列表。
- 精确 symbols 请求同样按全局最新 trade_date + asset_type + code IN 查询。
- 两条路径都不用 QUALIFY/ROW_NUMBER/DISTINCT ON 扫历史取每 code 最新。
- realtime 查询走独立 _fstore_markets 客户端，不与主 _fstore 共享锁。
- 两条路径输出都保持 realtime shape（经 _fstore_quote_to_row / normalize_realtime）。
"""
from __future__ import annotations

import os

import pytest

from app.data_providers.fquant_provider import FQuantProvider

DUCKDB_PATH = "/Volumes/WD1/duckdb/fstore.duckdb"

pytestmark_integration = pytest.mark.skipif(
    not os.path.exists(DUCKDB_PATH),
    reason=f"本机没有挂载 {DUCKDB_PATH}",
)


class _FakeFStore:
    """记录 query(sql, params) 调用，按预置行返回。断言用 records。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, tuple]] = []
        # 全局最新 trade_date（MAX 查询返回值）
        self.latest_trade_date = "2026-08-07"
        # universe 数据：key asset_type(int) -> rows(list[dict])
        self.universe_rows: dict[int, list[dict]] = {}
        # symbols 数据：key (asset_type, frozenset(codes)) -> rows(list[dict])
        self.code_rows: dict[tuple[int, frozenset], list[dict]] = {}

    def query(self, sql: str, params=None):
        params = tuple(params or ())
        self.records.append((sql, params))
        up = sql.upper()
        # 全局最新 trade_date 探针：SELECT MAX(trade_date) AS latest ...
        if "MAX(" in up and "TRADE_DATE" in up:
            return [{"latest": self.latest_trade_date}]
        # 数据查询：params = (asset_type, latest_date[, codes...])
        asset_type = params[0]
        codes = params[2:]
        if not codes:
            return self.universe_rows.get(asset_type, [])
        return self.code_rows.get((asset_type, frozenset(codes)), [])


def _data_queries(fake: _FakeFStore) -> list[tuple[str, tuple]]:
    """从 fake.records 过滤掉 MAX(trade_date) 探针，只保留数据查询。"""
    return [(s, p) for s, p in fake.records if "MAX(" not in s.upper()]


def _daily_markets_row(code: str) -> dict:
    """构造一条与 _query_fstore_realtime_* 抽取字段对齐的原始行。"""
    return {
        "code": code,
        "name": f"n{code}",
        "tdate": "2026-08-07",
        "price": "10.00",
        "zdfd": "1.00",
        "zded": "0.10",
        "cjl": "100",
        "cje": "1000.00",
        "jrkpj": "9.90",
        "zgj": "10.20",
        "zdj": "9.80",
        "zrspj": "9.90",
        "hslv": "0.5",
        "zhfu": "4.04",
    }


def _new_provider(fake: _FakeFStore) -> FQuantProvider:
    provider = FQuantProvider()
    provider._fstore_markets = fake
    return provider


# --------------------------------------------------------------------------- #
# universe 前缀 -> asset_type 映射
# --------------------------------------------------------------------------- #
def test_universe_prefix_to_asset_types():
    assert FQuantProvider._realtime_universe_asset_types(["CN_Equity_A"]) == [1]
    assert FQuantProvider._realtime_universe_asset_types(["CN_ETF"]) == [20]
    assert FQuantProvider._realtime_universe_asset_types(["CN_Index"]) == [10]
    assert FQuantProvider._realtime_universe_asset_types(
        ["cn_equity_all", "CN_ETF_CORE", "CN_Index"]
    ) == [1, 20, 10]
    assert FQuantProvider._realtime_universe_asset_types(["unknown"]) == []
    assert FQuantProvider._realtime_universe_asset_types([]) == []


# --------------------------------------------------------------------------- #
# 全 universe 快路径：先取全局最新 trade_date，再按 asset_type 查该日全表
# --------------------------------------------------------------------------- #
def test_universe_fast_path_no_large_in_clause():
    """CN_Equity_A 快路径：一次 MAX 探针 + 一次数据查询；数据查询参数为
    (asset_type, trade_date)，不含成千个 code 占位符。"""
    fake = _FakeFStore()
    fake.universe_rows[1] = [_daily_markets_row("600519"), _daily_markets_row("000001")]
    provider = _new_provider(fake)

    df = provider.get_realtime(universes=["CN_Equity_A"])

    assert not df.is_empty()
    data_qs = _data_queries(fake)
    # 恰好一次数据查询（另有一次 MAX 探针）。
    assert len(data_qs) == 1
    sql, params = data_qs[0]
    assert len(params) == 2
    assert params[0] == 1
    assert params[1] == "2026-08-07"
    # provider 写的是 %s 占位符（%s->? 替换只发生在真正的 FStoreDuckDBClient.query，
    # 注入的 fake 收到的是原始 %s）。数据查询只有两个参数位，且不含 code IN 子句。
    assert sql.count("%s") == 2
    assert " code IN " not in sql.upper()
    assert " IN (" not in sql.upper()


def test_universe_fast_path_output_shape_matches_realtime():
    """全 universe 路径输出经 normalize_realtime，列固定为 REALTIME_COLS。"""
    fake = _FakeFStore()
    fake.universe_rows[1] = [_daily_markets_row("600519")]
    provider = _new_provider(fake)

    df = provider.get_realtime(universes=["CN_EQUITY_A"])

    expected = {"symbol", "name", "last_price", "prev_close", "open", "high", "low",
                "volume", "amount", "timestamp", "source", "ext"}
    assert set(df.columns) == expected
    row = df.row(0, named=True)
    assert row["symbol"] == "600519.SH"
    assert row["last_price"] == 10.0
    assert row["source"] == "fquant:fstore:daily_markets"
    assert "change_pct" in row["ext"]


def test_universe_multi_asset_types_one_query_each():
    """多 universe 合并请求：一次 MAX 探针 + 每个 asset_type 各一次数据查询。"""
    fake = _FakeFStore()
    fake.universe_rows[1] = [_daily_markets_row("600519")]
    fake.universe_rows[10] = [_daily_markets_row("000001")]
    provider = _new_provider(fake)

    df = provider.get_realtime(universes=["CN_Equity_A", "CN_Index"])

    assert len(df) == 2
    data_qs = _data_queries(fake)
    assert len(data_qs) == 2
    for _sql, params in data_qs:
        assert len(params) == 2
    asset_types_queried = {params[0] for _sql, params in data_qs}
    assert asset_types_queried == {1, 10}


def test_universe_fast_path_no_qualify_no_row_number():
    """快路径 SQL 禁止 QUALIFY/ROW_NUMBER/DISTINCT ON；改为 trade_date = 点查。"""
    fake = _FakeFStore()
    fake.universe_rows[20] = [_daily_markets_row("510050")]
    provider = _new_provider(fake)

    provider.get_realtime(universes=["CN_ETF"])

    sql, _ = _data_queries(fake)[0]
    up = sql.upper()
    assert "QUALIFY" not in up
    assert "ROW_NUMBER()" not in up
    assert "DISTINCT ON" not in up
    assert "code IN" not in sql
    # 新方式：按确定的 trade_date 点查当前交易日。
    assert "trade_date = %s" in sql


def test_universe_empty_returns_empty_df_without_query():
    """未知 universe 或空列表：不查询，返回空 DataFrame。"""
    fake = _FakeFStore()
    provider = _new_provider(fake)

    assert provider.get_realtime(universes=[]).is_empty()
    assert provider.get_realtime(universes=["unknown"]).is_empty()
    assert fake.records == []


# --------------------------------------------------------------------------- #
# 精确 symbols 路径：全局最新 trade_date + asset_type + code IN
# --------------------------------------------------------------------------- #
def test_symbols_path_still_uses_code_in():
    """精确 symbols 请求按全局最新 trade_date + asset_type + code IN 查询。"""
    fake = _FakeFStore()
    fake.code_rows[(1, frozenset({"600519"}))] = [_daily_markets_row("600519")]
    provider = _new_provider(fake)

    df = provider.get_realtime(symbols=["600519.SH"])

    assert not df.is_empty()
    data_qs = _data_queries(fake)
    assert len(data_qs) == 1
    sql, params = data_qs[0]
    assert "code IN" in sql
    assert params[0] == 1
    assert params[1] == "2026-08-07"
    assert "600519" in params


def test_symbols_path_no_distinct_on_scans_history():
    """symbols 路径同样禁止 DISTINCT ON 扫历史，改为 trade_date = 点查。"""
    fake = _FakeFStore()
    fake.code_rows[(1, frozenset({"600519"}))] = [_daily_markets_row("600519")]
    provider = _new_provider(fake)

    provider.get_realtime(symbols=["600519.SH"])

    sql, _ = _data_queries(fake)[0]
    up = sql.upper()
    assert "DISTINCT ON" not in up
    assert "trade_date = %s" in sql


def test_symbols_and_universes_mutually_exclusive():
    fake = _FakeFStore()
    provider = _new_provider(fake)

    with pytest.raises(ValueError):
        provider.get_realtime(universes=["CN_Equity_A"], symbols=["600519.SH"])


# --------------------------------------------------------------------------- #
# 只读最新全局日期
# --------------------------------------------------------------------------- #
def test_realtime_probes_global_max_trade_date_readonly():
    """realtime 先发一次全局 MAX(trade_date) 查询，不带 asset_type 过滤。"""
    fake = _FakeFStore()
    fake.universe_rows[1] = [_daily_markets_row("600519")]
    provider = _new_provider(fake)

    provider.get_realtime(universes=["CN_Equity_A"])

    max_queries = [(s, p) for s, p in fake.records if "MAX(" in s.upper()]
    assert len(max_queries) == 1
    sql, params = max_queries[0]
    up = sql.upper()
    assert "MAX(TRADE_DATE)" in up
    assert "FROM DAILY_MARKETS" in up
    # 全局探针不参数化 asset_type（只读标量，无 WHERE）。
    assert params == ()


# --------------------------------------------------------------------------- #
# 旧标的不混入：只返回全局最新 trade_date 的行
# --------------------------------------------------------------------------- #
def test_realtime_old_codes_do_not_bleed_in():
    """全局最新 trade_date 上没有的 code 不出现在结果中。

    退市/停牌等只存在于旧日期的旧标的不会被扫历史取回——新逻辑只查当前交易日。
    fake 只给最新日期的行；一个旧 code 即使在请求范围内也不会出现，且数据查询
    锁定了全局最新 trade_date。
    """
    fake = _FakeFStore()
    fake.latest_trade_date = "2026-08-07"
    fake.universe_rows[1] = [_daily_markets_row("600519")]
    provider = _new_provider(fake)

    df = provider.get_realtime(universes=["CN_Equity_A"])

    symbols = set(df["symbol"].to_list())
    assert "600519.SH" in symbols
    assert "000002.SZ" not in symbols
    # 数据查询锁定了全局最新 trade_date。
    _sql, params = _data_queries(fake)[0]
    assert params[1] == "2026-08-07"


def test_symbols_request_old_code_absent_on_latest_date_returns_empty():
    """精确请求一个在最新 trade_date 无数据的旧 code → 不回退扫历史，返回空。"""
    fake = _FakeFStore()
    fake.latest_trade_date = "2026-08-07"
    # 不预置任何 code_rows：最新日期查不到 000002。
    provider = _new_provider(fake)

    df = provider.get_realtime(symbols=["000002.SZ"])

    assert df.is_empty()


# --------------------------------------------------------------------------- #
# 独立锁/客户端：realtime 走 _fstore_markets，不与主 _fstore 共享
# --------------------------------------------------------------------------- #
def test_realtime_uses_dedicated_markets_client():
    """realtime 查询走独立的 _fstore_markets 客户端，完全不触碰主 _fstore。"""
    markets_fake = _FakeFStore()
    markets_fake.universe_rows[1] = [_daily_markets_row("600519")]
    main_fake = _FakeFStore()
    provider = FQuantProvider()
    provider._fstore = main_fake
    provider._fstore_markets = markets_fake

    df = provider.get_realtime(universes=["CN_Equity_A"])

    assert not df.is_empty()
    # markets 客户端收到了查询（MAX 探针 + 数据查询）。
    assert len(markets_fake.records) >= 2
    # 主 fstore 客户端完全没被 realtime 触碰（独立锁/连接的可观察回归）。
    assert main_fake.records == []


def test_fstore_markets_is_separate_instance_from_fstore():
    """构造即保证 _fstore 与 _fstore_markets 是两个独立客户端实例（独立锁/连接）。"""
    provider = FQuantProvider()
    assert provider._fstore_markets is not provider._fstore


@pytestmark_integration
def test_fstore_realtime_duckdb_matches_postgres_shape():
    duckdb_provider = FQuantProvider()
    duckdb_rows = duckdb_provider._get_fstore_realtime(["600519.SH"])
    assert len(duckdb_rows) == 1
    row = duckdb_rows[0]
    assert row["symbol"] == "600519.SH"
    assert row["last_price"] > 0
    assert row["source"] == "fquant:fstore:daily_markets"
    assert "change_pct" in row["ext"]
