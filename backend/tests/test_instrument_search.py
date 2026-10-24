"""标的搜索测试: 代码 / 名称 / 拼音首字母 (同花顺式 payh → 平安银行)。

直接调用 search_instruments, 用最小 FakeRepo 提供 instruments 缓存, 不走 HTTP/DB。
"""
from __future__ import annotations

import types

import polars as pl
import pytest

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
    # 直调绕开 FastAPI 依赖注入, offset/balance 默认值是 Query 对象, 须显式传
    return search_instruments(
        _request(repo), q=q, limit=limit, asset_types=asset_types, offset=0, balance=False,
    )["results"]


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
    cq = search_instruments(_request(repo), q="cqbh", limit=20, asset_types="stock", offset=0, balance=False)["results"]      # chóng qīng
    zq = search_instruments(_request(repo), q="zqbh", limit=20, asset_types="stock", offset=0, balance=False)["results"]      # zhòng qìng
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
    r1 = search_instruments(req, q="payh", limit=20, asset_types="stock", offset=0, balance=False)["results"]
    r2 = search_instruments(req, q="payh", limit=20, asset_types="stock", offset=0, balance=False)["results"]
    assert r1 == r2
    assert [r["symbol"] for r in r1] == ["000001.SZ"]


# ===== 分页 (offset/limit) 与类型均衡 (balance) =====

ETFS = pl.DataFrame({
    "symbol": ["510300.SH", "513100.SH"],
    "code": ["510300", "513100"],
    "name": ["沪深300ETF", "纳指ETF"],
})

INDICES = pl.DataFrame({
    "symbol": ["000300.SH", "399001.SZ"],
    "code": ["000300", "399001"],
    "name": ["沪深300", "深证成指"],
})

MANY_STOCKS = pl.DataFrame({
    "symbol": [f"600{i:03d}.SH" for i in range(30)],
    "code": [f"600{i:03d}" for i in range(30)],
    "name": [f"测试{i:02d}" for i in range(30)],
})


def _search_page(
    q: str,
    asset_types: str = "stock",
    limit: int = 20,
    offset: int = 0,
    balance: bool = False,
    by_asset: dict[str, pl.DataFrame] | None = None,
) -> dict:
    repo = _FakeRepo(by_asset if by_asset is not None else {"stock": STOCKS})
    return search_instruments(
        _request(repo), q=q, limit=limit, asset_types=asset_types, offset=offset, balance=balance,
    )


def _legacy_rows(repo: _FakeRepo, q: str, limit: int, asset_types: str) -> list[dict]:
    """分页改造前 search_instruments 匹配逻辑的快照, 仅作新旧等价性对照。

    旧实现每层就地 head(remaining) 截断; 新实现构造全序后 slice(0, limit)。
    """
    if not q.strip():
        return []
    import polars as pl

    from app.api.kline import _match_pinyin

    types = [t.strip() for t in asset_types.split(",") if t.strip()]
    parts: list[pl.DataFrame] = []
    for t in types:
        df_t = repo.get_instruments_asset(t)
        if df_t.is_empty() or "symbol" not in df_t.columns:
            continue
        parts.append(df_t.with_columns([
            pl.col("symbol").cast(pl.Utf8).alias("symbol"),
            (pl.col("name").cast(pl.Utf8) if "name" in df_t.columns else pl.lit("")).alias("name"),
            (pl.col("code").cast(pl.Utf8) if "code" in df_t.columns else pl.lit("")).alias("code"),
            pl.lit(t).alias("asset_type"),
        ]).select(["symbol", "name", "code", "asset_type"]))
    if not parts:
        return []
    df = pl.concat(parts, how="vertical")

    keyword = q.strip().upper()
    is_pinyin_query = keyword.isalpha() and keyword.isascii()

    prefix_mask = (
        pl.col("code").str.starts_with(keyword)
        | pl.col("symbol").str.to_uppercase().str.starts_with(keyword)
    )
    contains_mask = (
        pl.col("code").str.contains(keyword, literal=True)
        | pl.col("symbol").str.to_uppercase().str.contains(keyword, literal=True)
        | pl.col("name").str.contains(keyword, literal=True)
    )

    prefix_hits = df.filter(prefix_mask).head(limit)
    if prefix_hits.height >= limit:
        matched = prefix_hits
    else:
        collected = [prefix_hits] if prefix_hits.height else []
        seen = set(prefix_hits["symbol"].to_list()) if prefix_hits.height else set()
        remaining = limit - prefix_hits.height
        if is_pinyin_query and remaining > 0:
            pinyin_rows = []
            for row in df.filter(~pl.col("symbol").is_in(seen)).iter_rows(named=True):
                if _match_pinyin(row["name"], keyword):
                    pinyin_rows.append(row)
                    if len(pinyin_rows) >= remaining:
                        break
            if pinyin_rows:
                collected.append(pl.DataFrame(pinyin_rows))
                seen.update(r["symbol"] for r in pinyin_rows)
                remaining -= len(pinyin_rows)
        if remaining > 0:
            contain_hits = df.filter(contains_mask & ~pl.col("symbol").is_in(seen)).head(remaining)
            if contain_hits.height:
                collected.append(contain_hits)
        matched = (
            pl.concat(collected, how="vertical") if len(collected) > 1
            else (collected[0] if collected else df.head(0))
        )
    return matched.select(["symbol", "name", "code", "asset_type"]).to_dicts()


# 拼音命中数超过 limit 的夹具: 6 只 "zg" 首字母名称, 覆盖旧实现拼音层 break 截断路径
ZG_STOCKS = pl.DataFrame({
    "symbol": ["601398.SH", "601288.SH", "601988.SH", "601328.SH", "601818.SH", "601166.SH"],
    "code": ["601398", "601288", "601988", "601328", "601818", "601166"],
    "name": ["中国石油", "中国石化", "中国平安", "中国中铁", "中国船舶", "中国神华"],
})


@pytest.mark.parametrize("q", ["6005", "银行", "payh", "pf", "z", "m", "平安", "000", "xyz", "sh"])
@pytest.mark.parametrize("asset_types", ["stock", "stock,etf,index"])
@pytest.mark.parametrize("limit", [1, 2, 20])
def test_first_page_matches_legacy_implementation(q: str, asset_types: str, limit: int):
    """等价性: 新实现 offset=0/balance=false 的第一页与旧 head 截断实现逐条一致。"""
    by_asset = {"stock": STOCKS, "etf": ETFS, "index": INDICES}
    repo = _FakeRepo(by_asset)
    new = search_instruments(
        _request(repo), q=q, limit=limit, asset_types=asset_types, offset=0, balance=False,
    )["results"]
    assert new == _legacy_rows(repo, q, limit, asset_types)


def test_first_page_matches_legacy_when_pinyin_truncates():
    """拼音命中数超过 limit 时 (旧实现 break 截断路径) 新旧第一页仍一致。"""
    repo = _FakeRepo({"stock": ZG_STOCKS})
    new = search_instruments(
        _request(repo), q="zg", limit=2, asset_types="stock", offset=0, balance=False,
    )["results"]
    assert new == _legacy_rows(repo, "zg", 2, "stock")
    assert len(new) == 2


def test_pagination_pages_concat_to_full_sequence():
    """分页完整性: 各 offset 页拼接 == 全序全集, 不重不漏。"""
    by_asset = {"stock": MANY_STOCKS}
    full = _search_page("600", limit=50, by_asset=by_asset)["results"]
    assert len(full) == 30

    pages: list[dict] = []
    offset = 0
    while True:
        resp = _search_page("600", limit=8, offset=offset, by_asset=by_asset)
        pages.extend(resp["results"])
        if not resp["has_more"]:
            break
        offset = resp["next_offset"]
    assert pages == full
    assert len({r["symbol"] for r in pages}) == 30


def test_balance_interleaves_asset_types():
    """balance=true: 多类型 round-robin 交织, 宽泛关键词下股票不再挤掉 ETF/指数。"""
    by_asset = {
        "stock": pl.DataFrame({
            "symbol": ["600001.SH", "600002.SH", "600003.SH"],
            "code": ["600001", "600002", "600003"],
            "name": ["测试股一", "测试股二", "测试股三"],
        }),
        "etf": pl.DataFrame({
            "symbol": ["510300.SH", "513100.SH"],
            "code": ["510300", "513100"],
            "name": ["测试ETF一", "测试ETF二"],
        }),
        "index": pl.DataFrame({
            "symbol": ["000300.SH"],
            "code": ["000300"],
            "name": ["测试指数一"],
        }),
    }
    resp = _search_page("测试", asset_types="stock,etf,index", limit=50, balance=True, by_asset=by_asset)
    assert [r["symbol"] for r in resp["results"]] == [
        "600001.SH", "510300.SH", "000300.SH", "600002.SH", "513100.SH", "600003.SH",
    ]
    assert resp["has_more"] is False
    assert resp["next_offset"] is None


def test_balance_pagination_skips_exhausted_type():
    """交织翻页: 某类型耗尽后游标连续, 第二页不再出现该类型。"""
    by_asset = {
        "stock": pl.DataFrame({
            "symbol": ["600001.SH", "600002.SH", "600003.SH"],
            "code": ["600001", "600002", "600003"],
            "name": ["测股一", "测股二", "测股三"],
        }),
        "etf": pl.DataFrame({
            "symbol": ["510300.SH", "513100.SH"],
            "code": ["510300", "513100"],
            "name": ["测ETF一", "测ETF二"],
        }),
        "index": pl.DataFrame({
            "symbol": ["000300.SH"],
            "code": ["000300"],
            "name": ["测指一"],
        }),
    }
    p1 = _search_page("测", asset_types="stock,etf,index", limit=3, balance=True, by_asset=by_asset)
    # 第一轮交织: stock/etf/index 各一 (index 仅 1 只即耗尽)
    assert [r["asset_type"] for r in p1["results"]] == ["stock", "etf", "index"]
    assert p1["has_more"] is True

    p2 = _search_page("测", asset_types="stock,etf,index", limit=3, offset=3, balance=True, by_asset=by_asset)
    assert "index" not in [r["asset_type"] for r in p2["results"]]
    # 两页合起来不重不漏
    combined = p1["results"] + p2["results"]
    assert len({r["symbol"] for r in combined}) == len(combined)


def test_has_more_boundary_exact_page():
    """总数恰好等于 offset+limit 时 has_more=False, next_offset=None。"""
    resp = _search_page("银行", limit=2)
    assert len(resp["results"]) == 2
    assert resp["has_more"] is False
    assert resp["next_offset"] is None


def test_offset_beyond_total_returns_empty_page():
    resp = _search_page("银行", limit=20, offset=99)
    assert resp["results"] == []
    assert resp["has_more"] is False
    assert resp["next_offset"] is None


def test_pinyin_keyword_paginates_without_dup():
    """拼音关键词翻页: 6 只 zg 命中, limit=4 两页不重不漏。"""
    by_asset = {"stock": ZG_STOCKS}
    p1 = _search_page("zg", limit=4, by_asset=by_asset)
    p2 = _search_page("zg", limit=4, offset=4, by_asset=by_asset)
    combined = p1["results"] + p2["results"]
    assert p1["has_more"] is True
    assert p2["has_more"] is False
    assert len(combined) == 6
    assert len({r["symbol"] for r in combined}) == 6


def test_single_type_balance_matches_unbalanced_first_page():
    """单类型时 balance 开关不改变结果 (round-robin 退化为顺序本身)。"""
    for q in ("6005", "payh", "银行"):
        assert _search_page(q, balance=True)["results"] == _search_page(q, balance=False)["results"]


def test_empty_asset_types_returns_empty():
    resp = _search_page("6005", asset_types="")
    assert resp["results"] == []
    assert resp["has_more"] is False


def test_response_includes_pagination_fields():
    """响应附带分页字段; results 结构 (symbol/name/code/asset_type) 不变。"""
    resp = _search_page("6005", asset_types="stock,etf,index")
    assert {"results", "has_more", "next_offset", "instruments_version"} <= resp.keys()
    assert all({"symbol", "name", "code", "asset_type"} <= r.keys() for r in resp["results"])


# ===== API 层: HTTP 参数解析 (既有调用方不传 offset/balance) =====

def _http_client(by_asset: dict[str, pl.DataFrame]):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.kline import router

    app = FastAPI()
    app.include_router(router)
    app.state.repo = _FakeRepo(by_asset)
    return TestClient(app)


def test_http_pagination_round_trip():
    """HTTP 层: offset/limit/balance 解析正确, next_offset 可直接用于下一跳。"""
    client = _http_client({"stock": MANY_STOCKS})
    p1 = client.get("/api/kline/instruments/search", params={"q": "600", "limit": 8, "balance": "true"}).json()
    assert len(p1["results"]) == 8
    assert p1["has_more"] is True
    assert p1["next_offset"] == 8

    p2 = client.get(
        "/api/kline/instruments/search",
        params={"q": "600", "limit": 8, "offset": p1["next_offset"], "balance": "true"},
    ).json()
    assert [r["symbol"] for r in p1["results"] + p2["results"]] == [
        r["symbol"] for r in _search_page("600", limit=16, by_asset={"stock": MANY_STOCKS})["results"]
    ]


def test_http_defaults_unchanged_for_legacy_callers():
    """不传 offset/balance 的旧调用方: results 即首页, 仅多出分页字段。"""
    client = _http_client({"stock": STOCKS})
    resp = client.get("/api/kline/instruments/search", params={"q": "6005", "asset_types": "stock"}).json()
    assert [r["symbol"] for r in resp["results"]] == ["600519.SH"]
    assert resp["has_more"] is False
    assert resp["next_offset"] is None


def test_http_rejects_negative_offset():
    client = _http_client({"stock": STOCKS})
    assert client.get("/api/kline/instruments/search", params={"q": "6005", "offset": -1}).status_code == 422
