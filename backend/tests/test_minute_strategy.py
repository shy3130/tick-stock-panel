"""分钟策略 (minute_filter 后端) 测试。

覆盖:
- minute_red_streak 形态: 命中 / 不足根数不触发 / 最高K不红 / rank_by 两口径 /
  乱序输入 / 最高价并列取更晚K线 / 开盘窗口(当日最早N根, 与最近N根区分)
- 引擎加载校验: 只能声明 filter_minute_history、timeframes 必须且只能是 ["1m"]
- 引擎 1m 运行: enriched 联表基础过滤 (剔除ST / 股价区间)、entry hits、
  日线 context 拒绝
- ScreenerService 1m context: 当日分区优先、缺失回退最近分区、空库报错、
  非股票资产拒绝
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.services.screener import ScreenerService
from app.strategy.engine import StrategyDataContext, StrategyEngine

# 分钟红7 已从内置策略改为自定义策略 (运行时 data/strategies/custom/, 不入库);
# 测试通过仓库内的参考实现夹具加载, 覆盖同一份策略逻辑。
STRATEGY_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "strategies_minute"
_spec = importlib.util.spec_from_file_location(
    "minute_red_streak_fixture", STRATEGY_FIXTURE_DIR / "minute_red_streak.py"
)
minute_red_streak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(minute_red_streak)


def _bars(symbol: str, candles: list[tuple[float, float, float]], start_hour: int = 9) -> pl.DataFrame:
    """candles: (open, close, high) 序列, 时间从 start_hour:30 起每分钟一根。"""
    n = len(candles)
    base = datetime(2026, 8, 25, start_hour, 30)
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "datetime": [base + _dt.timedelta(minutes=i) for i in range(n)],
        "open": [float(c[0]) for c in candles],
        "high": [float(c[2]) for c in candles],
        "low": [float(min(c[0], c[1])) for c in candles],
        "close": [float(c[1]) for c in candles],
        "volume": [100.0] * n,
        "amount": [10000.0] * n,
    })


# ── 形态 ────────────────────────────────────────────────────────────


def test_pattern_hits_five_red_of_seven_with_red_top_two():
    # 7根: 5红2绿, 绿K的最高价都压得比红K低 → 最高的两根(10.9/10.7)都是红
    candles = [
        (10.0, 10.2, 10.30),  # 红
        (10.2, 10.1, 10.25),  # 绿 (低高点)
        (10.1, 10.4, 10.50),  # 红
        (10.4, 10.6, 10.70),  # 红 (次高)
        (10.6, 10.5, 10.65),  # 绿 (低高点)
        (10.5, 10.7, 10.80),  # 红
        (10.7, 10.8, 10.90),  # 红 (最高)
    ]
    out = minute_red_streak.filter_minute_history(_bars("600000.SH", candles), {"require_limit_up": False})
    assert out["symbol"].to_list() == ["600000.SH"]
    row = out.row(0, named=True)
    assert row["red_count"] == 5
    assert row["top_red_count"] == 2
    assert row["close"] == 10.8


def test_pattern_insufficient_bars_never_triggers():
    out = minute_red_streak.filter_minute_history(_bars("600000.SH", [(10.0, 10.2, 10.3)] * 6), {"require_limit_up": False})
    assert out.is_empty()


def test_pattern_green_at_top_blocks_hit():
    # 5红, 但最高的一根是绿 (高开回落) → 最高两根不全红, 不触发
    candles = [
        (10.0, 10.2, 10.30),  # 红
        (10.1, 10.4, 10.50),  # 红
        (10.3, 10.6, 10.70),  # 红
        (10.6, 10.5, 10.65),  # 绿 (低高点)
        (10.4, 10.5, 10.55),  # 红 (低高点)
        (11.5, 11.0, 12.00),  # 绿 (最高)
        (11.0, 11.4, 11.90),  # 红 (次高)
    ]
    out = minute_red_streak.filter_minute_history(_bars("600000.SH", candles), {"require_limit_up": False})
    assert out.is_empty()


def test_pattern_rank_by_close_uses_close_not_high():
    # high 口径最高两根是绿K冲高; close 口径最高两根是红K → 仅 close 口径命中
    candles = [
        (10.0, 10.5, 10.60),  # 红
        (10.5, 10.9, 11.50),  # 绿 (high 最高, 并列)
        (10.9, 11.2, 11.40),  # 红
        (11.2, 11.3, 11.35),  # 红
        (11.3, 11.4, 11.45),  # 红 (close 次高)
        (11.4, 11.1, 11.50),  # 绿 (high 最高, 并列)
        (11.1, 11.5, 11.55),  # 红 (close 最高)
    ]
    by_high = minute_red_streak.filter_minute_history(_bars("600000.SH", candles), {"require_limit_up": False})
    by_close = minute_red_streak.filter_minute_history(
        _bars("600000.SH", candles), {"rank_by_close": True, "require_limit_up": False}
    )
    assert by_high.is_empty()
    assert by_close["symbol"].to_list() == ["600000.SH"]


def test_pattern_sorts_unordered_input_by_datetime():
    bars = pl.concat([
        _bars("600000.SH", [(10.0, 10.2, 10.30)]),
        _bars("600000.SH", [
            (10.2, 10.1, 10.25), (10.1, 10.4, 10.50), (10.4, 10.6, 10.70),
            (10.6, 10.5, 10.65), (10.5, 10.7, 10.80), (10.7, 10.8, 10.90),
        ]),
    ]).sample(fraction=1.0, shuffle=True, seed=7)
    out = minute_red_streak.filter_minute_history(bars, {"require_limit_up": False})
    assert out["symbol"].to_list() == ["600000.SH"]
    assert out.row(0, named=True)["close"] == 10.8  # 最后一根(时间最大)的收盘


def test_pattern_three_way_high_tie_prefers_later_bars():
    # 三根 high 并列最高: 更早的绿K应被更晚的两根红K挤出 top2 → 命中
    # (若并列取更早, top2 = {红, 绿} → 不命中; 该测试固定 "同值取更晚" 契约)
    candles = [
        (10.0, 10.2, 10.30),  # 红
        (10.1, 10.4, 10.50),  # 红
        (10.2, 10.1, 10.25),  # 绿 (低高点)
        (10.3, 10.6, 10.70),  # 红
        (10.8, 10.5, 10.90),  # 绿 (并列最高, 最早 → 被 top2 排除)
        (10.5, 10.6, 10.90),  # 红 (并列最高, 中间)
        (10.6, 10.8, 10.90),  # 红 (并列最高, 最晚)
    ]
    out = minute_red_streak.filter_minute_history(_bars("600000.SH", candles), {"require_limit_up": False})
    assert out["symbol"].to_list() == ["600000.SH"]
    assert out.row(0, named=True)["top_red_count"] == 2


def test_pattern_min_red_threshold_respected():
    # 4红3绿, 最高的两根红 → min_red=5 不命中, min_red=4 命中
    candles = [
        (10.0, 10.2, 10.30),  # 红
        (10.2, 10.1, 10.25),  # 绿
        (10.1, 10.4, 10.50),  # 红
        (10.4, 10.3, 10.45),  # 绿
        (10.3, 10.6, 10.70),  # 红
        (10.6, 10.5, 10.65),  # 绿
        (10.5, 10.8, 10.90),  # 红
    ]
    bars = _bars("600000.SH", candles)
    assert minute_red_streak.filter_minute_history(bars, {"min_red": 5, "require_limit_up": False}).is_empty()
    assert not minute_red_streak.filter_minute_history(bars, {"min_red": 4, "require_limit_up": False}).is_empty()


def test_pattern_uses_opening_bars_even_if_day_turns_green():
    # 开盘7根 = 5红2绿命中; 第8/9根大绿回落 → 开盘窗口语义下仍命中,
    # 且 close 取窗口末根 (10.8) 而非全天最新价
    candles = [
        (10.0, 10.2, 10.30),  # 红
        (10.2, 10.1, 10.25),  # 绿 (低高点)
        (10.1, 10.4, 10.50),  # 红
        (10.4, 10.6, 10.70),  # 红 (次高)
        (10.6, 10.5, 10.65),  # 绿 (低高点)
        (10.5, 10.7, 10.80),  # 红
        (10.7, 10.8, 10.90),  # 红 (最高) ← 窗口末根
        (10.8, 10.0, 10.85),  # 开盘窗口外的绿
        (10.0, 9.5, 10.05),   # 开盘窗口外的绿
    ]
    out = minute_red_streak.filter_minute_history(_bars("600000.SH", candles), {"require_limit_up": False})
    assert out["symbol"].to_list() == ["600000.SH"]
    row = out.row(0, named=True)
    assert row["red_count"] == 5
    assert row["close"] == 10.8  # 窗口末根收盘, 不是第9根的 9.5
    assert row["last_datetime"] == datetime(2026, 8, 25, 9, 36)


def test_pattern_opening_window_miss_not_rescued_by_late_reds():
    # 开盘7根仅4红不命中; 第8/9根转红 (最近7根口径会命中) → 开盘窗口仍不触发
    candles = [
        (10.0, 10.2, 10.30),  # 红
        (10.2, 10.1, 10.25),  # 绿
        (10.1, 10.4, 10.50),  # 红
        (10.4, 10.3, 10.45),  # 绿
        (10.3, 10.6, 10.70),  # 红
        (10.6, 10.5, 10.65),  # 绿
        (10.5, 10.8, 10.90),  # 红
        (10.8, 10.9, 11.00),  # 红 (窗口外)
        (10.9, 11.0, 11.10),  # 红 (窗口外)
    ]
    out = minute_red_streak.filter_minute_history(_bars("600000.SH", candles), {"require_limit_up": False})
    assert out.is_empty()


# ── 涨停条件 (日线维度) ─────────────────────────────────────────────


_HIT_CANDLES = [
    (10.0, 10.2, 10.30),  # 红
    (10.2, 10.1, 10.25),  # 绿 (低高点)
    (10.1, 10.4, 10.50),  # 红
    (10.4, 10.6, 10.70),  # 红 (次高)
    (10.6, 10.5, 10.65),  # 绿 (低高点)
    (10.5, 10.7, 10.80),  # 红
    (10.7, 10.8, 10.90),  # 红 (最高)
]


def _daily(
    symbol: str,
    days: int,
    flag_on: set[int] | None = None,
    *,
    broken: bool = False,
) -> pl.DataFrame:
    """days 个交易日的日线帧; flag_on 指定第几天 (0=最早) 触发涨停信号。"""
    flag_on = flag_on or set()
    base = date(2026, 8, 25)
    return pl.DataFrame({
        "symbol": [symbol] * days,
        "date": [base - _dt.timedelta(days=days - i) for i in range(days)],
        "signal_limit_up": [i in flag_on and not broken for i in range(days)],
        "signal_broken_limit_up": [i in flag_on and broken for i in range(days)],
    })


def test_pattern_limit_up_condition_filters_by_daily_signals():
    bars = pl.concat([
        _bars("600001.SH", _HIT_CANDLES),
        _bars("600002.SH", _HIT_CANDLES),
        _bars("600003.SH", _HIT_CANDLES),
    ])
    daily = pl.concat([
        _daily("600001.SH", 20, {3}),                 # 收盘涨停 → 过
        _daily("600002.SH", 20, {15}, broken=True),   # 炸板触及 → 过
        _daily("600003.SH", 20),                      # 无涨停 → 剔除
    ])
    out = minute_red_streak.filter_minute_history(bars, {}, daily=daily)
    assert sorted(out["symbol"].to_list()) == ["600001.SH", "600002.SH"]
    assert sorted(out["recent_limit_ups"].to_list()) == [1, 1]


def test_pattern_limit_up_lookback_window_boundary():
    # 25 个交易日, 涨停仅发生在第 5 天 (0=最早): 回看 20 日窗口 = 最后 20 根
    # (索引 5..24), 第 5 天在窗外 → 不命中; 回看放宽到 25 → 命中
    bars = _bars("600000.SH", _HIT_CANDLES)
    daily = _daily("600000.SH", 25, {4})
    assert minute_red_streak.filter_minute_history(bars, {}, daily=daily).is_empty()
    out = minute_red_streak.filter_minute_history(
        bars, {"limit_up_days": 25}, daily=daily
    )
    assert out["symbol"].to_list() == ["600000.SH"]


def test_pattern_limit_up_fails_closed_without_daily():
    # 日线窗口缺失时失败闭合 (宁可漏过不可错报)
    out = minute_red_streak.filter_minute_history(_bars("600000.SH", _HIT_CANDLES), {})
    assert out.is_empty()


def test_pattern_limit_up_disabled_ignores_daily():
    out = minute_red_streak.filter_minute_history(
        _bars("600000.SH", _HIT_CANDLES), {"require_limit_up": False}
    )
    assert out["symbol"].to_list() == ["600000.SH"]
    assert "recent_limit_ups" not in out.columns


# ── 引擎加载与运行 ──────────────────────────────────────────────────


def test_custom_minute_strategy_loads_with_minute_filter_backend():
    # 自定义策略与内置策略共用同一加载器: 夹具目录即一个 custom 目录
    engine = StrategyEngine(strategy_dirs=[STRATEGY_FIXTURE_DIR])
    assert not [e for e in engine.load_errors() if "minute" in e["file"]]
    s = engine.get("minute_red_streak")
    assert s.execution_backend == "minute_filter"
    assert s.filter_minute_history_fn is not None
    assert s.meta["timeframes"] == ["1m"]
    assert s.source == "custom"


def _minute_code(sid: str, timeframes: str = '["1m"]', extra: str = "") -> str:
    return f'''import polars as pl
META = {{"id": "{sid}", "name": "{sid}", "asset_types": ["stock"], "timeframes": {timeframes}}}
EXECUTION_BACKEND = "minute_filter"
{extra}
def filter_minute_history(df, params):
    return df.group_by("symbol").agg(
        close=pl.col("close").max(), last_datetime=pl.col("datetime").max()
    )
'''


def test_minute_filter_backend_validation(tmp_path):
    (tmp_path / "ok.py").write_text(_minute_code("m_ok"))
    (tmp_path / "bad_filter.py").write_text(
        _minute_code("m_bad1", extra="def filter(df, params):\n    return pl.lit(True)")
    )
    (tmp_path / "bad_tf.py").write_text(_minute_code("m_bad2", timeframes='["1d", "1m"]'))
    engine = StrategyEngine(strategy_dirs=[tmp_path])
    ids = {m["id"] for m in engine.list_strategies(include_research=True)}
    assert "m_ok" in ids
    assert "m_bad1" not in ids
    assert "m_bad2" not in ids
    assert any("only filter_minute_history" in e["error"] for e in engine.load_errors())
    assert any("timeframes" in e["error"] for e in engine.load_errors())


def test_minute_filter_daily_history_validation(tmp_path):
    # 声明 daily_history_bars: fn 必须接受 daily 关键字, 且范围 [0, 250]
    (tmp_path / "m_daily_ok.py").write_text(
        'import polars as pl\n'
        'META = {"id": "m_daily_ok", "name": "x", "asset_types": ["stock"], '
        '"timeframes": ["1m"], "daily_history_bars": 20}\n'
        'EXECUTION_BACKEND = "minute_filter"\n'
        'def filter_minute_history(df, params, *, daily=None):\n'
        '    return df.group_by("symbol").agg(close=pl.col("close").max())\n'
    )
    (tmp_path / "m_daily_kw.py").write_text(
        'import polars as pl\n'
        'META = {"id": "m_daily_kw", "name": "x", "asset_types": ["stock"], '
        '"timeframes": ["1m"], "daily_history_bars": 20}\n'
        'EXECUTION_BACKEND = "minute_filter"\n'
        'def filter_minute_history(df, params):\n'
        '    return df.group_by("symbol").agg(close=pl.col("close").max())\n'
    )
    (tmp_path / "m_daily_range.py").write_text(
        'import polars as pl\n'
        'META = {"id": "m_daily_range", "name": "x", "asset_types": ["stock"], '
        '"timeframes": ["1m"], "daily_history_bars": 300}\n'
        'EXECUTION_BACKEND = "minute_filter"\n'
        'def filter_minute_history(df, params, *, daily=None):\n'
        '    return df.group_by("symbol").agg(close=pl.col("close").max())\n'
    )
    engine = StrategyEngine(strategy_dirs=[tmp_path])
    assert engine.has("m_daily_ok")
    assert engine.get("m_daily_ok").minute_daily_bars == 20
    assert not engine.has("m_daily_kw")
    assert not engine.has("m_daily_range")
    assert any("'daily' keyword" in e["error"] for e in engine.load_errors())
    assert any("[0, 250]" in e["error"] for e in engine.load_errors())


def test_minute_run_injects_daily_history(tmp_path):
    # fn 直接消费 daily (对涨停信号求和), 验证引擎把 context.daily_history 注入
    (tmp_path / "m_use_daily.py").write_text(
        'import polars as pl\n'
        'META = {"id": "m_use_daily", "name": "x", "asset_types": ["stock"], '
        '"timeframes": ["1m"], "daily_history_bars": 10}\n'
        'EXECUTION_BACKEND = "minute_filter"\n'
        'def filter_minute_history(df, params, *, daily=None):\n'
        '    if daily is None:\n'
        '        return pl.DataFrame(schema={"symbol": pl.Utf8})\n'
        '    return daily.group_by("symbol").agg(\n'
        '        close=pl.col("signal_limit_up").sum() + 10.0)\n'
    )
    engine = StrategyEngine(strategy_dirs=[tmp_path])
    context = StrategyDataContext(
        asset_type="stock",
        timeframe="1m",
        as_of=date(2026, 8, 25),
        current=pl.DataFrame({
            "symbol": ["600001.SH"],
            "name": ["正常股"],
            "total_shares": [1e8],
            "float_shares": [5e7],
            "amount": [3e8],
            "change_pct": [0.01],
        }),
        history=_bars("600001.SH", [(10.0, 10.2, 10.3)] * 7),
        daily_history=_daily("600001.SH", 10, {2}),
    )
    result = engine.run("m_use_daily", context)
    assert result.total == 1
    assert result.rows[0]["close"] == 11  # 10 + 窗口内 1 次收盘涨停


def test_minute_context_run_applies_enriched_basic_filter(tmp_path):
    (tmp_path / "m_basic.py").write_text(_minute_code("m_basic"))
    engine = StrategyEngine(strategy_dirs=[tmp_path])

    hist = pl.concat([
        _bars("600001.SH", [(10.0, 20.0, 25.0)] * 7),   # 命中, 收盘 20
        _bars("600002.SH", [(10.0, 20.0, 25.0)] * 7),   # 命中但 ST → 剔除
        _bars("600003.SH", [(10.0, 20.0, 25.0)] * 7),   # 命中
        _bars("600004.SH", [(100.0, 200.0, 250.0)] * 7), # 命中但收盘 200 → 超上限剔除
    ])
    current = pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH", "600003.SH", "600004.SH"],
        "name": ["正常股", "ST垃圾", "正常股2", "高价股"],
        "total_shares": [1e8, 1e8, 1e8, 1e8],
        "float_shares": [5e7, 5e7, 5e7, 5e7],
        "amount": [3e8, 3e8, 3e8, 3e8],
        "change_pct": [0.01, 0.01, 0.01, 0.01],
    })
    context = StrategyDataContext(
        asset_type="stock",
        timeframe="1m",
        as_of=date(2026, 8, 25),
        current=current,
        history=hist,
    )
    result = engine.run(
        "m_basic", context, overrides={"basic_filter": {"price_max": 150.0}}
    )
    symbols = {r["symbol"] for r in result.rows}
    assert symbols == {"600001.SH", "600003.SH"}
    assert all("name" in r for r in result.rows)  # enriched 列已联表
    assert {h["symbol"] for h in result.entry_signal_hits} == symbols


def test_minute_strategy_rejects_daily_context(tmp_path):
    (tmp_path / "m_daily.py").write_text(_minute_code("m_daily"))
    engine = StrategyEngine(strategy_dirs=[tmp_path])
    context = StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 8, 25),
        current=pl.DataFrame({"symbol": ["600001.SH"]}),
    )
    try:
        engine.run("m_daily", context)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "timeframe" in str(e)


# ── ScreenerService 1m context ──────────────────────────────────────


class _FakeMinuteRepo:
    def __init__(self, partitions: dict[date, pl.DataFrame]):
        self.partitions = partitions

    def get_minute_by_dates(self, symbols, dates, asset_type="stock"):
        frames = [self.partitions[d] for d in dates if d in self.partitions]
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames).filter(pl.col("symbol").is_in(symbols))

    def latest_minute_date_global(self):
        return max(self.partitions) if self.partitions else None


def _svc(partitions: dict[date, pl.DataFrame], asset_type: str = "stock") -> ScreenerService:
    return ScreenerService(_FakeMinuteRepo(partitions), asset_type=asset_type)  # type: ignore[arg-type]


def test_minute_context_prefers_as_of_partition():
    d1, d2 = date(2026, 8, 24), date(2026, 8, 25)
    svc = _svc({
        d1: _bars("600001.SH", [(10.0, 10.2, 10.3)] * 3),
        d2: _bars("600001.SH", [(10.0, 10.2, 10.3)] * 4),
    })
    ctx = svc.build_strategy_context(
        None, d1, [], timeframe="1m",
        current=pl.DataFrame({"symbol": ["600001.SH"], "name": ["x"]}),
    )
    assert ctx.history.height == 3  # as_of 当日分区, 不取更新的 d2
    assert ctx.timeframe == "1m"


def test_minute_context_falls_back_to_latest_partition():
    d1, d2 = date(2026, 8, 24), date(2026, 8, 25)
    svc = _svc({
        d1: _bars("600001.SH", [(10.0, 10.2, 10.3)] * 3),
        d2: _bars("600001.SH", [(10.0, 10.2, 10.3)] * 4),
    })
    ctx = svc.build_strategy_context(
        None, date(2026, 8, 20), [], timeframe="1m",
        current=pl.DataFrame({"symbol": ["600001.SH"]}),
    )
    assert ctx.history.height == 4  # 回退到最近分区 d2


def test_minute_context_empty_store_raises_with_guidance():
    svc = _svc({})
    try:
        svc.build_strategy_context(
            None, date(2026, 8, 25), [], timeframe="1m",
            current=pl.DataFrame({"symbol": ["600001.SH"]}),
        )
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "分钟K" in str(e)


def test_minute_context_rejects_non_stock_asset():
    svc = _svc({date(2026, 8, 25): _bars("510300.SH", [(10.0, 10.2, 10.3)] * 3)}, asset_type="etf")
    try:
        svc.build_strategy_context(
            None, date(2026, 8, 25), [], timeframe="1m",
            current=pl.DataFrame({"symbol": ["510300.SH"]}),
        )
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "A 股" in str(e)


def test_minute_context_loads_daily_history_for_declared_strategies():
    class _FakeEngine:
        def minute_daily_history_bars(self, strategy_ids):
            return 5

    daily = _daily("600001.SH", 6, {1})
    repo = _FakeMinuteRepo({date(2026, 8, 25): _bars("600001.SH", [(10.0, 10.2, 10.3)] * 3)})
    repo.get_enriched_history = lambda target_date, lookback_days: daily  # type: ignore[method-assign]
    repo.get_instruments_asset = lambda asset_type: None  # type: ignore[method-assign]
    svc = ScreenerService(repo, asset_type="stock")  # type: ignore[arg-type]
    ctx = svc.build_strategy_context(
        _FakeEngine(), date(2026, 8, 25), ["m_x"], timeframe="1m",
        current=pl.DataFrame({"symbol": ["600001.SH"], "name": ["x"]}),
    )
    assert ctx.daily_history is not None
    assert ctx.daily_history.height == 6  # 引擎声明 5 → 装配日线窗口


def test_minute_context_without_engine_skips_daily_history():
    svc = _svc({date(2026, 8, 25): _bars("600001.SH", [(10.0, 10.2, 10.3)] * 3)})
    ctx = svc.build_strategy_context(
        None, date(2026, 8, 25), [], timeframe="1m",
        current=pl.DataFrame({"symbol": ["600001.SH"]}),
    )
    assert ctx.daily_history is None  # 无引擎声明 → 不装配日线
