import polars as pl

from app.services.trade_journal.parser import normalize_code, normalize_rows, read_upload
from app.services.trade_journal.presets import THS_PRESET

THS_COLS = ["成交日期", "成交时间", "代码", "名称", "交易类别", "成交数量", "成交价格", "发生金额", "成交金额", "费用", "备注"]

ROWS = [
    ["2024-02-01", "", "", "", "银行转证券", 0, 0, 13000, None, None, ""],
    ["2024-02-05", "14:53:08", "601127", "赛力斯", "买入", 200, 56.1, -11221.23, 11220, 1.23, ""],
    ["2024-02-06", "14:54:53", "601127", "赛力斯", "卖出", 200, 61.71, 12334.48, 12342, 7.52, ""],
    ["2025-06-20", "10:00:00", "02577", "英诺赛科", "买入", 200, 72.7, -14542.6, 14540, 2.6, ""],
    ["2024-07-18", "16:00:00", "600418", "江淮汽车", "除权除息", None, None, 42, 42, None, ""],
    ["2024-07-23", "09:00:00", "600418", "江淮汽车", "股息个税征收", None, None, -8.4, None, None, ""],
    ["2024-03-05", "", "204001", "GC001", "融券回购", 0, 0, -50000, None, None, ""],
]


def _df():
    return pl.DataFrame([dict(zip(THS_COLS, row, strict=False)) for row in ROWS])


def test_normalize_code():
    assert normalize_code("601127") == "601127.SH"
    assert normalize_code("000988") == "000988.SZ"
    assert normalize_code("300433") == "300433.SZ"
    assert normalize_code("688347") == "688347.SH"
    assert normalize_code("830799") == "830799.BJ"
    assert normalize_code("02577") == "02577.HK"


def test_normalize_rows_splits_fills_and_cash_events():
    fills, events, warnings = normalize_rows(_df(), THS_PRESET["mapping"])
    assert len(fills) == 3
    assert (fills[0].symbol, fills[0].side, fills[0].qty, fills[0].amount) == ("601127.SH", "buy", 200.0, -11221.23)
    assert fills[2].symbol == "02577.HK"
    assert sorted(ev.kind for ev in events) == ["dividend", "dividend_tax", "repo", "transfer_in"]
    assert warnings == []


def test_normalize_rows_warns_on_unknown_category():
    df = pl.DataFrame([dict(zip(THS_COLS, ["2024-01-01", "", "600000", "浦发银行", "担保品划入", 100, 8.0, -800, None, None, ""], strict=False))])
    fills, events, warnings = normalize_rows(df, THS_PRESET["mapping"])
    assert fills == [] and len(events) == 1 and events[0].kind == "other"
    assert len(warnings) == 1 and "担保品划入" in warnings[0]


def test_read_upload_csv():
    csv = "成交日期,代码,交易类别,成交数量,成交价格,发生金额\n2024-02-05,601127,买入,200,56.1,-11221.23\n"
    sheets, df = read_upload(csv.encode(), "a.csv", sheet=None)
    assert sheets == []
    assert df.height == 1
