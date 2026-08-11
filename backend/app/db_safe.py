"""DuckDB 标识符安全原语 (Issue #150: ext_columns SQL 注入防护)。

集中提供标识符转义与校验, 供所有拼接 DuckDB SQL 的 sink 复用, 消除
"screener 有防护、kline/watchlist 没有"的不一致根因。
"""
from __future__ import annotations

import re

# 合法标识符白名单: 字母 / 数字 / 下划线。与 CreateExtReq.id 创建端校验一致,
# 用于 config_id 等「纯用户可控 + 仅合法标识符」字段的深度防御。
EXT_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


def quote_ident(name: str) -> str:
    """把列名/字段名转义为 DuckDB 双引号标识符。

    对任意字符安全: 仅对内嵌的双引号做双写转义 (``"`` → ``""``), 再用双引号包裹。
    转义后整个串成为单个「带引号标识符」, 即便 name 含 ``--``/``;``/``UNION``/``COPY``
    也只会被 DuckDB 当作字面列名, 不会被解释为 SQL 语法, 从根上杜绝注入。

    用于 field_name: 因 FieldDef.name 可合法含中文/点/特殊字符, 不能用白名单,
    只能在 sink 处转义。
    """
    return '"' + name.replace('"', '""') + '"'


def is_valid_ext_ident(name: str) -> bool:
    """config_id 等纯标识符字段是否仅含合法字符 (字母/数字/下划线)。"""
    return bool(EXT_IDENT_RE.match(name))
