"""列映射预设。"""
from __future__ import annotations

CANONICAL_FIELDS = ("date", "time", "code", "name", "category", "qty", "price", "amount", "fee")

THS_PRESET = {
    "id": "ths_journal",
    "label": "同花顺投资账本",
    "sheet": "交易记录",
    "mapping": {
        "成交日期": "date",
        "成交时间": "time",
        "代码": "code",
        "名称": "name",
        "交易类别": "category",
        "成交数量": "qty",
        "成交价格": "price",
        "发生金额": "amount",
        "费用": "fee",
    },
}

PRESETS = [THS_PRESET]

_GUESS: dict[str, str] = {}
for _syns, _field in [
    (("成交日期", "交易日期", "日期", "委托日期"), "date"),
    (("成交时间", "时间", "委托时间"), "time"),
    (("代码", "证券代码", "股票代码"), "code"),
    (("名称", "证券名称", "股票名称"), "name"),
    (("交易类别", "操作", "业务名称", "买卖标志", "委托类别"), "category"),
    (("成交数量", "成交量", "数量", "成交股数"), "qty"),
    (("成交价格", "成交均价", "价格", "成交价"), "price"),
    (("发生金额", "清算金额", "资金发生数"), "amount"),
    (("费用", "手续费", "佣金"), "fee"),
]:
    for _s in _syns:
        _GUESS[_s] = _field


def guess_mapping(columns: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    used: set[str] = set()
    for col in columns:
        field = _GUESS.get(str(col).strip())
        if field and field not in used:
            out[str(col)] = field
            used.add(field)
    return out
