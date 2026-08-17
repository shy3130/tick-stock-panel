"""自选标签 (watchlist) 服务单测: set_tags 往返 + 旧文件 schema 兼容。

存储是 `data/user_data/watchlist.parquet`, 通过 patch `_path()` 隔离到 tmp 目录。
"""
from unittest.mock import patch

import polars as pl

from app.services import watchlist


def _patch_path(tmp_path):
    return patch.object(watchlist, "_path", return_value=tmp_path / "watchlist.parquet")


def test_set_tags_roundtrip_normalize(tmp_path):
    with _patch_path(tmp_path):
        watchlist.add("600519.SH")
        watchlist.add("000858.SZ", note="茅台")

        # 去重 / trim / 剥离中英文逗号 (chr(0xFF0C) = 全角逗号)
        watchlist.set_tags("600519.SH", ["白酒", "白酒", " 短期 ", "困境,反转", chr(0xFF0C) + "测试"])
        rows = watchlist.list_symbols()
        by_symbol = {r["symbol"]: r for r in rows}
        assert by_symbol["600519.SH"]["tags"] == "白酒,短期,困境反转,测试"
        assert by_symbol["000858.SZ"]["tags"] == ""

        # 整体替换清空
        watchlist.set_tags("600519.SH", [])
        rows = watchlist.list_symbols()
        assert {r["symbol"]: r["tags"] for r in rows}["600519.SH"] == ""


def test_normalize_truncates_to_max_len(tmp_path):
    with _patch_path(tmp_path):
        watchlist.add("600519.SH")
        watchlist.set_tags("600519.SH", ["很" * 50])
        rows = watchlist.list_symbols()
        assert rows[0]["tags"] == "很" * 20


def test_add_batch_merges_tags_preserving_existing(tmp_path):
    with _patch_path(tmp_path):
        watchlist.add("600519.SH")
        watchlist.set_tags("600519.SH", ["白酒"])
        rows, added = watchlist.add_batch(["600519.SH", "000858.SZ", "000858.SZ"], tags=["导入", "白酒", "导入"])
        tags = {r["symbol"]: r["tags"] for r in rows}
        assert added == 1
        # 已有标的: 原标签保留, 批量标签追加去重
        assert tags["600519.SH"] == "白酒,导入"
        assert tags["000858.SZ"] == "导入,白酒"

        # 不传 tags 时已有标的标签原样保留
        rows, added = watchlist.add_batch(["600519.SH"])
        assert added == 0
        assert {r["symbol"]: r["tags"] for r in rows}["600519.SH"] == "白酒,导入"

        # 向后兼容: 不传 tags 新增标的无标签
        rows, added = watchlist.add_batch(["601318.SH"])
        assert added == 1
        assert {r["symbol"]: r["tags"] for r in rows}["601318.SH"] == ""


def test_add_preserves_tags_on_readd(tmp_path):
    with _patch_path(tmp_path):
        watchlist.add("600519.SH")
        watchlist.set_tags("600519.SH", ["白酒"])
        # 重新插入到最前 (同 symbol), 标签应保留
        watchlist.add("600519.SH")
        rows = watchlist.list_symbols()
        assert rows[0]["symbol"] == "600519.SH"
        assert rows[0]["tags"] == "白酒"


def test_old_file_without_tags_column_is_normalized(tmp_path):
    p = tmp_path / "watchlist.parquet"
    # 模拟旧版本文件: 只有 symbol/added_at/note, 无 tags 列
    pl.DataFrame({
        "symbol": ["600519.SH"],
        "added_at": ["2026-01-01T00:00:00"],
        "note": [""],
    }).write_parquet(p)
    with _patch_path(tmp_path):
        rows = watchlist.list_symbols()
        assert rows[0]["symbol"] == "600519.SH"
        assert rows[0]["tags"] == ""

        # 旧文件上的 set_tags 应正常工作且写出新列
        watchlist.set_tags("600519.SH", ["白酒"])
        rows = watchlist.list_symbols()
        assert rows[0]["tags"] == "白酒"


def test_clear_keeps_tags_column(tmp_path):
    with _patch_path(tmp_path):
        watchlist.add("600519.SH")
        watchlist.set_tags("600519.SH", ["白酒"])
        assert watchlist.clear() == 1
        watchlist.add("000858.SZ")
        rows = watchlist.list_symbols()
        assert len(rows) == 1
        assert rows[0]["tags"] == ""


def test_remove_preserves_schema(tmp_path):
    with _patch_path(tmp_path):
        watchlist.add("600519.SH")
        watchlist.add("000858.SZ")
        watchlist.set_tags("600519.SH", ["白酒"])
        watchlist.remove("000858.SZ")
        rows = watchlist.list_symbols()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "600519.SH"
        assert rows[0]["tags"] == "白酒"
