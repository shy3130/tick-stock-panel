"""龙虎榜服务测试 (不依赖真实网络)。

覆盖: 交易日回退 (非交易日/目标日解析)、历史日 JSON 缓存命中与落盘、
fuyao 未配置降级、当日未发布 fallback_prev、彻底失败 no_data、
AI 复盘摘要段构建。
日期用 2026-08-27/28 (写作时为过去交易日), 与仓库既有绝对日期测试风格一致。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.plugins.fuyao.client import FuyaoError
from app.services import dragon_tiger as dt


def _mk_days(data_dir: Path, *days: str) -> None:
    for d in days:
        (data_dir / "kline_daily" / f"date={d}").mkdir(parents=True, exist_ok=True)


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    _mk_days(tmp_path, "2026-08-26", "2026-08-27", "2026-08-28")
    return tmp_path


class _FakeProvider:
    """按 (board, date) 记录调用; fail_dates 中的日期抛 FuyaoError。"""

    def __init__(self, fail_dates: set[str] | None = None):
        self.calls: list[tuple] = []
        self.fail_dates = fail_dates or set()

    def dragon_tiger(self, board_type: str, date: str | None) -> dict:
        self.calls.append((board_type, date))
        if date in self.fail_dates:
            raise FuyaoError(f"code=3002: {date} 未就绪")
        iso = date or "2026-08-28"
        return {
            "trade_date": iso,
            "count": 2,
            "stock_count": 2,
            "stock_items": [
                {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
                 "change": 0.05, "net_value": 1.5e8, "net_rate": 0.01,
                 "buy_value": 2e8, "sell_value": 0.5e8, "hot_rank": 3, "range_days": 1,
                 "org_net_value": 0.8e8},
                {"thscode": "000858.SZ", "ticker": "000858", "name": "五粮液",
                 "change": -0.03, "net_value": -0.6e8, "net_rate": -0.004,
                 "buy_value": 0.4e8, "sell_value": 1e8, "range_days": 3},
            ],
            "hot_money_items": [
                {"name": "宁波桑田路", "buying": 1.9e8,
                 "rows": [{"thscode": "600519.SH", "name": "贵州茅台",
                           "hot_money_item_net_value": 1.2e8}]},
            ],
        }


def _use_provider(monkeypatch, provider) -> _FakeProvider:
    monkeypatch.setattr(dt, "_provider", lambda: provider)
    return provider


# ---- 交易日解析 ----

def test_resolve_rolls_back_non_trading_day(data_dir):
    # 周日 08-30 → 最近分区 08-28
    assert dt.resolve_trade_date(data_dir, date(2026, 8, 30)) == date(2026, 8, 28)
    # 08-27 是交易日 → 原样
    assert dt.resolve_trade_date(data_dir, date(2026, 8, 27)) == date(2026, 8, 27)


def test_resolve_older_than_all_partitions_returns_target(data_dir):
    # 早于全部本地分区 → 原样返回, 由调用方显式尝试 (失败如实 no_data)
    assert dt.resolve_trade_date(data_dir, date(2020, 1, 1)) == date(2020, 1, 1)


# ---- 状态与缓存 ----

def test_source_unavailable_without_fuyao(data_dir, monkeypatch):
    monkeypatch.setattr(dt, "_provider", lambda: None)
    out = dt.get_dragon_tiger(data_dir, None)
    assert out["state"] == "source_unavailable"


def test_fetch_stores_cache_then_hits_cache(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider())
    out = dt.get_dragon_tiger(data_dir, None)  # 默认 → 最近分区 08-28
    assert out["state"] == "ok"
    assert out["trade_date"] == "2026-08-28"
    assert len(provider.calls) == 3  # 三榜一次取齐
    cache = data_dir / "dragon_tiger" / "date=2026-08-28.json"
    assert cache.exists()

    provider.calls.clear()
    out2 = dt.get_dragon_tiger(data_dir, date(2026, 8, 30))  # 周日 → 08-28 → 命中缓存
    assert out2["state"] == "ok" and out2["trade_date"] == "2026-08-28"
    assert provider.calls == []  # 纯本地, 不打网络


def test_explicit_history_date_uses_cache(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider())
    dt.get_dragon_tiger(data_dir, date(2026, 8, 27))
    assert provider.calls[0] == ("all", "2026-08-27")
    provider.calls.clear()
    dt.get_dragon_tiger(data_dir, date(2026, 8, 27))
    assert provider.calls == []


def test_unpublished_today_falls_back_to_prev(data_dir, monkeypatch):
    # 08-28 拉取失败 (未发布/未就绪) → 回退 08-27, state=fallback_prev
    provider = _use_provider(monkeypatch, _FakeProvider(fail_dates={"2026-08-28"}))
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert out["state"] == "fallback_prev"
    assert out["trade_date"] == "2026-08-27"
    assert out["requested_date"] == "2026-08-28"
    # 回退日缓存以 ok 状态落盘 (不污染直查)
    cached = json.loads((data_dir / "dragon_tiger" / "date=2026-08-27.json").read_text(encoding="utf-8"))
    assert cached["state"] == "ok"


def test_total_failure_returns_no_data(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider(fail_dates={"2026-08-28", "2026-08-27"}))
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert out["state"] == "no_data"
    assert "2026-08-28" in out.get("message", "")


def test_corrupt_cache_refetches(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider())
    cache = data_dir / "dragon_tiger" / "date=2026-08-28.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{broken json", encoding="utf-8")
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert out["state"] == "ok"
    assert provider.calls  # 缓存损坏 → 重新拉取


# ---- AI 复盘摘要 ----

def test_build_recap_context_contains_summary(data_dir, monkeypatch):
    _use_provider(monkeypatch, _FakeProvider())
    ctx = dt.build_recap_context(data_dir)
    assert "净买入居前" in ctx and "贵州茅台" in ctx
    assert "机构净买居前" in ctx
    assert "宁波桑田路" in ctx


def test_build_recap_context_empty_without_source(data_dir, monkeypatch):
    monkeypatch.setattr(dt, "_provider", lambda: None)
    assert dt.build_recap_context(data_dir) == ""


# ---- thscode 归一化 (2026-10-29 用户反馈: 退市整理期股/B股 thscode=null 进前端) ----

def test_derive_thscode_market_suffix():
    assert dt.derive_thscode("002731") == "002731.SZ"
    assert dt.derive_thscode("300750") == "300750.SZ"
    assert dt.derive_thscode("600519") == "600519.SH"
    assert dt.derive_thscode("688111") == "688111.SH"
    assert dt.derive_thscode("830799") == "830799.BJ"
    assert dt.derive_thscode("900920") == "900920.SH"   # 沪B
    assert dt.derive_thscode("200468") == "200468.SZ"   # 深B
    assert dt.derive_thscode("600519.SH") == "600519.SH"  # 已带后缀原样返回
    assert dt.derive_thscode(None) is None
    assert dt.derive_thscode("ABCDEF") is None


def test_normalize_stock_items_fills_null_thscode_from_ticker():
    items = [
        {"thscode": None, "ticker": "002731", "name": "萃华退", "net_value": -525558.3},
        {"thscode": None, "ticker": "200468", "name": "宁通信B"},
        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"},
    ]
    out = dt.normalize_stock_items(items)
    assert [i["thscode"] for i in out] == ["002731.SZ", "200468.SZ", "600519.SH"]
    # 其它字段原样保留
    assert out[0]["name"] == "萃华退" and out[0]["net_value"] == -525558.3


def test_normalize_stock_items_drops_unidentifiable():
    out = dt.normalize_stock_items([
        {"thscode": None, "ticker": None, "name": "无代码"},
        {"thscode": "  ", "ticker": "nope", "name": "坏ticker"},
        "junk",
    ])
    assert out == []


def test_boards_of_normalizes_nested_seat_rows():
    class _NullProvider:
        def dragon_tiger(self, board_type, date):
            return {
                "trade_date": date,
                "stock_items": [{"thscode": None, "ticker": "900920", "name": "动力B股"}],
                "hot_money_items": [{"name": "T王", "buying": 1.0,
                                     "rows": [{"thscode": None, "ticker": "002731", "name": "萃华退"}]}],
            }

    got = dt._boards_of(_NullProvider(), "all", "2026-10-29")
    assert got["stock_items"][0]["thscode"] == "900920.SH"
    assert got["hot_money_items"][0]["rows"][0]["thscode"] == "002731.SZ"


def test_load_cache_normalizes_legacy_null_thscode(tmp_path):
    """旧版本写入的历史缓存含 null thscode → 读回即归一, 不把脏数据透给前端。"""
    path = tmp_path / "date=2026-10-29.json"
    path.write_text(json.dumps({
        "state": "ok",
        "trade_date": "2026-10-29",
        "all": {"stock_items": [{"thscode": None, "ticker": "002731", "name": "萃华退"}]},
        "org": {"stock_items": [{"thscode": "600519.SH"}]},
        "hot_money": {"hot_money_items": [
            {"name": "T王", "rows": [{"thscode": None, "ticker": "200468"}]},
        ]},
    }, ensure_ascii=False), encoding="utf-8")

    payload = dt._load_cache(path)
    assert payload["all"]["stock_items"][0]["thscode"] == "002731.SZ"
    assert payload["org"]["stock_items"][0]["thscode"] == "600519.SH"
    assert payload["hot_money"]["hot_money_items"][0]["rows"][0]["thscode"] == "200468.SZ"


def test_get_dragon_tiger_normalizes_provider_null_thscode(data_dir, monkeypatch):
    class _NullProvider:
        def dragon_tiger(self, board_type, date):
            return {
                "trade_date": "2026-08-28",
                "count": 1,
                "stock_count": 1,
                "stock_items": [{"thscode": None, "ticker": "002731", "name": "萃华退",
                                 "net_value": -525558.3}],
                "hot_money_items": [],
            }

    _use_provider(monkeypatch, _NullProvider())
    payload = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert payload["state"] == "ok"
    assert payload["all"]["stock_items"][0]["thscode"] == "002731.SZ"
