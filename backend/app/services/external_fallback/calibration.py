"""口径校准与验证 — 符号 / 单位 / 时区 pinning (受控 fallback 契约 §4.5)。

本模块对适配层输出的每条外部行情行做 defensive validation:
  - symbol 合法性 (SH/SZ/BJ/HK + 指数/个股/ETF code 形状)
  - 单位口径: volume=股 (正数), amount=元 (正数), change_pct=百分点
  - timestamp: Asia/Shanghai 时区 ISO 字符串, 非空

校准失败行被丢弃 (不抛异常, 保证批量部分成功可用); 连续校准失败由
CircuitBreaker 在适配层统计 (契约: 连续 3 次口径校验失败 → 熔断)。

本模块不执行网络调用。
"""
from __future__ import annotations

import re

_SHANGHAI_TZ_SUFFIX = "+08:00"

# symbol 形状: <digits>.<SH|SZ|BJ|HK>; 指数 000xxx.SH / 399xxx.SZ 同形。
_SYMBOL_RE = re.compile(r"^\d{4,6}\.(SH|SZ|BJ|HK)$")


def is_valid_symbol(symbol: str) -> bool:
    """symbol 是否符合内部规范化形状。"""
    return bool(symbol and _SYMBOL_RE.match(str(symbol).strip().upper()))


def validate_row(row: dict) -> bool:
    """单条外部行情行口径验证。通过返回 True, 不通过返回 False (行应被丢弃)。

    不抛异常; 校验项:
      - symbol 合法
      - last_price 为正数
      - volume 若存在则 ≥ 0 (单位: 股)
      - amount 若存在则 ≥ 0 (单位: 元)
      - change_pct 若存在则有限 (单位: 百分点, 例如 1.23 = 1.23%)
      - timestamp 为 Asia/Shanghai ISO 字符串 (含 +08:00)
    """
    symbol = row.get("symbol")
    if not is_valid_symbol(str(symbol)):
        return False
    last_price = row.get("last_price")
    if not _is_positive_number(last_price):
        return False
    volume = row.get("volume")
    if volume is not None and not _is_nonneg_number(volume):
        return False
    amount = row.get("amount")
    if amount is not None and not _is_nonneg_number(amount):
        return False
    change_pct = row.get("change_pct")
    if change_pct is not None and not _is_finite_number(change_pct):
        return False
    ts = row.get("timestamp")
    if not ts or not isinstance(ts, str) or _SHANGHAI_TZ_SUFFIX not in ts:
        return False
    return True


def filter_valid_rows(rows: list[dict]) -> list[dict]:
    """过滤出口径合法的行; 记录被丢弃数 (不记原始内容)。"""
    valid: list[dict] = []
    dropped = 0
    for r in rows:
        if validate_row(r):
            valid.append(r)
        else:
            dropped += 1
    return valid


def validate_depth_row(row: dict) -> bool:
    """单条五档 depth 行口径验证。通过返回 True, 不通过返回 False (行应被丢弃)。

    校验项:
      - symbol 合法
      - bid_prices/ask_prices: 各 5 元素 list, 元素为正数或 None
      - bid_volumes/ask_volumes: 各 5 元素 list, 元素为非负 int 或 None
        (0 有效 = 封单检测不变量, 不可被正数过滤丢弃)
      - timestamp: Asia/Shanghai ISO 字符串 (含 +08:00)
    """
    symbol = row.get("symbol")
    if not is_valid_symbol(str(symbol)):
        return False
    for key in ("bid_prices", "bid_volumes", "ask_prices", "ask_volumes"):
        val = row.get(key)
        if not isinstance(val, list) or len(val) != 5:
            return False
    # prices: 正数或 None (0 价无效)
    for p in row["bid_prices"] + row["ask_prices"]:
        if p is not None and not _is_positive_number(p):
            return False
    # volumes: 非负 int 或 None (0 有效)
    for v in row["bid_volumes"] + row["ask_volumes"]:
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
            return False
    ts = row.get("timestamp")
    if not ts or not isinstance(ts, str) or _SHANGHAI_TZ_SUFFIX not in ts:
        return False
    return True


def filter_valid_depth(depth_map: dict[str, dict]) -> dict[str, dict]:
    """过滤口径合法的 depth 条目; 记录被丢弃数 (不记原始内容)。"""
    valid: dict[str, dict] = {}
    dropped = 0
    for sym, row in depth_map.items():
        if validate_depth_row({**row, "symbol": sym}):
            valid[sym] = row
        else:
            dropped += 1
    return valid


def _is_positive_number(value) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _is_nonneg_number(value) -> bool:
    try:
        return float(value) >= 0
    except (TypeError, ValueError):
        return False


def _is_finite_number(value) -> bool:
    try:
        import math

        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
