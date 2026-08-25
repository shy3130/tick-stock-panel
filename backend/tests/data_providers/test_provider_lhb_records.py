"""FQuantProvider.get_lhb_records 单元测试。

纯单元测试：注入伪造的 _fstore，不依赖 /Volumes 实体数据。
契约：返回 (symbol, trade_date) 去重对，code → 带后缀 symbol；
查询失败 fail-soft 空 df。
"""
from datetime import date

import polars as pl

from app.data_providers.fquant_provider import FQuantProvider


class _FakeFStore:
    def __init__(self, rows=None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.calls: list[tuple[str, list | None]] = []

    def query(self, sql: str, params=None):
        self.calls.append((sql, params))
        if self.error is not None:
            raise self.error
        return self.rows


def test_lhb_records_maps_code_to_symbol():
    fake = _FakeFStore(
        [
            {"code": "600519", "trade_date": date(2026, 8, 14)},
            {"code": "000001", "trade_date": date(2026, 8, 13)},
            {"code": "830799", "trade_date": date(2026, 8, 13)},
        ]
    )
    provider = FQuantProvider()
    provider._fstore = fake

    df = provider.get_lhb_records(date(2026, 8, 1), date(2026, 8, 14))

    assert df.columns == ["trade_date", "symbol"]
    assert set(df["symbol"].to_list()) == {"600519.SH", "000001.SZ", "830799.BJ"}
    # 日期区间参数透传给 fstore 客户端
    sql, params = fake.calls[0]
    assert "longhb_detail" in sql
    assert params == ["2026-08-01", "2026-08-14"]

    provider.close()


def test_lhb_records_no_rows_returns_empty_df():
    provider = FQuantProvider()
    provider._fstore = _FakeFStore([])

    df = provider.get_lhb_records(date(2026, 8, 1), date(2026, 8, 14))
    assert df.is_empty()

    provider.close()


def test_lhb_records_fail_soft_on_error():
    provider = FQuantProvider()
    provider._fstore = _FakeFStore(error=RuntimeError("duckdb down"))

    df = provider.get_lhb_records(date(2026, 8, 1), date(2026, 8, 14))
    assert df.is_empty()

    provider.close()


def test_lhb_institution_records_aggregate_by_symbol_and_date():
    fake = _FakeFStore(
        [
            {
                "code": "600519",
                "trade_date": date(2026, 8, 14),
                "net_buy_amount": 123_000_000.0,
            },
            {
                "code": "000001",
                "trade_date": date(2026, 8, 13),
                "net_buy_amount": -5_000_000.0,
            },
        ]
    )
    provider = FQuantProvider()
    provider._fstore = fake

    df = provider.get_lhb_institution_records(date(2026, 8, 1), date(2026, 8, 14))

    rows = {row["symbol"]: row for row in df.to_dicts()}
    assert rows["600519.SH"]["net_buy_amount"] == 123_000_000.0
    assert rows["000001.SZ"]["trade_date"] == date(2026, 8, 13)
    sql, params = fake.calls[0]
    assert "longhb_jigou" in sql
    assert "SUM(net_buy_amount)" in sql
    assert params == ["2026-08-01", "2026-08-14"]
    provider.close()


def test_margin_records_map_financing_balance_and_net_buy():
    fake = _FakeFStore(
        [
            {
                "code": "600519",
                "trade_date": date(2026, 8, 13),
                "financing_balance": 478_002.9952,
                "financing_net_buy": 368.527,
            }
        ]
    )
    provider = FQuantProvider()
    provider._fstore = fake

    df = provider.get_margin_records(date(2026, 8, 1), date(2026, 8, 13))

    assert df.to_dicts() == [
        {
            "trade_date": date(2026, 8, 13),
            "financing_balance": 478_002.9952,
            "financing_net_buy": 368.527,
            "symbol": "600519.SH",
        }
    ]
    sql, params = fake.calls[0]
    assert "stock_rzrj" in sql
    assert "buy_balance AS financing_balance" in sql
    assert "buy_net_amount AS financing_net_buy" in sql
    assert params == ["2026-08-01", "2026-08-13"]
    provider.close()
