"""交易所口径异动监测 — 聚焦契约测试。

覆盖: 阈值边界(含等号)/方向与窗口/板块与ST/指数缺失不伪零/边缘触发 re-arm/
scope 过滤(含 watchlist_group)/API 过滤。全部使用纯数据或 tmp_path,
不写 data/、不依赖真实 DuckDB/网络。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import abnormal as abnormal_api
from app.services import abnormal_moves as am
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine


# ── 测试数据构造 ───────────────────────────────────────


def _trading_dates(n: int, end: date | None = None) -> list[date]:
    """生成 n 个连续工作日 (周六日跳过, 与交易日近似)。"""
    end = end or date(2026, 8, 22)
    out: list[date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def _make_series(dates: list[date], stock: list[float], index: list[float]):
    assert len(stock) == len(index) == len(dates)
    return (
        {d: s for d, s in zip(dates, stock)},
        {d: i for d, i in zip(dates, index)},
    )


# ── 1. 板块识别 ────────────────────────────────────────


@pytest.mark.parametrize(
    "symbol,board",
    [
        ("600000.SH", "主板"),
        ("000001.SZ", "主板"),
        ("300750.SZ", "创业板"),
        ("301001.SZ", "创业板"),
        ("688981.SH", "科创板"),
        ("832000.BJ", "北交所"),
        ("430047.BJ", "北交所"),
        ("920001.BJ", "北交所"),
    ],
)
def test_board_for_symbol(symbol, board):
    assert am.board_for_symbol(symbol) == board


def test_benchmark_mapping():
    assert am.benchmark_for_symbol("600000.SH") == "000001.INDEX"
    assert am.benchmark_for_symbol("000001.SZ") == "399001.INDEX"
    assert am.benchmark_for_symbol("300750.SZ") == "399006.INDEX"
    assert am.benchmark_for_symbol("688981.SH") == "000680.INDEX"
    assert am.benchmark_for_symbol("832000.BJ") == "899050.INDEX"


# ── 2. 阈值边界 (含等号) ───────────────────────────────


def test_threshold_boundary_inclusive_3d_main_board():
    # 主板 3日: 偏离恰好 20% → ratio=1.0 → triggered (等号包含)
    dates = _trading_dates(3)
    stock, index = _make_series(dates, [8.0, 8.0, 8.0], [1.33, 1.33, 1.34])
    rows = am.build_rows({"600000.SH": stock}, {"000001.INDEX": index})
    row = next(r for r in rows if r["window"] == "3d")
    assert row["deviation_pct"] == pytest.approx(20.0, abs=0.2)
    assert row["status"] == "triggered"
    assert row["direction"] == "up"


def test_threshold_boundary_below_not_triggered():
    # 主板 3日: 偏离 19.9% → ratio≈0.995 → edge (非 triggered)
    dates = _trading_dates(3)
    stock, index = _make_series(dates, [7.0, 7.0, 6.0], [0.37, 0.37, 0.4])
    rows = am.build_rows({"600000.SH": stock}, {"000001.INDEX": index})
    row = next(r for r in rows if r["window"] == "3d")
    assert row["deviation_pct"] < 20.0
    assert row["status"] != "triggered"


def test_monitor_threshold_uses_unrounded_ratio():
    dates = _trading_dates(3)
    daily_return = (20.0 - 0.0000005) / 3
    stock, index = _make_series(
        dates,
        [daily_return] * 3,
        [0.0] * 3,
    )
    rows = am.build_rows({"600000.SH": stock}, {"000001.INDEX": index})
    row = next(r for r in rows if r["window"] == "3d")
    assert row["ratio"] < 1.0

    engine = _engine_with_loader(
        lambda: ({"600000.SH": stock}, {"000001.INDEX": index}, {}, {}),
    )
    assert engine.evaluate_abnormal() == []
    assert set(engine._abnormal_state.values()) == {False}


@pytest.mark.parametrize(
    "board,symbol,bench,thr",
    [
        ("主板", "600000.SH", "000001.INDEX", 20.0),
        ("创业板", "300750.SZ", "399006.INDEX", 30.0),
        ("科创板", "688981.SH", "000680.INDEX", 30.0),
        ("北交所", "832000.BJ", "899050.INDEX", 40.0),
    ],
)
def test_board_thresholds_3d(board, symbol, bench, thr):
    dates = _trading_dates(3)
    per = (thr + 5) / 3  # 每日跑赢, 总偏离 = thr+5
    stock, index = _make_series(dates, [per] * 3, [0.0] * 3)
    rows = am.build_rows({symbol: stock}, {bench: index})
    row = next(r for r in rows if r["window"] == "3d")
    assert row["board"] == board
    assert row["threshold_pct"] == thr
    assert row["status"] == "triggered"
    assert row["benchmark_symbol"] == bench


@pytest.mark.parametrize(
    "window,up_thr,down_thr",
    [
        (10, 100.0, 50.0),
        (30, 200.0, 70.0),
    ],
)
def test_severe_thresholds_by_direction(window, up_thr, down_thr):
    n = window
    dates = _trading_dates(n)
    up_stock, index = _make_series(dates, [(up_thr + 10) / n] * n, [0.0] * n)
    down_stock, _ = _make_series(dates, [-(down_thr + 10) / n] * n, [0.0] * n)
    up_rows = am.build_rows({"600000.SH": up_stock}, {"000001.INDEX": index})
    down_rows = am.build_rows({"600000.SH": down_stock}, {"000001.INDEX": index})
    up_row = next(r for r in up_rows if r["window"] == f"{window}d")
    down_row = next(r for r in down_rows if r["window"] == f"{window}d")
    assert up_row["threshold_pct"] == up_thr and up_row["direction"] == "up"
    assert down_row["threshold_pct"] == down_thr and down_row["direction"] == "down"
    assert up_row["status"] == "triggered" and down_row["status"] == "triggered"


def test_status_ratio_bands():
    assert am.status_for_ratio(1.0) == "triggered"
    assert am.status_for_ratio(0.7) == "edge"
    assert am.status_for_ratio(0.5) == "watch"
    assert am.status_for_ratio(0.49) == "normal"


# ── 3. deviate 定义: 逐日差值和, 非累计收益差 ───────────


def test_deviation_is_daily_diff_sum_not_cumulative():
    # 个股三日 +10%/+10%/+10% (累计 33.1%), 指数 0: 偏离=30 不是 33.1
    dates = _trading_dates(3)
    stock, index = _make_series(dates, [10.0, 10.0, 10.0], [0.0, 0.0, 0.0])
    rows = am.build_rows({"600000.SH": stock}, {"000001.INDEX": index})
    row = next(r for r in rows if r["window"] == "3d")
    assert row["deviation_pct"] == pytest.approx(30.0, abs=0.01)


def test_daily_returns_prefer_raw_close_over_adjusted_close():
    dates = _trading_dates(2)
    rows = [
        {"date": dates[0], "raw_close": 10.0, "close": 5.0},
        {"date": dates[1], "raw_close": 10.5, "close": 10.5},
    ]

    returns = am._series_to_points(rows)

    assert returns[dates[1]] == pytest.approx(5.0)


# ── 4. ST 标记与过滤 ──────────────────────────────────


def test_st_marked_and_filtered():
    dates = _trading_dates(3)
    stock, index = _make_series(dates, [8.0] * 3, [0.0] * 3)
    rows = am.build_rows(
        {"600000.SH": stock, "000001.SZ": stock},
        {"000001.INDEX": index, "399001.INDEX": index},
        {"600000.SH": "浦发银行", "000001.SZ": "*ST 平安"},
    )
    by_sym = {r["symbol"]: r for r in rows if r["window"] == "3d"}
    assert by_sym["000001.SZ"]["is_st"] is True
    assert by_sym["600000.SH"]["is_st"] is False
    filtered = am.filter_rows(rows, hide_st=True)
    assert all(not r["is_st"] for r in filtered)
    assert {r["symbol"] for r in filtered} == {"600000.SH"}


def test_st_uses_board_threshold_no_extra_cut():
    # ST 创业板股仍用 30% 阈值 (不额外降低)
    dates = _trading_dates(3)
    stock, index = _make_series(dates, [11.0] * 3, [0.0] * 3)
    rows = am.build_rows(
        {"300750.SZ": stock},
        {"399006.INDEX": index},
        {"300750.SZ": "ST 股"},
    )
    row = next(r for r in rows if r["window"] == "3d")
    assert row["is_st"] is True
    assert row["threshold_pct"] == 30.0
    assert row["status"] == "triggered"  # 33 > 30, 不因 ST 降阈值而漏报


# ── 5. 指数缺失不伪零 ─────────────────────────────────


def test_missing_benchmark_skips_window_no_fake_zero():
    dates = _trading_dates(3)
    stock, _ = _make_series(dates, [10.0] * 3, [0.0] * 3)
    # 北交所基准 899050 完全缺失 → 该股无任何窗口行, 绝不出 deviation=个股值
    rows = am.build_rows({"832000.BJ": stock}, {})
    assert rows == []


def test_partial_benchmark_date_missing_skips_window():
    dates = _trading_dates(3)
    stock, index = _make_series(dates, [10.0] * 3, [0.0] * 3)
    bench = dict(index)
    bench.pop(dates[-1])  # 最后一日指数缺失
    rows = am.build_rows({"600000.SH": stock}, {"000001.INDEX": bench})
    assert all(r["window"] != "3d" for r in rows)


def test_window_uses_latest_market_days_and_skips_suspension_gap():
    dates = _trading_dates(4)
    # 个股缺少最近 3 个市场交易日中的第一个，但有更早的成交日；不得拼成 3 日窗口。
    stock = {dates[0]: 8.0, dates[2]: 8.0, dates[3]: 8.0}
    benchmark = {d: 0.0 for d in dates}

    rows = am.build_rows(
        {"600000.SH": stock},
        {"000001.INDEX": benchmark},
    )

    assert all(row["window"] != "3d" for row in rows)


def test_build_overview_missing_benchmark_warning():
    class _Repo:
        def get_enriched_range(self, *a, **k):
            return pl.DataFrame()

        def get_index_daily(self, *a, **k):
            return pl.DataFrame()

        def get_instruments(self):
            return pl.DataFrame()

    out = am.build_overview(_Repo(), None)
    assert out["rows"] == []
    assert any("本地无个股" in w for w in out["warnings"])


# ── 6. 窗口产出与方向过滤 ──────────────────────────────


def test_window_coverage_and_direction_filter():
    dates = _trading_dates(30)
    stock, index = _make_series(dates, [2.0] * 30, [0.0] * 30)
    rows = am.build_rows({"600000.SH": stock}, {"000001.INDEX": index})
    windows = {r["window"] for r in rows}
    assert windows == {"3d", "10d", "30d"}
    assert all(r["direction"] == "up" for r in rows)
    down = am.filter_rows(rows, direction="down")
    assert down == []


def test_insufficient_history_no_row():
    dates = _trading_dates(2)
    stock, index = _make_series(dates, [10.0] * 2, [0.0] * 2)
    rows = am.build_rows({"600000.SH": stock}, {"000001.INDEX": index})
    assert rows == []


# ── 7. 边缘触发状态机 (prime/no-repeat/re-arm/清状态) ──


def _engine_with_loader(loader, **rule_kw):
    e = MonitorRuleEngine()
    e.set_abnormal_loader(loader)
    rule = {
        "id": "ab1",
        "name": "异动",
        "type": "abnormal",
        "enabled": True,
        "scope": "all",
        "direction": "both",
        "abnormal_window": "any",
        "threshold_pct": 100,
        "cooldown_seconds": 0,
    }
    rule.update(rule_kw)
    e.set_rules([rule])
    return e


def _empty_df() -> pl.DataFrame:
    return pl.DataFrame(schema={"symbol": pl.Utf8, "close": pl.Float64})


def test_edge_trigger_prime_no_repeat_rearm():
    dates = _trading_dates(3)
    hot_stock, index = _make_series(dates, [8.0] * 3, [0.0] * 3)  # 24% > 20%
    cool_stock, _ = _make_series(dates, [1.0] * 3, [0.0] * 3)  # 3% < 20%
    state = {"hot": True}

    def loader():
        s = hot_stock if state["hot"] else cool_stock
        return {"600000.SH": s}, {"000001.INDEX": index}, {"600000.SH": "浦发"}, {}

    e = _engine_with_loader(loader)
    assert e.evaluate_abnormal() == []  # 首次: prime 不告警
    assert e.evaluate_abnormal() == []  # 持续越线: 不重复
    state["hot"] = False
    assert e.evaluate_abnormal() == []  # 降回 false: re-arm
    state["hot"] = True
    evs = e.evaluate_abnormal()  # 再次越线: 告警
    assert len(evs) == 1
    assert evs[0]["source"] == "abnormal"
    assert "交易所规则近似监测" in evs[0]["message"]
    assert e.evaluate_abnormal() == []  # 又持续: 不再告警


def test_direction_change_rearms_selected_direction():
    dates = _trading_dates(3)
    up_stock, index = _make_series(dates, [8.0] * 3, [0.0] * 3)
    down_stock, _ = _make_series(dates, [-8.0] * 3, [0.0] * 3)
    state = {"direction": "up"}

    def loader():
        stock = up_stock if state["direction"] == "up" else down_stock
        return {"600000.SH": stock}, {"000001.INDEX": index}, {}, {}

    engine = _engine_with_loader(loader, direction="up")
    assert engine.evaluate(_empty_df()) == []
    assert engine._abnormal_state == {}
    assert engine.evaluate_abnormal() == []  # 上涨首次越线仅 prime
    state["direction"] = "down"
    assert engine.evaluate_abnormal() == []  # 反方向观测将同窗口 re-arm
    state["direction"] = "up"
    assert len(engine.evaluate_abnormal()) == 1


def test_edge_trigger_threshold_multiplier():
    # threshold_pct=150: 需 ratio*100>=150 才算越线; 24/20=1.2 不够 → 永不告警
    dates = _trading_dates(3)
    stock, index = _make_series(dates, [8.0] * 3, [0.0] * 3)
    e = _engine_with_loader(
        lambda: ({"600000.SH": stock}, {"000001.INDEX": index}, {}, {}),
        threshold_pct=150,
    )
    assert e.evaluate_abnormal() == []  # prime
    assert e.evaluate_abnormal() == []  # 未越线, 状态 False
    assert e.evaluate_abnormal() == []  # 仍 False, 无边缘
    # 升到 30% 偏离 (ratio 1.5, 边界含等号): False→True 边缘 → 告警
    big = {d: 10.0 for d in dates}
    e._abnormal_loader = lambda: ({"600000.SH": big}, {"000001.INDEX": index}, {}, {})
    assert len(e.evaluate_abnormal()) == 1


def test_rule_update_clears_state():
    dates = _trading_dates(3)
    stock, index = _make_series(dates, [8.0] * 3, [0.0] * 3)
    e = _engine_with_loader(
        lambda: ({"600000.SH": stock}, {"000001.INDEX": index}, {}, {}),
    )
    e.evaluate_abnormal()  # prime True
    assert e._abnormal_state
    e.add_rule(
        {
            "id": "ab1",
            "name": "异动",
            "type": "abnormal",
            "enabled": True,
            "scope": "all",
            "direction": "both",
            "abnormal_window": "any",
            "threshold_pct": 100,
        }
    )
    assert e._abnormal_state == {}  # 更新清状态 → 下一轮重新 prime
    e.remove_rule("ab1")
    assert e._abnormal_state == {}
    assert not e.has_abnormal_rules


def test_set_rules_clears_abnormal_edge_state():
    dates = _trading_dates(3)
    stock, index = _make_series(dates, [8.0] * 3, [0.0] * 3)
    engine = _engine_with_loader(
        lambda: ({"600000.SH": stock}, {"000001.INDEX": index}, {}, {}),
    )
    engine.evaluate_abnormal()
    assert engine._abnormal_state

    engine.set_rules(list(engine.rules.values()))

    assert engine._abnormal_state == {}
    assert engine.evaluate_abnormal() == []


def test_scope_symbols_and_unknown_fail_closed():
    dates = _trading_dates(3)
    stock, index = _make_series(dates, [8.0] * 3, [0.0] * 3)
    data = (
        {"600000.SH": stock, "000002.SZ": stock},
        {"000001.INDEX": index, "399001.INDEX": index},
        {},
        {},
    )
    e = _engine_with_loader(lambda: data, scope="symbols", symbols=["000002.SZ"])
    e.evaluate_abnormal()
    e.evaluate_abnormal()
    assert set(e._abnormal_state) == {("ab1", "000002.SZ", "3d")}

    e2 = _engine_with_loader(lambda: data, scope="galaxy")
    assert e2.evaluate_abnormal() == []  # 未知 scope fail-closed


def test_scope_watchlist_group_missing_fails_closed(tmp_path):
    dates = _trading_dates(3)
    stock, index = _make_series(dates, [8.0] * 3, [0.0] * 3)
    data = ({"600000.SH": stock}, {"000001.INDEX": index}, {}, {})
    e = _engine_with_loader(
        lambda: data,
        scope="watchlist_group",
        group_id="g_missing",
    )
    e.set_data_dir(tmp_path)  # 空目录: 无任何分组 → 分组缺失 fail-closed
    assert e.evaluate_abnormal() == []
    assert e._abnormal_state == {}


def test_scope_watchlist_group_members_only(tmp_path):
    # 真实分组存储: 建组 + 只把 000002.SZ 加入组
    from app.services import watchlist as wl

    groups, g = wl.create_group("组一", data_dir=tmp_path)
    wl.add("000002.SZ", data_dir=tmp_path)
    wl.add_to_group("000002.SZ", g["id"], data_dir=tmp_path)
    dates = _trading_dates(3)
    stock, index = _make_series(dates, [8.0] * 3, [0.0] * 3)
    data = (
        {"600000.SH": stock, "000002.SZ": stock},
        {"000001.INDEX": index, "399001.INDEX": index},
        {},
        {},
    )
    e = _engine_with_loader(
        lambda: data,
        scope="watchlist_group",
        group_id=g["id"],
    )
    e.set_data_dir(tmp_path)
    e.evaluate_abnormal()
    e.evaluate_abnormal()
    assert set(e._abnormal_state) == {("ab1", "000002.SZ", "3d")}


def test_validate_abnormal_rule():
    ok = monitor_rules.normalize(
        {
            "id": "ab_ok",
            "name": "x",
            "type": "abnormal",
            "scope": "all",
            "direction": "up",
            "abnormal_window": "3d",
            "threshold_pct": 100,
        }
    )
    monitor_rules.validate(ok)
    assert ok["direction"] == "up"

    for bad in [
        {"threshold_pct": 49},
        {"threshold_pct": 151},
        {"abnormal_window": "5d"},
        {"direction": "entry"},
    ]:
        rule = {"id": "ab_bad", "name": "x", "type": "abnormal", "scope": "all"}
        rule.update(bad)
        with pytest.raises(ValueError):
            monitor_rules.validate(monitor_rules.normalize(rule))


# ── 9. API 过滤与 fail-soft ────────────────────────────


class _StubRepo:
    """最小 repo 桩: 只实现 load_inputs 用到的三个方法, 纯内存数据。"""

    def __init__(self, stock_df, index_df, instruments=None):
        self._stock = stock_df
        self._index = index_df
        self._inst = (
            instruments
            if instruments is not None
            else pl.DataFrame(
                {"symbol": [], "name": []},
                schema={"symbol": pl.Utf8, "name": pl.Utf8},
            )
        )

    def get_enriched_range(self, start, end, columns=None, **kw):
        return self._stock

    def get_index_daily(self, symbol, start, end, columns=None):
        if "symbol" in self._index.columns:
            return self._index.filter(pl.col("symbol") == symbol)
        return pl.DataFrame()

    def get_instruments(self):
        return self._inst


def _api_client(repo):
    app = FastAPI()
    app.state.repo = repo
    app.state.quote_service = None
    app.include_router(abnormal_api.router)
    return TestClient(app)


def test_api_overview_filter_and_fail_soft():
    dates = _trading_dates(3)
    stock = pl.DataFrame(
        {
            "symbol": ["600000.SH"] * 3,
            "date": dates,
            "change_pct": [0.08, 0.08, 0.08],  # 小数 → 8pp
        }
    )
    index = pl.DataFrame(
        {
            "symbol": ["000001.INDEX"] * 3,
            "date": dates,
            "change_pct": [0.0] * 3,
        }
    )
    client = _api_client(_StubRepo(stock, index))

    r = client.get("/api/abnormal/overview", params={"hide_st": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["disclaimer"].startswith("交易所规则近似监测")
    assert len(body["rows"]) == 1
    assert body["total"] == 1
    row = body["rows"][0]
    assert row["status"] == "triggered"
    assert row["board"] == "主板"
    assert row["benchmark_symbol"] == "000001.INDEX"
    assert row["benchmark_available"] is True
    for key in (
        "symbol",
        "name",
        "board",
        "is_st",
        "window",
        "direction",
        "deviation_pct",
        "threshold_pct",
        "ratio",
        "status",
        "benchmark_symbol",
        "benchmark_available",
    ):
        assert key in row

    # status 过滤: triggered 命中 / normal 空
    assert client.get("/api/abnormal/overview", params={"status": "triggered"}).json()["rows"]
    assert client.get("/api/abnormal/overview", params={"status": "normal"}).json()["rows"] == []
    # direction 过滤
    assert client.get("/api/abnormal/overview", params={"direction": "up"}).json()["rows"]
    assert client.get("/api/abnormal/overview", params={"direction": "down"}).json()["rows"] == []
    # board 过滤
    assert client.get("/api/abnormal/overview", params={"board": "主板"}).json()["rows"]
    assert client.get("/api/abnormal/overview", params={"board": "北交所"}).json()["rows"] == []


def test_api_fail_soft_empty_data():
    empty = pl.DataFrame(
        {"symbol": [], "date": [], "change_pct": []},
        schema={"symbol": pl.Utf8, "date": pl.Date, "change_pct": pl.Float64},
    )
    client = _api_client(_StubRepo(empty, empty))
    r = client.get("/api/abnormal/overview")
    body = r.json()
    assert body["rows"] == []
    assert body["warnings"], "缺数据必须返回明确 warnings, 不伪造 0"


def test_api_repo_unavailable():
    app = FastAPI()
    app.include_router(abnormal_api.router)
    client = TestClient(app)
    body = client.get("/api/abnormal/overview").json()
    assert body["rows"] == []
    assert body["warnings"]
