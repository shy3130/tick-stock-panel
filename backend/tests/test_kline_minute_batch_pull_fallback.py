"""minute-batch 补拉落空兜底: 本地已有部分K + 本轮补拉失败/为空时回退返回本地行。

2026-10-23 实测事故: 上游断网 40 分钟, 全量分钟服务停写, 所有标的尾部落后,
实时补拉全空 → 未修复代码把这些标的整只从结果丢弃, 前端分时列全部退化为
只剩竞价栏。修复后: 回退返回本地已落盘的行 (图冻结在最后落盘时刻), 并以
truncated 字段标记残缺 symbol, 供前端提示。
"""
import json
import datetime as _dt

import polars as pl
import pytest

from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

_BJ_DATE = _dt.date(2026, 10, 23)  # 周五交易日, 钉死不随跑测试当天漂移
_BJ_NOW = _dt.datetime(2026, 10, 23, 11, 0)  # 盘中 11:00 → expected=90 根
_SYM = "600000.SH"
_COLS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]


def _bars(symbol: str, start_hm: _dt.datetime, n: int) -> pl.DataFrame:
    """n 根连续 1 分钟 bar (datetime 为 Datetime 类型, 供 sort/diff 用)。"""
    ts = pl.datetime_range(start_hm, start_hm + _dt.timedelta(minutes=n - 1), "1m", eager=True)
    return pl.DataFrame({
        "symbol": pl.Series([symbol] * n, dtype=pl.String),
        "datetime": ts,
        "open": [10.0] * n, "high": [10.1] * n, "low": [9.9] * n, "close": [10.0] * n,
        "volume": [100.0] * n, "amount": [1000.0] * n,
    })


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def _fake_req(repo):
    capset = CapabilitySet()
    capset.grant(Cap.KLINE_MINUTE_BATCH)
    state = type("S", (), {"repo": repo, "capabilities": capset})()
    return type("R", (), {"app": type("A", (), {"state": state})()})()


def _pin_clock(repo, monkeypatch, local_df_by_symbol: dict[str, pl.DataFrame], pull_df: pl.DataFrame, calls: list):
    """钉死北京时钟 + 本地分区 + 补拉结果。pull_df 为空帧即模拟补拉失败/断网。"""
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: _BJ_DATE)
    monkeypatch.setattr(kline_api, "cn_now", lambda: _BJ_NOW)
    monkeypatch.setattr(kline_api.trading_day, "is_trading_day", lambda: True)

    def fake_get_minute_batch(symbols, d, asset_type="stock"):
        frames = [local_df_by_symbol[s] for s in symbols if s in local_df_by_symbol]
        return pl.concat(frames) if frames else pl.DataFrame()

    def fake_sync(symbols, *args, **kwargs):
        calls.append(list(symbols))
        return pull_df

    monkeypatch.setattr(repo, "get_etf_symbol_set", lambda: set())
    monkeypatch.setattr(repo, "get_minute_batch", fake_get_minute_batch)
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", fake_sync)


def test_pull_failure_falls_back_to_local_rows(repo, monkeypatch):
    """本地 30 根 (尾部落后) + 补拉为空 → 回退返回本地行并标记 truncated。"""
    from app.api import kline as kline_api

    calls: list = []
    _pin_clock(repo, monkeypatch, {_SYM: _bars(_SYM, _dt.datetime(2026, 10, 23, 9, 30), 30)},
               pl.DataFrame(), calls)

    out = json.loads(kline_api.get_minute_batch(_fake_req(repo), {"symbols": [_SYM]}).body)
    rows = out["data"].get(_SYM)
    assert rows, "补拉失败时本地已有K必须回退返回, 不得整只消失"
    assert len(rows) == 30
    assert rows[-1]["datetime"] == _dt.datetime(2026, 10, 23, 9, 59).isoformat()
    assert out.get("truncated") == [_SYM], "残缺快照必须带 truncated 标记"


def test_pull_success_merges_without_truncated(repo, monkeypatch):
    """补拉成功 → 本地+拉取 upsert 合并, 无 truncated 标记 (正常路径不变)。"""
    from app.api import kline as kline_api

    calls: list = []
    _pin_clock(repo, monkeypatch, {_SYM: _bars(_SYM, _dt.datetime(2026, 10, 23, 9, 30), 30)},
               _bars(_SYM, _dt.datetime(2026, 10, 23, 10, 0), 30), calls)

    out = json.loads(kline_api.get_minute_batch(_fake_req(repo), {"symbols": [_SYM]}).body)
    rows = out["data"][_SYM]
    assert len(rows) == 60
    assert rows[0]["datetime"] == _dt.datetime(2026, 10, 23, 9, 30).isoformat()
    assert rows[-1]["datetime"] == _dt.datetime(2026, 10, 23, 10, 29).isoformat()
    assert "truncated" not in out


def test_fresh_local_skips_pull_and_truncated(repo, monkeypatch):
    """本地已够新鲜 (90 根 ≥ expected-2) → 不补拉、直接本地, 不进兜底。"""
    from app.api import kline as kline_api

    calls: list = []
    _pin_clock(repo, monkeypatch, {_SYM: _bars(_SYM, _dt.datetime(2026, 10, 23, 9, 30), 90)},
               pl.DataFrame(), calls)

    out = json.loads(kline_api.get_minute_batch(_fake_req(repo), {"symbols": [_SYM]}).body)
    assert len(out["data"][_SYM]) == 90
    assert calls == [], "新鲜路径不应触发补拉"
    assert "truncated" not in out


def test_no_local_rows_stays_absent(repo, monkeypatch):
    """无本地行 + 补拉为空 → 结果中不出现 (无可回退, 不虚构数据)。"""
    from app.api import kline as kline_api

    calls: list = []
    _pin_clock(repo, monkeypatch, {}, pl.DataFrame(), calls)

    out = json.loads(kline_api.get_minute_batch(_fake_req(repo), {"symbols": [_SYM]}).body)
    assert _SYM not in out["data"]
    assert "truncated" not in out


def test_since_filter_drops_symbol_and_truncated_marker(repo, monkeypatch):
    """since 晚于本地最后一根 → 增量语义下该标的无新行, 不回传也不标记 truncated。"""
    from app.api import kline as kline_api

    calls: list = []
    _pin_clock(repo, monkeypatch, {_SYM: _bars(_SYM, _dt.datetime(2026, 10, 23, 9, 30), 30)},
               pl.DataFrame(), calls)

    out = json.loads(kline_api.get_minute_batch(
        _fake_req(repo), {"symbols": [_SYM], "since": "2026-10-23T10:50:00"},
    ).body)
    assert _SYM not in out["data"]
    assert "truncated" not in out, "标记必须与 data 键一致, 不回传的标的不得标记"
