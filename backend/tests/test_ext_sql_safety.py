"""ext_columns SQL 注入防护测试 (Issue #150)。

覆盖:
- quote_ident 对各注入 payload 的转义正确性 (含 RCE 向量 COPY TO)
- 端到端: 用真实 DuckDB 验证恶意 field_name 不会产生文件 / 不泄漏数据
- is_valid_ext_ident 白名单 (config_id 防御)
- 回归: 合法特殊字符字段名 (中文/点) 仍能透传到 sink (证明未误伤数据)
"""
from __future__ import annotations

import os
import tempfile

import duckdb
import pytest

from app.db_safe import is_valid_ext_ident, quote_ident


# ===== quote_ident 转义正确性 =====

def test_quote_ident_wraps_in_double_quotes():
    assert quote_ident("close") == '"close"'


def test_quote_ident_escapes_embedded_double_quote():
    # 双引号双写: 字段名含 " 时必须转义, 否则可逃逸标识符
    assert quote_ident('a"b') == '"a""b"'


@pytest.mark.parametrize("payload", [
    'x" UNION SELECT 1',
    'x"); DROP TABLE t; --',
    'x" -- comment',
    'x"; --',
    'x" COPY (SELECT 1) TO \'/tmp/evil\' --',
])
def test_quote_ident_neutralizes_injection_payloads(payload):
    """转义后的串必须是一个整体带引号标识符, 内部双引号被双写。"""
    escaped = quote_ident(payload)
    # 整体仍是 "...." 包裹, 内部每个原始 " 都变成 ""
    assert escaped.startswith('"') and escaped.endswith('"')
    # 原始 payload 里的每个 " 都被双写
    inner = escaped[1:-1]
    assert inner == payload.replace('"', '""')


# ===== 端到端注入测试 (真实 DuckDB) =====

def _build_ext_view(con: duckdb.DuckDBPyConnection, field_name: str) -> None:
    """建一个含指定 field_name 列的 ext_x 视图, 模拟项目扩展数据。"""
    # 列名含特殊字符时, 建表也必须用双引号转义
    con.execute(f'CREATE OR REPLACE TABLE _t_{id(field_name) % 10000} (symbol VARCHAR, {quote_ident(field_name)} DOUBLE)')
    con.execute(f"INSERT INTO _t_{id(field_name) % 10000} VALUES ('000001.SZ', 12.3)")
    con.execute(f'CREATE OR REPLACE VIEW ext_x AS SELECT symbol, {quote_ident(field_name)} FROM _t_{id(field_name) % 10000}')


def test_sink_with_malicious_field_name_does_not_write_file():
    """恶意 field_name 经 quote_ident 后, COPY TO payload 不产生文件。

    复现 Issue #150 的 RCE 向量: 若 field_name 裸拼, 攻击者可构造 COPY TO 写任意文件。
    转义后, 整个 payload 变成字面列名, DuckDB 查询报「列不存在」, 不会执行 COPY。
    """
    tmp = os.path.join(tempfile.gettempdir(), "tf_sec_inject_sink.out")
    if os.path.exists(tmp):
        os.remove(tmp)

    con = duckdb.connect(":memory:")
    _build_ext_view(con, "close")  # 合法列名

    # 攻击 payload: 企图在查询里塞 COPY TO 写文件
    evil = 'close" UNION SELECT * FROM (COPY (SELECT 1) TO \'' + tmp + '\') --'
    sql = f'SELECT symbol, {quote_ident(evil)} FROM ext_x'
    # 转义后查询合法列失败 → 报 Binder Error, 绝不会执行 COPY
    with pytest.raises(Exception):
        con.execute(sql).fetchall()

    # 关键断言: 文件未被创建 (COPY 未执行)
    assert not os.path.exists(tmp), "RCE: COPY TO 写文件成功, 注入未堵死!"


def test_sink_with_malicious_field_name_does_not_leak_data():
    """恶意 field_name 企图用 UNION 读其它表数据 → 转义后仅作字面列名, 查询失败。"""
    con = duckdb.connect(":memory:")
    _build_ext_view(con, "close")
    con.execute("CREATE TABLE secret (pw VARCHAR)")
    con.execute("INSERT INTO secret VALUES ('leaked')")

    evil = 'close" UNION SELECT pw FROM secret --'
    sql = f'SELECT symbol, {quote_ident(evil)} FROM ext_x'
    # 不会返回 secret 表内容, 而是报错 (列 close"... UNION... 不存在)
    with pytest.raises(Exception):
        con.execute(sql).fetchall()


# ===== is_valid_ext_ident 白名单 =====

@pytest.mark.parametrize("ident, expected", [
    ("abc", True),
    ("abc_123", True),
    ("ABC_xyz", True),
    ("x_y", True),
    ("", False),
    ("a b", False),          # 含空格
    ("a-b", False),          # 含连字符
    ("a.b", False),          # 含点
    ("a\"b", False),         # 含双引号
    ("a'b", False),          # 含单引号
    ("a;b", False),          # 含分号
    ("中文", False),         # 含中文 (config_id 不允许, 但 field_name 允许 → 见回归测试)
])
def test_is_valid_ext_ident(ident, expected):
    assert is_valid_ext_ident(ident) is expected


# ===== parser 白名单: 拒绝恶意 config_id =====

def test_watchlist_parser_rejects_malicious_config_id():
    from app.api.watchlist import _parse_ext_columns
    # config_id 含注入字符 → 应被白名单拒绝
    assert _parse_ext_columns("evil'; DROP--.field") == []
    assert _parse_ext_columns("normal.field") == [("normal", "field")]


def test_kline_inline_parser_filter_via_is_valid_ext_ident():
    """kline parser 内联, 直接用 is_valid_ext_ident 验证过滤逻辑。"""
    # 模拟 kline._attach_ext 的解析过滤
    parts = ["ok.field", "bad';DROP.field", "中文.field", "a_1.value"]
    specs = []
    for part in parts:
        config_id, field_name = part.split(".", 1)
        if config_id and field_name and is_valid_ext_ident(config_id):
            specs.append((config_id, field_name))
    assert specs == [("ok", "field"), ("a_1", "value")]


# ===== 回归: 合法特殊字符字段名未被误伤 =====

def test_legal_special_field_name_still_queryable():
    """field_name 可合法含中文/点 (FieldDef.name 无校验), quote_ident 必须支持。"""
    con = duckdb.connect(":memory:")
    for name in ["涨幅", "市盈率.静态", "field.name"]:
        _build_ext_view(con, name)
        # 用 quote_ident 转义后, 特殊列名仍能正确查询
        sql = f'SELECT symbol, {quote_ident(name)} FROM ext_x'
        rows = con.execute(sql).fetchall()
        assert rows and rows[0][0] == "000001.SZ", f"合法字段名 {name} 查询失败 (被误伤)"
    con.close()
