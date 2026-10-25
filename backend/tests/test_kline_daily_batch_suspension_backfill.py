"""daily-batch 停牌回补: 迷你K按交易日语义取「最近 N 根」, 跳过停牌空档。

未修复行为: 首段「自然日×2」窗口内交易日不足 days 时直接欠取样
(长期停牌股复牌当日迷你K只剩 3~4 根, 正常股 12 根)。
"""
import json
import datetime as _dt

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository

# 钉死的北京日期, 不随跑测试那天的 date.today() 漂移
_BJ = _dt.date(2026, 3, 2)
# days=12 → 首段窗口 start = _BJ - 24 自然日; 回补轮 start 再各减 120/480 自然日
_SUSPENDED = "002388.SZ"
_NORMAL = "600000.SH"
_ETF = "510300.SH"


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def _rows(symbol: str, dates: list[_dt.date]) -> pl.DataFrame:
    n = len(dates)
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "date": dates,
        "open": [10.0] * n, "high": [10.5] * n,
        "low": [9.5] * n, "close": [10.2] * n,
        "volume": [100.0] * n,
    })


def _window_filter(df: pl.DataFrame, start, end) -> pl.DataFrame:
    return df.filter((pl.col("date") >= start) & (pl.col("date") <= end))


def _fake_req(repo):
    state = type("S", (), {"repo": repo})()
    return type("R", (), {"app": type("A", (), {"state": state})()})()


def test_suspended_stock_backfills_across_gap(repo, monkeypatch):
    """窗口内只有复牌后 3 根 → 回补跨过停牌空档补满 days 根; 正常股零额外读。"""
    from app.api import kline as kline_api

    calls: list[tuple[list[str], _dt.date]] = []
    # 停牌前 1 月连续 20 天有K + 复牌后 3 根; 首段窗口 [2026-02-06, 03-02] 只含后 3 根
    jan = [_dt.date(2026, 1, d) for d in range(5, 25)]
    suspended_hist = _rows(_SUSPENDED, [*jan, _dt.date(2026, 2, 26), _dt.date(2026, 2, 27), _BJ])
    # 正常股: 窗口内每隔一天一根 (13 根 ≥ 12)
    normal_hist = _rows(_NORMAL, [_dt.date(2026, 2, d) for d in range(6, 28, 2)] + [_BJ])
    hist = pl.concat([suspended_hist, normal_hist])

    def fake_stock_batch(symbols, start, end, columns=None):
        calls.append((list(symbols), start))
        return _window_filter(hist, start, end)

    monkeypatch.setattr(kline_api, "cn_today", lambda: _BJ, raising=False)
    monkeypatch.setattr(repo, "resolve_asset_type", lambda s: "stock")
    monkeypatch.setattr(repo, "get_daily_batch", fake_stock_batch)

    out = json.loads(kline_api.get_daily_batch(
        _fake_req(repo), {"symbols": [_NORMAL, _SUSPENDED], "days": 12},
    ).body)

    rows = out["data"][_SUSPENDED]
    assert len(rows) == 12, "停牌股应跨过空档补满 days 根"
    assert rows[-1]["date"] == _BJ.isoformat()
    assert rows[0]["date"] < _dt.date(2026, 2, 1).isoformat(), "首根应来自停牌前 (回补跨过空档)"
    assert len(out["data"][_NORMAL]) == 12, "正常股仍 tail 封顶"
    # 补满即停: 初始读 + 一轮回补; 回补只读欠取样的股票, 不为正常股多花一次读
    assert calls[0][0] == [_NORMAL, _SUSPENDED]
    assert calls[1][0] == [_SUSPENDED]
    assert len(calls) == 2


def test_stock_absent_from_window_backfills_from_older(repo, monkeypatch):
    """停牌早于首段窗口起点 (整窗 0 根) → 从停牌前数据回补, 返回最新可得 N 根。"""
    from app.api import kline as kline_api

    calls = []
    hist = _rows(_SUSPENDED, [_dt.date(2026, 1, d) for d in range(5, 25)])  # 仅停牌前

    def fake_stock_batch(symbols, start, end, columns=None):
        calls.append(start)
        return _window_filter(hist, start, end)

    monkeypatch.setattr(kline_api, "cn_today", lambda: _BJ, raising=False)
    monkeypatch.setattr(repo, "resolve_asset_type", lambda s: "stock")
    monkeypatch.setattr(repo, "get_daily_batch", fake_stock_batch)

    out = json.loads(kline_api.get_daily_batch(_fake_req(repo), {"symbols": [_SUSPENDED], "days": 12}).body)
    rows = out["data"].get(_SUSPENDED)
    assert rows and len(rows) == 12
    assert rows[-1]["date"] == _dt.date(2026, 1, 24).isoformat(), "末端是最新可得交易日"
    assert len(calls) == 2


def test_beyond_backfill_cap_returns_nothing_and_stops(repo, monkeypatch):
    """停牌早于回补上限 (约一年) → 不在结果中; 读取次数有界 (初始 + 两轮), 不扫全库。"""
    from app.api import kline as kline_api

    calls = []
    # 回补第二轮窗口起点 = 2026-02-06 - 480d = 2024-10-14; 数据早于此 → 两轮都够不着
    hist = _rows(_SUSPENDED, [_dt.date(2024, 6, d) for d in range(2, 7)])

    def fake_stock_batch(symbols, start, end, columns=None):
        calls.append(start)
        return _window_filter(hist, start, end)

    monkeypatch.setattr(kline_api, "cn_today", lambda: _BJ, raising=False)
    monkeypatch.setattr(repo, "resolve_asset_type", lambda s: "stock")
    monkeypatch.setattr(repo, "get_daily_batch", fake_stock_batch)

    out = json.loads(kline_api.get_daily_batch(_fake_req(repo), {"symbols": [_SUSPENDED], "days": 12}).body)
    assert _SUSPENDED not in out["data"]
    assert len(calls) == 3, "初始读 + 两轮回补后必须终止"


def test_suspended_etf_backfills_per_symbol(repo, monkeypatch):
    """ETF 分支 (逐只独立存储) 同样享受停牌回补。"""
    from app.api import kline as kline_api

    calls = []
    hist = _rows(
        _ETF,
        [_dt.date(2026, 1, d) for d in range(5, 25)]
        + [_dt.date(2026, 2, 26), _dt.date(2026, 2, 27), _BJ],
    )

    def fake_etf_daily(symbol, start, end, columns=None):
        calls.append(symbol)
        return _window_filter(hist, start, end)

    monkeypatch.setattr(kline_api, "cn_today", lambda: _BJ, raising=False)
    monkeypatch.setattr(repo, "resolve_asset_type", lambda s: "etf" if s == _ETF else "stock")
    monkeypatch.setattr(repo, "get_etf_daily", fake_etf_daily)
    monkeypatch.setattr(repo, "get_daily_batch", lambda *a, **k: pl.DataFrame())

    out = json.loads(kline_api.get_daily_batch(_fake_req(repo), {"symbols": [_ETF], "days": 12}).body)
    rows = out["data"].get(_ETF)
    assert rows and len(rows) == 12
    assert rows[0]["date"] < _dt.date(2026, 2, 1).isoformat()
    assert len(calls) == 2
