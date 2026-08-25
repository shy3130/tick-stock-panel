"""FQuantProvider.get_stock_reference_flags 单元测试。

纯单元测试：注入伪造的 _fstore，不依赖 /Volumes 实体数据。
契约：symbol/is_ah/ah_premium/hk_connect/listing_date；
ah 只取最新 trade_date 一行；24h 缓存；查询失败 fail-soft 空 df。
"""
from datetime import date

import polars as pl

from app.data_providers.fquant_provider import FQuantProvider


class _FakeFStore:
    def __init__(self, rows=None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.calls = 0

    def query(self, sql: str, params=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.rows


def _row(symbol: str, code: str, is_ah, ah_premium, hk_connect, listing_date):
    return {
        "symbol": symbol,
        "code": code,
        "is_ah": is_ah,
        "ah_premium": ah_premium,
        "hk_connect": hk_connect,
        "listing_date": listing_date,
    }


def test_reference_flags_shape_and_values():
    fake = _FakeFStore(
        [
            _row("600001.SH", "600001", True, 50.5, True, date(2020, 1, 1)),
            _row("000001.SZ", "000001", False, None, False, date(1991, 4, 3)),
        ]
    )
    provider = FQuantProvider()
    provider._fstore = fake

    df = provider.get_stock_reference_flags()
    by_symbol = {r["symbol"]: r for r in df.to_dicts()}
    assert by_symbol["600001.SH"]["is_ah"] is True
    assert df.columns == ["symbol", "is_ah", "ah_premium", "hk_connect", "listing_date"]
    assert by_symbol["600001.SH"]["hk_connect"] is True
    assert by_symbol["000001.SZ"]["is_ah"] is False
    assert by_symbol["000001.SZ"]["ah_premium"] is None

    # 24h 缓存：第二次调用不再触达 fstore
    provider.get_stock_reference_flags()
    assert fake.calls == 1

    provider.close()


def test_reference_flags_fail_soft_on_error():
    fake = _FakeFStore(error=RuntimeError("duckdb down"))
    provider = FQuantProvider()
    provider._fstore = fake

    df = provider.get_stock_reference_flags()
    assert df.is_empty()

    provider.close()


def test_reference_flags_dedup_by_symbol():
    fake = _FakeFStore(
        [
            _row("600001.SH", "600001", True, 50.5, True, date(2020, 1, 1)),
            _row("600001.SH", "600001", True, 51.0, True, date(2020, 1, 1)),
        ]
    )
    provider = FQuantProvider()
    provider._fstore = fake

    df = provider.get_stock_reference_flags()
    assert df.height == 1

    provider.close()
