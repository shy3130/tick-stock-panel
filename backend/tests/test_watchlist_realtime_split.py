"""Free 档自选实时资产分流测试。"""
from app.services.quote_service import QuoteService


def test_split_records_by_asset():
    records = [
        {"symbol": "600000.SH"}, {"symbol": "510300.SH"}, {"symbol": "000001.SH"},
    ]
    index, etf, stock = QuoteService._split_records_by_asset(
        records, {"000001.SH"}, {"510300.SH"},
    )
    assert [r["symbol"] for r in index] == ["000001.SH"]
    assert [r["symbol"] for r in etf] == ["510300.SH"]
    assert [r["symbol"] for r in stock] == ["600000.SH"]
    # etf 优先于 index (与 resolve_asset_type 判定顺序一致)
    index2, etf2, stock2 = QuoteService._split_records_by_asset(
        [{"symbol": "X"}], {"X"}, {"X"},
    )
    assert etf2 and not index2 and not stock2
