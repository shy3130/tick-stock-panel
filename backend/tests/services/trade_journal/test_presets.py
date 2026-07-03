from app.services.trade_journal.presets import THS_PRESET, guess_mapping


def test_ths_preset_covers_required_fields():
    required = {"date", "code", "category", "qty", "price", "amount"}
    assert required <= set(THS_PRESET["mapping"].values())
    assert THS_PRESET["sheet"] == "交易记录"


def test_guess_mapping_exact_ths_columns():
    cols = ["成交日期", "成交时间", "代码", "名称", "交易类别", "成交数量", "成交价格", "发生金额", "成交金额", "费用", "备注"]
    m = guess_mapping(cols)
    assert m["成交日期"] == "date"
    assert m["交易类别"] == "category"
    assert m["发生金额"] == "amount"
    assert m["费用"] == "fee"
    assert "备注" not in m


def test_guess_mapping_generic_variants():
    m = guess_mapping(["交易日期", "证券代码", "操作", "成交量", "成交均价", "发生金额"])
    assert m["交易日期"] == "date"
    assert m["证券代码"] == "code"
    assert m["操作"] == "category"
    assert m["成交量"] == "qty"
