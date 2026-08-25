"""JSON 边界的有限数值与日期规范化工具。"""
from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime
from numbers import Integral, Real
from typing import Any


def finite_float_or_none(value: Any) -> float | None:
    """把数值转成有限 float；bool、NaN、Inf、非数值统一返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def json_safe(value: Any) -> Any:
    """递归转换为严格 JSON 可序列化值，所有非有限数值变为 null。"""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]

    # numpy 标量等通常提供 item()；只解包标量，不猜测数组语义。
    item = getattr(value, "item", None)
    if callable(item):
        try:
            scalar = item()
        except (TypeError, ValueError):
            pass
        else:
            if scalar is not value:
                return json_safe(scalar)
    return value
