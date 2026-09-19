"""异动边缘统计测试 — 偏离列附着 + 规则口径 + 快照接近度。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.indicators.pipeline import (
    attach_deviation_columns,
    attach_deviation_columns_today,
    benchmark_momentum_today,
    load_benchmark_momentum,
)
from app.services.abnormal_moves import (
    _bench_rt_pct,
    _hist_cache,
    _hist_cache_lock,
    board_of,
    build_overview,
    is_st_name,
    rule_for,
)


def _write_index_daily(tmp_path, rows: list[tuple[str, date, float]]) -> None:
    df = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [r[1] for r in rows],
            "close": [r[2] for r in rows],
        }
    )
    for dt in sorted({r[1] for r in rows}):
        target = tmp_path / "kline_index_daily" / f"date={dt.isoformat()}"
        target.mkdir(parents=True, exist_ok=True)
        df.filter(pl.col("date") == dt).write_parquet(target / "part.parquet")


def test_attach_deviation_columns_math(tmp_path) -> None:
    # 上证指数 4 天等差 +1: 3日动量 = 13/10-1 = 0.30
    # 个股 close 与指数同序列 → momentum_3d 缺失时按 close 就地补算, 偏离 = 0
    days = [date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 15), date(2026, 8, 18)]
    index_rows = [("000001.SH", d, 10.0 + i) for i, d in enumerate(days)]
    _write_index_daily(tmp_path, index_rows)

    stock = pl.DataFrame(
        {
            "symbol": ["600000.SH"] * len(days),
            "date": days,
            "close": [10.0 + i for i in range(len(days))],
            # 10/30 日窗口已有动量列 → 直接使用
            "momentum_10d": [None] * 4,
            "momentum_30d": [None] * 4,
        }
    )
    out = attach_deviation_columns(stock, tmp_path)
    assert "deviate_3d" in out.columns
    assert "momentum_3d" in out.columns  # 就地补算
    last = out.sort("date").row(-1, named=True)
    assert abs(last["deviate_3d"] - 0.0) < 1e-9


def test_attach_deviation_columns_missing_benchmark(tmp_path) -> None:
    # 无指数数据: 偏离列为 null, 不抛异常
    stock = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [date(2026, 8, 18)],
            "momentum_3d": [0.2],
            "momentum_10d": [0.5],
            "momentum_30d": [1.0],
        }
    )
    out = attach_deviation_columns(stock, tmp_path)
    assert out["deviate_3d"][0] is None


# ── 盘中路径: 今日基准动量外推 + 单日帧偏离附着 ──────────────────

_BENCH_DAYS = [date(2026, 8, 11), date(2026, 8, 12), date(2026, 8, 13),
               date(2026, 8, 14), date(2026, 8, 15), date(2026, 8, 18)]


def _write_sh_bench(tmp_path) -> None:
    # 上证指数 6 日收盘 10..15, 末值 15 为昨收
    _write_index_daily(tmp_path, [("000001.SH", d, 10.0 + i) for i, d in enumerate(_BENCH_DAYS)])


def test_benchmark_momentum_today_math(tmp_path) -> None:
    _write_sh_bench(tmp_path)
    # 指数展示缓存为百分数口径 (quote_service._build_index_quotes 已 x100),
    # 10.0 表示 +10%; 早期测试直接传小数 0.10 绕过了该契约, 未覆盖 #232
    quotes = pl.DataFrame({"symbol": ["000001.SH"], "change_pct": [10.0]})

    out = benchmark_momentum_today(tmp_path, quotes)
    row = out.row(0, named=True)
    # 今收 = 15 x 1.10 = 16.5; 3 个交易日前的收盘 = 13 (与全量路径 shift(3) 同口径)
    # mom3d = 16.5/13 - 1
    assert abs(row["bench_mom3d"] - (16.5 / 13 - 1)) < 1e-9
    # 10/30 日窗口收盘数不足 → null
    assert row["bench_mom10d"] is None
    assert row["bench_mom30d"] is None

    # 无实时行情 → rt 按 0 处理: mom3d = 15/13 - 1
    out0 = benchmark_momentum_today(tmp_path, None)
    assert abs(out0.row(0, named=True)["bench_mom3d"] - (15.0 / 13 - 1)) < 1e-9


def test_benchmark_momentum_today_percent_not_treated_as_decimal(tmp_path) -> None:
    """#232 回归: 百分数 -1.88 (实际 -1.88%) 不得被当小数 (否则 1+rt=-0.88,
    构造出负的指数点位, 偏离值被放大两个数量级)。"""
    _write_sh_bench(tmp_path)
    quotes = pl.DataFrame({"symbol": ["000001.SH"], "change_pct": [-1.88]})

    out = benchmark_momentum_today(tmp_path, quotes)
    row = out.row(0, named=True)
    # 今收 = 15 x (1 - 0.0188) = 14.718; mom3d = 14.718/13 - 1
    assert abs(row["bench_mom3d"] - (15.0 * 0.9812 / 13 - 1)) < 1e-9


def test_benchmark_momentum_today_excludes_today_rows(tmp_path) -> None:
    # 指数监控盘写入的今日行不能当昨收 (否则实时涨跌被重复叠加)
    today = date.today()
    rows = [("000001.SH", d, 10.0 + i) for i, d in enumerate(_BENCH_DAYS)]
    rows.append(("000001.SH", today, 99.0))  # 今日脏行
    _write_index_daily(tmp_path, rows)

    out = benchmark_momentum_today(tmp_path, None)
    assert abs(out.row(0, named=True)["bench_mom3d"] - (15.0 / 13 - 1)) < 1e-9


def test_attach_deviation_columns_today(tmp_path) -> None:
    _write_sh_bench(tmp_path)
    # 百分数口径: 10.0 = +10% (#232)
    quotes = pl.DataFrame({"symbol": ["000001.SH"], "change_pct": [10.0]})
    # 单日帧: 增量路径产出的 momentum 列 (无 date 历史, 无法 shift 补算)
    today_df = pl.DataFrame(
        {
            "symbol": ["600000.SH", "000001.SZ"],
            "momentum_3d": [0.5, 0.2],
            "momentum_10d": [0.2, None],
            "momentum_30d": [1.0, None],
        }
    )
    out = attach_deviation_columns_today(today_df, tmp_path, quotes)
    # SH: 0.5 - (16.5/13 - 1)
    assert abs(out["deviate_3d"][0] - (0.5 - (16.5 / 13 - 1))) < 1e-9
    # SZ 无深证基准 → 按选基设计回退上证基准 (rt=0): 0.2 - (15/13 - 1)
    assert abs(out["deviate_3d"][1] - (0.2 - (15.0 / 13 - 1))) < 1e-9
    assert "bench_close" not in out.columns


def test_attach_deviation_columns_today_missing_momentum(tmp_path) -> None:
    # 全量回退路径可能缺 momentum_3d: 该窗口置 null, 其余窗口正常
    days = [date(2026, 7, 1) + timedelta(days=i) for i in range(35)]
    _write_index_daily(tmp_path, [("000001.SH", d, 10.0 + i) for i, d in enumerate(days)])
    df = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "momentum_10d": [0.2],
            "momentum_30d": [1.0],
        }
    )
    out = attach_deviation_columns_today(df, tmp_path, None)
    assert out["deviate_3d"][0] is None
    assert out["deviate_10d"][0] is not None
    assert out["deviate_30d"][0] is not None


def test_attach_deviation_columns_no_bench_close_leak(tmp_path) -> None:
    # load_benchmark_momentum 新增 bench_close 列后, 冷路径输出不应泄漏该列
    _write_sh_bench(tmp_path)
    stock = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [date(2026, 8, 18)],
            "close": [15.0],
        }
    )
    out = attach_deviation_columns(stock, tmp_path)
    assert "bench_close" not in out.columns
    frame = load_benchmark_momentum(tmp_path)
    assert "bench_close" in frame.columns


def test_board_and_st_rules() -> None:
    assert board_of("600000.SH") == "主板"
    assert board_of("000001.SZ") == "主板"
    assert board_of("301123.SZ") == "创业板"
    assert board_of("688123.SH") == "科创板"
    assert board_of("920001.BJ") == "北交所"
    assert is_st_name("*ST 某某") is True
    assert is_st_name("正常股") is False

    main = rule_for("600000.SH", "正常股")
    # 3日对称 ±20%; 严重异动负向更严: 10日+100%(-50%), 30日+200%(-70%)
    assert main.thresholds == {3: (0.20, 0.20), 10: (1.00, 0.50), 30: (2.00, 0.70)}
    # 2026-07-06 起主板风险警示股票与普通股票同标准 (原±15%特别规定已废止)
    st = rule_for("600000.SH", "ST 某某")
    assert st.thresholds == main.thresholds
    assert st.st is True
    gem = rule_for("301123.SZ", "正常股")
    assert gem.thresholds[3] == (0.30, 0.30)
    assert gem.thresholds[10] == (1.00, 0.50)
    bse = rule_for("920001.BJ", "正常股")
    assert bse.thresholds[3] == (0.40, 0.40)


class _FakeRepo:
    """最小 repo: get_enriched_latest 返回构造帧。"""

    def __init__(self, df: pl.DataFrame) -> None:
        self._df = df

    def get_enriched_latest(self):
        return self._df, date(2026, 8, 19)


class _FakeQuotes:
    def get_index_quotes(self):
        return pl.DataFrame(
            {"symbol": ["000001.SH"], "close": [3300.0], "prev_close": [3270.0]}
        )


class _PercentQuotes:
    """返回百分数口径的指数缓存 (与 _build_index_quotes 的 x100 输出同形态)。"""

    def get_index_quotes(self):
        return pl.DataFrame({"symbol": ["000001.SH"], "change_pct": [-1.88]})


def test_bench_rt_pct_converts_percent_to_decimal() -> None:
    """#232 回归: _bench_rt_pct 必须把指数展示缓存的百分数转成小数,
    与 enriched 侧小数制 change_pct 同口径参与 rt_delta 计算。"""
    assert abs(_bench_rt_pct(_PercentQuotes()) - (-0.0188)) < 1e-12
    # close/prev_close 兜底路径本身是小数, 不受影响
    assert abs(_bench_rt_pct(_FakeQuotes()) - (3300.0 / 3270.0 - 1)) < 1e-12


def test_build_overview_closeness_and_status() -> None:
    with _hist_cache_lock:
        _hist_cache.clear()
    df = pl.DataFrame(
        {
            "symbol": ["600000.SH", "300001.SZ", "000002.SZ"],
            "name": ["股A", "股B", "股C"],
            "close": [10.0, 20.0, 30.0],
            "change_pct": [0.05, 0.02, 0.01],
            "deviate_3d": [0.19, 0.35, 0.05],
            "deviate_10d": [0.99, 0.40, 0.20],
            "deviate_30d": [1.95, 2.10, 0.60],
        }
    )
    result = build_overview(_FakeRepo(df), _FakeQuotes(), min_closeness=0.5, limit=10)

    by_symbol = {r["symbol"]: r for r in result["rows"]}
    # 主板: 3d阈值0.2 → 0.19/0.2=0.95 边缘; 指数实时 +30/3270≈0.00917 叠加后略增
    a = by_symbol["600000.SH"]
    assert a["status"] in ("edge", "triggered")
    # 创业板: 30日 2.10/2.00 ≥ 1 → triggered
    b = by_symbol["300001.SZ"]
    assert b["status"] == "triggered"
    # 000002: 3d 0.05/0.2=0.25, 10d 0.2/1=0.2, 30d 0.6/2=0.3 → 全部 < 0.5 被过滤
    assert "000002.SZ" not in by_symbol
    # 排序按接近度降序
    closeness = [r["max_closeness"] for r in result["rows"]]
    assert closeness == sorted(closeness, reverse=True)
    assert result["counts"]["triggered"] >= 1


def test_build_overview_weekend_does_not_double_count_last_session(monkeypatch) -> None:
    """休市日没有新的「今日涨跌」; 快照停在最近交易日, 其 change_pct 已在 deviate 内。

    未修复代码只判断 cache_date >= date.today(): 周六/周日 (以及 UTC 主机上
    北京已经跨日、本地还没跨日的窗口) 会把周五涨跌再叠加一遍, 接近度被放大。
    """
    from app.services import abnormal_moves as am

    friday = date(2026, 9, 18)
    saturday = date(2026, 9, 19)
    monkeypatch.setattr(am, "cn_today", lambda: saturday, raising=False)
    with _hist_cache_lock:
        _hist_cache.clear()

    class _FridayRepo(_FakeRepo):
        def get_enriched_latest(self):
            return self._df, friday

    df = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "name": ["股A"],
            "close": [10.0],
            "change_pct": [0.05],
            "deviate_3d": [0.19],
            "deviate_10d": [None],
            "deviate_30d": [None],
        }
    )
    result = build_overview(_FridayRepo(df), _FakeQuotes(), min_closeness=0.5)
    row = result["rows"][0]
    assert abs(row["windows"]["3d"]["value"] - 0.19) < 1e-9, (
        f"周末不应再叠加快照自身的 change_pct, 实际 {row['windows']['3d']['value']}"
    )


def test_build_overview_cache_date_today_no_double_count() -> None:
    """cache_date >= 今天时不再叠加实时涨跌 (避免重复计入)。"""
    with _hist_cache_lock:
        _hist_cache.clear()

    class _TodayRepo(_FakeRepo):
        def get_enriched_latest(self):
            return self._df, date.today()

    df = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "name": ["股A"],
            "close": [10.0],
            "change_pct": [0.05],
            "deviate_3d": [0.19],
            "deviate_10d": [None],
            "deviate_30d": [None],
        }
    )
    result = build_overview(_TodayRepo(df), _FakeQuotes(), min_closeness=0.5)
    row = result["rows"][0]
    assert abs(row["windows"]["3d"]["value"] - 0.19) < 1e-9


def test_build_overview_negative_side_stricter_threshold() -> None:
    """严重异动负向阈值更严 (10日-50%/30日-70%), 跌方向更早触发。"""
    with _hist_cache_lock:
        _hist_cache.clear()

    class _TodayRepo(_FakeRepo):
        def get_enriched_latest(self):
            return self._df, date.today()

    df = pl.DataFrame(
        {
            "symbol": ["600000.SH", "600001.SH"],
            "name": ["跌一", "跌二"],
            "close": [10.0, 20.0],
            "change_pct": [-0.05, -0.05],
            # -0.55: 旧对称口径 0.55/1.00=0.55 (观察); 新口径 0.55/0.50=1.1 (触发)
            # -0.75: 30日 0.75/0.70≈1.07 (触发)
            "deviate_3d": [None, None],
            "deviate_10d": [-0.55, None],
            "deviate_30d": [None, -0.75],
        }
    )
    result = build_overview(_TodayRepo(df), None, min_closeness=0.5)
    by_symbol = {r["symbol"]: r for r in result["rows"]}
    a = by_symbol["600000.SH"]
    assert a["windows"]["10d"]["threshold"] == 0.50
    assert abs(a["windows"]["10d"]["closeness"] - 1.1) < 1e-9
    assert a["status"] == "triggered"
    b = by_symbol["600001.SH"]
    assert b["windows"]["30d"]["threshold"] == 0.70
    assert abs(b["windows"]["30d"]["closeness"] - round(0.75 / 0.7, 4)) < 1e-9
    assert b["status"] == "triggered"
    # 正向阈值不变: +100%/+200% (在正偏离用例中覆盖, 这里验证规则表)
    main = rule_for("600000.SH", "正常股")
    assert main.thresholds[10] == (1.00, 0.50)
    assert main.thresholds[30] == (2.00, 0.70)


# ── 监控规则接入 (type=abnormal) ────────────────────────

import pytest

from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine


def _ab_rule(**overrides) -> dict:
    rule = {
        "id": "r_ab",
        "name": "异动边缘",
        "type": "abnormal",
        "scope": "all",
        "symbols": [],
        "threshold_pct": 70,
        "direction": "both",
        "abnormal_window": "any",
        "cooldown_seconds": 0,
        "severity": "warn",
    }
    rule.update(overrides)
    return rule


def _row(symbol: str, *wins: tuple[str, float], name: str = "股A",
         board: str = "主板", rt_pct: float = 0.05) -> dict:
    # wins: (窗口, 偏离值) — 阈值按交易所口径: 主板 3d=0.2, 10d=1.0, 30d=2.0
    thresholds = {"3d": 0.2, "10d": 1.0, "30d": 2.0}
    windows = {
        key: {"value": value, "threshold": thresholds[key],
              "closeness": round(abs(value) / thresholds[key], 4)}
        for key, value in wins
    }
    return {"symbol": symbol, "name": name, "board": board, "st": False,
            "close": 10.0, "rt_pct": rt_pct, "windows": windows}


def test_abnormal_rule_validation_and_defaults() -> None:
    rule = monitor_rules.normalize({"id": "r1", "name": "n", "type": "abnormal"})
    assert rule["direction"] == "both"
    assert rule["threshold_pct"] == 70.0
    assert rule["abnormal_window"] == "any"
    monitor_rules.validate(rule)

    monitor_rules.validate(_ab_rule(threshold_pct=100, direction="up", abnormal_window="3d"))

    with pytest.raises(ValueError):
        monitor_rules.validate(_ab_rule(abnormal_window="5d"))
    with pytest.raises(ValueError):
        monitor_rules.validate(_ab_rule(threshold_pct=0.5))
    with pytest.raises(ValueError):
        monitor_rules.validate(_ab_rule(asset_type="etf"))
    with pytest.raises(ValueError):
        monitor_rules.validate(_ab_rule(direction="entry"))


def test_engine_abnormal_edge_trigger_and_cooldown() -> None:
    engine = MonitorRuleEngine()
    engine.set_rules([_ab_rule()])
    assert engine.min_abnormal_closeness() == pytest.approx(0.7)

    # 首轮观测不触发 (防新建规则刷屏); 0.10/0.2 = 50% 接近度, 低于阈值
    assert engine.evaluate_abnormal([_row("600000.SH", ("3d", 0.10))], now=1000.0) == []
    # 上穿 70% → 触发 (0.16/0.2 = 80%)
    events = engine.evaluate_abnormal([_row("600000.SH", ("3d", 0.16))], now=1006.0)
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "abnormal"
    assert ev["type"] == "abnormal_up"
    assert ev["symbol"] == "600000.SH"
    assert ev["abnormal_window"] == "3d"
    assert ev["abnormal_closeness"] == pytest.approx(0.8)
    assert "接近" in ev["message"] or "已达" in ev["message"]
    # 持续高于阈值: 不重复触发 (边缘语义)
    assert engine.evaluate_abnormal([_row("600000.SH", ("3d", 0.18))], now=1012.0) == []
    # 回落再上穿: cooldown=0 时再次触发
    engine.evaluate_abnormal([_row("600000.SH", ("3d", 0.10))], now=1018.0)
    assert len(engine.evaluate_abnormal([_row("600000.SH", ("3d", 0.17))], now=1024.0)) == 1

    # cooldown 内的上穿被抑制
    engine_cd = MonitorRuleEngine()
    engine_cd.set_rules([_ab_rule(cooldown_seconds=3600)])
    engine_cd.evaluate_abnormal([_row("600000.SH", ("3d", 0.10))], now=1000.0)
    engine_cd.evaluate_abnormal([_row("600000.SH", ("3d", 0.16))], now=1006.0)
    engine_cd.evaluate_abnormal([_row("600000.SH", ("3d", 0.10))], now=1012.0)
    assert engine_cd.evaluate_abnormal([_row("600000.SH", ("3d", 0.16))], now=1018.0) == []


def test_engine_abnormal_stale_symbol_state_cleared() -> None:
    """标的跌出快照后状态应清回 False, 回升穿过阈值时可再次触发。"""
    engine = MonitorRuleEngine()
    engine.set_rules([_ab_rule()])
    engine.evaluate_abnormal([_row("600000.SH", ("3d", 0.10))], now=1000.0)  # 首轮 False
    assert len(engine.evaluate_abnormal([_row("600000.SH", ("3d", 0.18))], now=1006.0)) == 1
    # 跌出预过滤区间 (快照中消失)
    engine.evaluate_abnormal([], now=1012.0)
    # 重新出现且超阈值 → 重新触发
    assert len(engine.evaluate_abnormal([_row("600000.SH", ("3d", 0.18))], now=1018.0)) == 1


def test_engine_abnormal_direction_window_scope_filters() -> None:
    # 方向: 只报上涨偏离
    engine = MonitorRuleEngine()
    engine.set_rules([_ab_rule(direction="up")])
    engine.evaluate_abnormal([_row("600000.SH", ("3d", -0.16))], now=1000.0)
    assert engine.evaluate_abnormal([_row("600000.SH", ("3d", -0.19))], now=1006.0) == []

    # 窗口: 只看 3d (10d/30d 的偏离不参与)
    engine = MonitorRuleEngine()
    engine.set_rules([_ab_rule(abnormal_window="3d")])
    engine.evaluate_abnormal([_row("600000.SH", ("10d", 0.98))], now=1000.0)
    assert engine.evaluate_abnormal([_row("600000.SH", ("10d", 0.99))], now=1006.0) == []

    # 作用域: 只监控指定标的
    engine = MonitorRuleEngine()
    engine.set_rules([_ab_rule(scope="symbols", symbols=["600000.SH"])])
    engine.evaluate_abnormal(
        [_row("600000.SH", ("3d", 0.10)), _row("000001.SZ", ("3d", 0.10))], now=1000.0,
    )
    events = engine.evaluate_abnormal(
        [_row("600000.SH", ("3d", 0.16)), _row("000001.SZ", ("3d", 0.19))], now=1006.0,
    )
    assert [ev["symbol"] for ev in events] == ["600000.SH"]


def test_engine_abnormal_down_direction_event_type() -> None:
    engine = MonitorRuleEngine()
    engine.set_rules([_ab_rule(direction="down")])
    engine.evaluate_abnormal([_row("600000.SH", ("3d", -0.10))], now=1000.0)
    events = engine.evaluate_abnormal([_row("600000.SH", ("3d", -0.16))], now=1006.0)
    assert len(events) == 1
    assert events[0]["type"] == "abnormal_down"


# ── 板块基准路由: 科创板→科创50, 创业板→创业板综指 (交易所官方对应指数) ──

def test_attach_deviation_columns_board_routing(tmp_path) -> None:
    """科创板股票减科创50、创业板股票减创业板综指, 不再共用交易所 A 指。

    构造 68/30 前缀股票 close 与各自官方基准同序列 → 偏离恰为 0。
    """
    days = [date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 15), date(2026, 8, 18)]
    rows = [("000688.SH", d, 100.0 + i) for i, d in enumerate(days)]       # 科创50
    rows += [("399102.SZ", d, 200.0 + 5 * i) for i, d in enumerate(days)]  # 创业板综指
    _write_index_daily(tmp_path, rows)

    stock = pl.DataFrame(
        {
            "symbol": ["688981.SH"] * 4 + ["300750.SZ"] * 4,
            "date": days * 2,
            "close": [100.0 + i for i in range(4)] + [200.0 + 5 * i for i in range(4)],
        }
    )
    out = attach_deviation_columns(stock, tmp_path)
    star = out.filter(pl.col("symbol") == "688981.SH").sort("date")["deviate_3d"][-1]
    gem = out.filter(pl.col("symbol") == "300750.SZ").sort("date")["deviate_3d"][-1]
    assert abs(star - 0.0) < 1e-9
    assert abs(gem - 0.0) < 1e-9


def test_attach_deviation_columns_star_fallback(tmp_path) -> None:
    """科创50 数据缺失 → 回退上证A指, 偏离列不整体缺失。"""
    days = [date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 15), date(2026, 8, 18)]
    _write_index_daily(tmp_path, [("000002.SH", d, 10.0 + i) for i, d in enumerate(days)])

    stock = pl.DataFrame(
        {
            "symbol": ["688981.SH"] * 4,
            "date": days,
            "close": [10.0 + i for i in range(4)],
        }
    )
    out = attach_deviation_columns(stock, tmp_path)
    dev = out.sort("date")["deviate_3d"][-1]
    assert dev is not None
    assert abs(dev - 0.0) < 1e-9


def test_benchmark_momentum_today_gem_key(tmp_path) -> None:
    """盘中外推按板块键产出: 创业板综指行情只喂 GEM 键的实时涨跌。"""
    _write_index_daily(tmp_path, [("399102.SZ", d, 10.0 + i) for i, d in enumerate(_BENCH_DAYS)])
    quotes = pl.DataFrame({"symbol": ["399102.SZ"], "change_pct": [10.0]})  # 百分数: +10%

    out = benchmark_momentum_today(tmp_path, quotes)
    gem = out.filter(pl.col("bench_key") == "GEM")
    assert gem.height == 1
    assert abs(gem["bench_mom3d"][0] - (15.0 * 1.10 / 13 - 1)) < 1e-9
    # 其他键 (如 SH) 候选不在实时缓存 → rt=0, 用昨收外推
    sh = out.filter(pl.col("bench_key") == "SH")
    assert sh.height == 1
    assert abs(sh["bench_mom3d"][0] - (15.0 / 13 - 1)) < 1e-9


def test_build_overview_rt_overlay_uses_board_benchmark(monkeypatch) -> None:
    """实时叠加按板块基准: 创业板股减创业板综指今日涨跌, 不再全市场混均值。"""
    from app.services import abnormal_moves as am

    # 钉在交易日, 否则周末守卫会关掉叠加, 本例测的是盘中叠加口径。
    monkeypatch.setattr(am, "cn_today", lambda: date(2026, 9, 18), raising=False)
    monkeypatch.setattr(am, "_is_closed_session", lambda: False, raising=False)
    with _hist_cache_lock:
        _hist_cache.clear()

    class _GemQuotes:
        def get_index_quotes(self):
            # 百分数口径: 创业板综指 +2%, 其余基准无行情 → 0
            return pl.DataFrame({"symbol": ["399102.SZ"], "change_pct": [2.0]})

    df = pl.DataFrame(
        {
            "symbol": ["300001.SZ", "600000.SH"],
            "name": ["创业板股", "沪主板股"],
            "close": [10.0, 20.0],
            "change_pct": [0.10, 0.10],
            "deviate_3d": [0.25, 0.25],
            "deviate_10d": [None, None],
            "deviate_30d": [None, None],
        }
    )
    result = build_overview(_FakeRepo(df), _GemQuotes(), min_closeness=0.5)
    by_symbol = {r["symbol"]: r for r in result["rows"]}
    # 创业板: 0.25 + (0.10 - 0.02) = 0.33; 沪主板无对应行情: 0.25 + 0.10 = 0.35
    assert abs(by_symbol["300001.SZ"]["windows"]["3d"]["value"] - 0.33) < 1e-9
    assert abs(by_symbol["600000.SH"]["windows"]["3d"]["value"] - 0.35) < 1e-9
