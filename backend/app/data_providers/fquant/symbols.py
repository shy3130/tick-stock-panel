"""符号归一工具（§5.1）。

三源异构代码口径归一为内部统一带后缀的 symbol：
- ``600519.SH`` / ``000001.SZ`` / ``00700.HK`` / ``000300.INDEX`` / ``510330.SH``

复用 PoC ``fquant_provider.py:71-114`` 的现成工具函数（符号归一零改动），
按 §5.1 全部表内不发明实现。
"""
from __future__ import annotations

# asset_type 数字 → 对外后缀（§5.1）
_SUFFIX_MAP: dict[int, str] = {
    1: "",      # A 股：交易所由 code 前缀推导
    3: "HK",
    10: "INDEX",
    20: "ETF",
}


def split_symbol(symbol: str) -> tuple[str, str]:
    """``600519.SH`` → ``("600519", "SH")``。无后缀时后缀为空串。

    与 PoC ``fquant_provider.py:71`` 完全一致。
    """
    if "." in symbol:
        code, _, suffix = symbol.rpartition(".")
        return code, suffix.upper()
    return symbol, ""


def code_to_symbol(code: str, asset_type_num: int = 1) -> str:
    """fstore/engine 的 ``(code, asset_type)`` → 对外带后缀符号。

    >>> code_to_symbol("600519", 1)
    '600519.SH'
    >>> code_to_symbol("000001", 1)
    '000001.SZ'
    >>> code_to_symbol("00700", 3)
    '00700.HK'
    >>> code_to_symbol("000300", 10)
    '000300.INDEX'
    >>> code_to_symbol("510330", 20)
    '510330.SH'
    >>> code_to_symbol("159915", 20)
    '159915.SZ'
    """
    code = str(code)
    if asset_type_num == 1:  # A 股：60/68/9(非92)上交所，0/3 深交所，8/4/92 北交所
        if code.startswith("92"):
            return f"{code}.BJ"
        if code.startswith(("60", "68", "9", "11", "13")):
            return f"{code}.SH"
        if code.startswith(("0", "30", "12")):
            return f"{code}.SZ"
        return f"{code}.BJ"
    if asset_type_num == 3:
        return f"{code}.HK"
    if asset_type_num == 10:
        return f"{code}.INDEX"
    if asset_type_num == 20:
        if code.startswith("5"):
            return f"{code}.SH"
        if code.startswith("1"):
            return f"{code}.SZ"
        return f"{code}.ETF"
    return code


def code_and_market_to_symbol(code: str, asset_type_num: int = 1) -> str:
    """``code_to_symbol`` 的别名，兼容 PoC 测试脚本调用名。"""
    return code_to_symbol(code, asset_type_num)


def symbol_to_code(symbol: str) -> str:
    """``600519.SH`` → ``600519``（纯数字，fstore/engine 通用）。"""
    code, _ = split_symbol(symbol)
    return code


def symbol_to_market(symbol: str) -> tuple[int, str] | None:
    """对外符号 → ``(asset_type_num, market_key)``。

    返回值保留给底层本地源客户端使用。
    指数当前未映射（返回 None）。

    >>> symbol_to_market("600519.SH")
    (1, 'a')
    >>> symbol_to_market("000001.SZ")
    (1, 'a')
    >>> symbol_to_market("00700.HK")
    (3, 'hk')
    >>> symbol_to_market("513050.SH")
    (20, 'etf')
    >>> symbol_to_market("000300.INDEX") is None
    True
    """
    _, suffix = split_symbol(symbol)
    if is_etf_symbol(symbol):
        return 20, "etf"
    if suffix in {"SH", "SZ", "BJ"}:
        return 1, "a"
    if suffix == "HK":
        return 3, "hk"
    return None


def is_a_stock(symbol: str) -> bool:
    """判断对外符号是否为 A 股。"""
    _, suffix = split_symbol(symbol)
    return suffix in {"SH", "SZ", "BJ"} and not is_etf_symbol(symbol)


def is_etf_symbol(symbol: str) -> bool:
    """判断对外符号是否为 ETF。"""
    code, suffix = split_symbol(symbol)
    return suffix == "ETF" or (suffix == "SH" and code.startswith("5")) or (suffix == "SZ" and code.startswith(("15", "16")))


def exchange_of(code: str) -> str:
    """从 code 前缀推导交易所（§4.3 归一映射）。

    - 60/68/9 → SH
    - 0/30/20 → SZ
    - 8/4 → BJ
    - 其它保持空串
    """
    code = str(code)
    if code.startswith(("8", "4", "92")):
        return "BJ"
    if code.startswith(("60", "68", "9", "11", "13")):
        return "SH"
    if code.startswith(("0", "30", "12", "20")):
        return "SZ"
    return ""


# fstore base_infos.asset_type 数字 → Provider 契约 AssetType 字符串
ASSET_TYPE_NUM_TO_STR: dict[int, str] = {
    1: "stock",
    3: "hk",
    10: "index",
    20: "etf",
}


def num_to_asset_type_str(num: int) -> str:
    """fstore ``base_infos.asset_type`` → 契约 ``AssetType``。"""
    return ASSET_TYPE_NUM_TO_STR.get(num, "stock")


def asset_type_str_to_nums(asset_type: str) -> list[int]:
    """契约 ``AssetType`` → fstore ``base_infos.asset_type`` 数字列表。

    >>> asset_type_str_to_nums("stock")
    [1]
    >>> asset_type_str_to_nums("index")
    [10]
    >>> asset_type_str_to_nums("etf")
    [20]
    >>> asset_type_str_to_nums("hk")
    [3]
    """
    mapping = {
        "stock": [1],
        "hk": [3],
        "index": [10],
        "etf": [20],
    }
    return mapping.get(asset_type, [1])

def canonical_index_symbol(symbol: str) -> str:
    """在明确的指数上下文，把任意输入形式规范成 ``{code}.INDEX``。

    持久化 canonical 指数 symbol 是 ``{code}.INDEX``。用户偏好、历史 API
    入参可能仍传 ``.SH`` / ``.SZ`` / ``.BJ`` 或纯 code；本函数在**单一读边界**
    把它们统一为 ``.INDEX``，绝不查询/优先命中旧后缀行。

    **仅在明确指数上下文调用** — 不猜测普通股票上下文。
    ``000001.SZ`` (平安银行) 若被误传入指数上下文，会变成 ``000001.INDEX``
    (上证指数)；调用方必须先做指数/股票分类 (如 ``_is_index_symbol``)。

    >>> canonical_index_symbol("000001")
    '000001.INDEX'
    >>> canonical_index_symbol("000001.SH")
    '000001.INDEX'
    >>> canonical_index_symbol("000001.INDEX")
    '000001.INDEX'
    >>> canonical_index_symbol("399001.SZ")
    '399001.INDEX'
    """
    code, _ = split_symbol(str(symbol).strip().upper())
    return f"{code}.INDEX"
