"""验证 get_realtime 全 universe 快路径与精确 symbols 路径。

纯单元测试：注入伪造的 _fstore 记录 SQL/参数，不依赖 /Volumes 实体数据。
- 全 universe 请求（A股/ETF/指数）不得构造数千个 code 的 IN 列表。
- 精确 symbols 请求继续走按 code IN 逻辑。
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
        # key: asset_type(int) -> rows(list[dict])
        self.universe_rows: dict[int, list[dict]] = {}
        # key: (asset_type, frozenset(codes)) -> rows(list[dict])
        self.code_rows: dict[tuple[int, frozenset], list[dict]] = {}

    def query(self, sql: str, params=None):
        self.records.append((sql, tuple(params or ())))
        # 区分 universe 快路径（params 只含一个 asset_type）与 code 路径（含多个）。
        if len(params) == 1:
            return self.universe_rows.get(params[0], [])
        return self.code_rows.get((params[0], frozenset(params[1:])), [])


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
    provider._fstore = fake
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
# 全 universe 快路径：不构造大 IN 列表
# --------------------------------------------------------------------------- #
def test_universe_fast_path_no_large_in_clause():
    """CN_Equity_A 快路径：SQL 仅参数化 asset_type，单次调用 params 长度为 1。"""
    fake = _FakeFStore()
    fake.universe_rows[1] = [_daily_markets_row("600519"), _daily_markets_row("000001")]
    provider = _new_provider(fake)

    df = provider.get_realtime(universes=["CN_Equity_A"])

    assert not df.is_empty()
    # 唯一一次 _fstore.query 必须只带一个参数（asset_type），没有成千个 code 占位符。
    assert len(fake.records) == 1
    sql, params = fake.records[0]
    assert len(params) == 1
    assert params[0] == 1
    # provider 写的是 %s 占位符（%s->? 的替换只发生在真正的 FStoreDuckDBClient.query，
    # 注入的 fake 收到的是原始 %s）。快路径只有一个参数位，且不含 code IN 子句。
    assert sql.count("%s") == 1
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
    """多 universe 合并请求：每个 asset_type 各一次参数化查询，无大 IN。"""
    fake = _FakeFStore()
    fake.universe_rows[1] = [_daily_markets_row("600519")]
    fake.universe_rows[10] = [_daily_markets_row("000001")]
    provider = _new_provider(fake)

    df = provider.get_realtime(universes=["CN_Equity_A", "CN_Index"])

    assert len(df) == 2
    assert len(fake.records) == 2
    for _sql, params in fake.records:
        assert len(params) == 1
    asset_types_queried = {params[0] for _sql, params in fake.records}
    assert asset_types_queried == {1, 10}


def test_universe_fast_path_uses_qualify_row_number_not_in():
    """快路径 SQL 用 QUALIFY/ROW_NUMBER 取最新行，而非 code IN。"""
    fake = _FakeFStore()
    fake.universe_rows[20] = [_daily_markets_row("510050")]
    provider = _new_provider(fake)

    provider.get_realtime(universes=["CN_ETF"])

    sql, _ = fake.records[0]
    assert "QUALIFY" in sql.upper()
    assert "ROW_NUMBER()" in sql.upper()
    assert "code IN" not in sql


def test_universe_empty_returns_empty_df_without_query():
    """未知 universe 或空列表：不查询，返回空 DataFrame。"""
    fake = _FakeFStore()
    provider = _new_provider(fake)

    assert provider.get_realtime(universes=[]).is_empty()
    assert provider.get_realtime(universes=["unknown"]).is_empty()
    assert fake.records == []


# --------------------------------------------------------------------------- #
# 精确 symbols 路径：保留按 code IN 逻辑
# --------------------------------------------------------------------------- #
def test_symbols_path_still_uses_code_in():
    """精确 symbols 请求继续走按 code IN，params 含 asset_type + 各 code。"""
    fake = _FakeFStore()
    fake.code_rows[(1, frozenset({"600519"}))] = [_daily_markets_row("600519")]
    provider = _new_provider(fake)

    df = provider.get_realtime(symbols=["600519.SH"])

    assert not df.is_empty()
    assert len(fake.records) == 1
    sql, params = fake.records[0]
    assert "code IN" in sql
    assert params[0] == 1
    assert "600519" in params


def test_symbols_and_universes_mutually_exclusive():
    fake = _FakeFStore()
    provider = _new_provider(fake)

    with pytest.raises(ValueError):
        provider.get_realtime(universes=["CN_Equity_A"], symbols=["600519.SH"])


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
