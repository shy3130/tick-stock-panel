"""自选页 enriched 端点的 LEFT JOIN 回归测试.

核心契约 (修复 inner-filter bug 后):
  自选列表里的每一只标的都必须出现在返回结果中, 即使它不在 enriched 缓存里
  (新股 / 冷门股 / 新用户未同步). 缺失标的的指标字段为 null, 前端渲染为 "—".

旧 bug: `df_e.filter(is_in(stock_symbols))` 以 enriched 为主表, 会把不在缓存
universe 里的自选股静默丢弃.
"""
from __future__ import annotations

from types import SimpleNamespace

import polars as pl

from app.api import watchlist as wl_api


class _FakeRepo:
    """最小化 repo mock: 只实现 watchlist_enriched 调用到的方法."""

    def __init__(self, enriched_df, enriched_date, etf_df=None, etf_date=None,
                 instruments_df=None, name_map=None, etf_set=None):
        self._enriched = enriched_df
        self._enriched_date = enriched_date
        self._etf = etf_df
        self._etf_date = etf_date
        self._instruments = instruments_df or pl.DataFrame()
        self._name_map = name_map or {}
        self._etf_set = etf_set or set()

    def get_enriched_latest(self):
        return self._enriched, self._enriched_date

    def get_enriched_latest_asset(self, asset):
        if asset == "etf":
            etf = self._etf if self._etf is not None else pl.DataFrame()
            return etf, self._etf_date
        return pl.DataFrame(), None

    def get_etf_symbol_set(self):
        return self._etf_set

    def get_instruments(self):
        return self._instruments

    def get_name_map(self, symbols):
        return {s: n for s, n in self._name_map.items() if s in (symbols or [])}


def _make_request(repo):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def _enriched_df(symbols_data):
    """symbols_data: [(symbol, close, change_pct, amount), ...]"""
    return pl.DataFrame(
        [{"symbol": s, "close": c, "change_pct": p, "amount": a, "turnover_rate": 1.0}
         for s, c, p, a in symbols_data],
        schema_overrides={
            "close": pl.Float64, "change_pct": pl.Float64,
            "amount": pl.Float64, "turnover_rate": pl.Float64,
        },
    )


def test_watchlist_symbol_not_in_enriched_still_returned(monkeypatch):
    """核心回归: 自选里有但 enriched 缓存里没有的标的, 必须仍返回一行 (指标 null)."""
    # enriched 缓存只覆盖 600519, 不覆盖 999999 (新加的冷门股)
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": "600519"}, {"symbol": "999999"}])
    repo = _FakeRepo(
        enriched_df=_enriched_df([("600519", 1800.0, 1.2, 1e9)]),
        enriched_date="2026-07-08",
        name_map={"600519": "贵州茅台", "999999": "未知股"},
    )

    # ext_columns 显式传 None 绕过 FastAPI Query 默认值
    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    syms = [r["symbol"] for r in res["rows"]]
    assert "600519" in syms, "缓存里有的标的必须返回"
    assert "999999" in syms, "缓存里没有的自选标的也必须返回 (修复的核心)"

    # 缺失标的指标应为 null
    row_999 = next(r for r in res["rows"] if r["symbol"] == "999999")
    assert row_999["close"] is None, f"缺失指标应为 null, 实际: {row_999['close']}"
    assert row_999["name"] == "未知股", "name 走 get_name_map, 应正常返回"

    # 命中标的指标正常
    row_519 = next(r for r in res["rows"] if r["symbol"] == "600519")
    assert row_519["close"] == 1800.0


def test_all_watchlist_missing_from_enriched(monkeypatch):
    """极端情况: 自选全是 enriched 没覆盖的 (新用户冷启动场景)."""
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": "000001"}, {"symbol": "000002"}])
    repo = _FakeRepo(
        enriched_df=pl.DataFrame(schema={"symbol": pl.Utf8}),  # 空 schema, 模拟未就绪
        enriched_date=None,
    )

    # 注: 原契约 stock_symbols 非空且 enriched 空 → 返回未就绪. 这是设计, 不变.
    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    assert res["rows"] == []
    assert res["as_of"] is None


def test_partial_coverage_preserves_count(monkeypatch):
    """多只自选, 部分覆盖: 返回行数必须 == 自选股票数."""
    syms = ["600519", "000001", "999888", "888999"]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in syms])
    repo = _FakeRepo(
        enriched_df=_enriched_df([
            ("600519", 1800.0, 1.2, 1e9),
            ("000001", 15.0, 0.3, 2e9),
        ]),
        enriched_date="2026-07-08",
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    assert len(res["rows"]) == len(syms), \
        f"返回行数应等于自选数 {len(syms)}, 实际 {len(res['rows'])}"

    returned = {r["symbol"] for r in res["rows"]}
    assert returned == set(syms)


def test_etf_not_in_enriched_still_returned(monkeypatch):
    """ETF 同样: 自选了但 ETF enriched 缓存没有的, 也应返回 (指标 null)."""
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": "510300"}, {"symbol": "599999"}])
    repo = _FakeRepo(
        enriched_df=pl.DataFrame(schema={"symbol": pl.Utf8}),  # 无股票自选
        enriched_date=None,
        etf_df=_enriched_df([("510300", 4.0, 0.5, 1e8)]),
        etf_date="2026-07-08",
        etf_set={"510300", "599999"},
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    syms = [r["symbol"] for r in res["rows"]]
    assert "510300" in syms
    assert "599999" in syms, "ETF enriched 缺失的自选标的也必须返回"

    row_missing = next(r for r in res["rows"] if r["symbol"] == "599999")
    assert row_missing["close"] is None
