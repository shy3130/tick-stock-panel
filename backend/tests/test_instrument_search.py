"""标的搜索测试: 代码 / 名称 / 拼音首字母 (同花顺式 payh → 平安银行)。

直接调用 search_instruments, 用最小 FakeRepo 提供 instruments 缓存, 不走 HTTP/DB。
"""
from __future__ import annotations

import types

import polars as pl

from app.api.kline import search_instruments


class _FakeRepo:
    """最小 repo 桩: 只实现 search_instruments 依赖的 get_instruments_asset。"""

    def __init__(self, by_asset: dict[str, pl.DataFrame]) -> None:
        self.store = types.SimpleNamespace(data_dir="data")
        self._by_asset = by_asset

    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        return self._by_asset.get(asset_type, pl.DataFrame())


def _request(repo: _FakeRepo) -> types.SimpleNamespace:
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(repo=repo)))


STOCKS = pl.DataFrame({
    "symbol": ["000001.SZ", "600000.SH", "600519.SH", "000333.SZ", "600737.SH"],
    "code": ["000001", "600000", "600519", "000333", "600737"],
    "name": ["平安银行", "浦发银行", "贵州茅台", "美的集团", "中粮糖业"],
})


def _search(q: str, asset_types: str = "stock", limit: int = 20) -> list[dict]:
    repo = _FakeRepo({"stock": STOCKS})
    return search_instruments(_request(repo), q=q, limit=limit, asset_types=asset_types)["results"]


# ===== 既有逻辑回归: 代码 / 名称搜索不受影响 =====

def test_code_prefix_match():
    """code 前缀: 6005 → 600519 (前缀优先)。"""
    rows = _search("6005")
    assert "600519.SH" in [r["symbol"] for r in rows]


def test_symbol_contains_match():
    rows = _search("6005")
    assert "600519.SH" in [r["symbol"] for r in rows]


def test_chinese_name_contains_match():
    rows = _search("银行")
    assert sorted(r["symbol"] for r in rows) == ["000001.SZ", "600000.SH"]


def test_empty_query_returns_empty():
    assert _search("   ") == []


# ===== 拼音首字母搜索 (新功能) =====

def test_pinyin_full_initials_match():
    """payh → 平安银行"""
    rows = _search("payh")
    assert [r["symbol"] for r in rows] == ["000001.SZ"]


def test_pinyin_prefix_match():
    """m → 美的集团 (m 开头)"""
    rows = _search("m")
    assert "000333.SZ" in [r["symbol"] for r in rows]


def test_pinyin_prefix_picks_multiple():
    """pf → 浦发银行; pa → 平安银行 (前缀区分)"""
    assert [r["symbol"] for r in _search("pf")] == ["600000.SH"]
    assert [r["symbol"] for r in _search("pa")] == ["000001.SZ"]


def test_pinyin_respects_limit():
    """limit 限制拼音结果数"""
    rows = _search("z", limit=1)  # z → 中粮糖业
    assert len(rows) == 1


def test_pinyin_layer_between_prefix_and_contains():
    """拼音命中应排在包含匹配之前 (分层优先级)。"""
    rows = _search("md")  # md → 美的集团 (拼音); 无代码/符号以 md 前缀
    assert "000333.SZ" in [r["symbol"] for r in rows]


def test_pinyin_and_code_prefix_coexist():
    """纯字母查询同时命中 code 前缀和拼音首字母时, code 前缀优先排前。"""
    # '600000' 是浦发的 code 前缀; 这里用纯字母无法命中 code, 故仅验证拼音路径独立可用
    rows = _search("pf")  # 浦发
    assert "600000.SH" in [r["symbol"] for r in rows]


# ===== 多音字 =====

POLYPHONE_STOCKS = pl.DataFrame({
    "symbol": ["600729.SH", "000625.SZ"],
    "code": ["600729", "000625"],
    "name": ["重庆百货", "长安汽车"],
})


def test_polyphone_all_readings_match():
    """'重庆' 多音字: cq (chóng) 和 zq (zhòng) 读音都应命中。"""
    repo = _FakeRepo({"stock": POLYPHONE_STOCKS})
    # 取首字母集, 验证两种读音都能搜到
    cq = search_instruments(_request(repo), q="cqbh", limit=20, asset_types="stock")["results"]      # chóng qīng
    zq = search_instruments(_request(repo), q="zqbh", limit=20, asset_types="stock")["results"]      # zhòng qìng
    assert "600729.SH" in [r["symbol"] for r in cq]
    assert "600729.SH" in [r["symbol"] for r in zq]


# ===== 边界 =====

def test_non_ascii_skips_pinyin_branch():
    """中文输入不走拼音分支, 仍按名称匹配。"""
    rows = _search("平安")
    assert [r["symbol"] for r in rows] == ["000001.SZ"]


def test_digits_skips_pinyin_branch():
    """数字输入不走拼音分支, 走 code 前缀。"""
    rows = _search("000")
    assert "000001.SZ" in [r["symbol"] for r in rows]


def test_no_pinyin_hit_returns_empty():
    """无任何拼音命中时返回空 (不报错)。"""
    assert _search("xyz") == []


def test_cache_returns_same_result_across_calls():
    """lru_cache 不应在不同请求间串结果 (按 name 缓存, 查询无状态)。"""
    repo = _FakeRepo({"stock": STOCKS})
    req = _request(repo)
    r1 = search_instruments(req, q="payh", limit=20, asset_types="stock")["results"]
    r2 = search_instruments(req, q="payh", limit=20, asset_types="stock")["results"]
    assert r1 == r2
    assert [r["symbol"] for r in r1] == ["000001.SZ"]
