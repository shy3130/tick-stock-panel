"""DuckDB 标识符安全原语。

扩展数据的表名由受限配置 ID 组成; 字段名可包含中文、点等非 ASCII
字符。所有动态 SQL sink 必须白名单校验前者, 并转义后者。
"""
from __future__ import annotations

import re

_EXT_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


def quote_ident(name: str) -> str:
    """将任意字段名转义为单个 DuckDB 双引号标识符。"""
    return '"' + name.replace('"', '""') + '"'


def is_valid_ext_ident(name: str) -> bool:
    """Return whether an extension configuration ID is safe in a table name."""
    return bool(_EXT_IDENT_RE.fullmatch(name))
