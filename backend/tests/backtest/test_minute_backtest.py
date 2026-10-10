"""分钟策略回测端到端集成测试 (三期 v1)。

用合成的分钟K分区 + 合成日线面板 + 专用测试策略, 完整跑通
StrategyBacktestService.run() 的 minute_filter 分支:
逐日回放 (与实盘策略同一条 StrategyEngine.run 路径) → 信号分钟收盘入场
→ 涨停拒买 → 日K矩阵离场 → 交易记录携带分钟时间戳。

核心断言:
- 日线窗口因果性: T 日的日线条件窗口只含 T-1 及更早 (测试策略内置守卫,
  窗口含 T 则拒绝命中 — 若回放器传错窗口, 全部用例的信号归零);
- 入场价 = 触发分钟收盘价 (entry_price_override 机制);
- 涨停拒买: 触发分钟收盘 >= 当日涨停价 (T-1 收盘 + 板块规则) 不成交;
- 缺分钟分区的交易日显式跳过 (不回退最近分区);
- 离场复用日K口径 (max_hold → 次日开盘)。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.engine import StrategyEngine

# ── 测试策略: 内置因果性守卫 ──────────────────────────────────────
# 命中条件: 当日某分钟 close > T-1 close * 1.05, 触发分钟 = 首根满足条件的K。
# daily 窗口的最后一个日期必须 < 触发日, 否则返回空 (回放器传错窗口时信号归零)。
TEST_STRATEGY_SOURCE = '''
import polars as pl

META = {
    "id": "test_minute_ping",
    "name": "test_minute_ping",
    "asset_types": ["stock"],
    "timeframes": ["1m"],
    "daily_history_bars": 5,
    "order_by": "close",
    "descending": True,
    "limit": 100,
}
EXECUTION_BACKEND = "minute_filter"


def filter_minute_history(df, params, *, daily=None):
    if daily is None or daily.is_empty():
        return pl.DataFrame()
    trigger_day = df.select(pl.col("datetime").max()).item().date()
    # 因果性守卫: 日线窗口不得包含触发日。
    if daily.get_column("date").max() >= trigger_day:
        return pl.DataFrame()
    prev = (
        daily.sort("date").group_by("symbol").last()
        .select(pl.col("symbol"), pl.col("close").alias("prev_close"))
    )
    joined = df.join(prev, on="symbol", how="inner")
    hits = joined.filter(pl.col("close") > pl.col("prev_close") * 1.05)
    if hits.is_empty():
        return pl.DataFrame()
    return (
        hits.sort("datetime").group_by("symbol").first()
        .select(
            pl.col("symbol"),
            pl.col("datetime").alias("last_datetime"),
            pl.col("close"),
        )
    )
'''


# ── 合成数据 ─────────────────────────────────────────────────────
def _trading_days(n: int, start: date = date(2026, 7, 1)) -> list[date]:
    days: list[date] = []
    cur = start
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _daily_panel(days: list[date], symbols: list[str]) -> pl.DataFrame:
    """合成日线面板: 三个符号的慢涨走势, raw_close == close (复权因子 1)。"""
    rows = []
    for sym_idx, sym in enumerate(symbols):
        base = 10.0 + sym_idx * 4.0
        for t, day in enumerate(days):
            close = round(base * (1 + t * 0.002), 3)
            open_p = round(close - 0.05, 3)
            rows.append({
                "symbol": sym,
                "date": day,
                "open": open_p,
                "high": round(close + 0.08, 3),
                "low": round(open_p - 0.06, 3),
                "close": close,
                "raw_close": close,
                # 成交额需过 DEFAULT_BASIC_FILTER.amount_min (2e8) — 命中行的
                # amount 由 T-1 enriched 快照联表注入 (与实盘同路径)。
                "volume": 2e7,
                "amount": round(close * 2e7, 3),
                "name": f"股票{sym_idx}",
                "total_shares": 5e8,
                "float_shares": 4e8,
                "signal_limit_up": False,
                "signal_limit_down": False,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"]).with_columns(
        pl.col("date").cast(pl.Date),
    )


def _minute_frame(day: date, bars: list[tuple[str, str, float]]) -> pl.DataFrame:
    """bars: (symbol, "HH:MM"(北京), close)。分区 datetime 为 naive-UTC 存储 (北京 - 8h)。"""
    rows = []
    for sym, hm, close in bars:
        local = datetime(day.year, day.month, day.day, int(hm[:2]), int(hm[3:]))
        rows.append({
            "symbol": sym,
            "datetime": local - timedelta(hours=8),
            "open": close - 0.01,
            "high": close + 0.01,
            "low": close - 0.02,
            "close": close,
            "volume": 1000.0,
            "amount": close * 1000.0,
        })
    return pl.DataFrame(rows).sort(["symbol", "datetime"]).with_columns(
        pl.col("datetime").cast(pl.Datetime("us")),
    )


class _FakeMinuteRepo:
    """仅实现分钟回测所需的最小 repo 接口。"""

    def __init__(self, minute_frames: dict[date, pl.DataFrame]) -> None:
        self.minute_frames = minute_frames
        self.store = None

    def list_minute_dates(self, start, end, asset_type="stock"):
        return sorted(d for d in self.minute_frames if start <= d <= end)

    def get_minute_by_dates(self, symbols, dates, asset_type="stock"):
        frames = [self.minute_frames[d] for d in dates if d in self.minute_frames]
        if not frames:
            return pl.DataFrame(
                schema={"symbol": pl.Utf8, "datetime": pl.Datetime("us"),
                        "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
                        "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64},
            )
        df = pl.concat(frames)
        if symbols:
            df = df.filter(pl.col("symbol").is_in(list(symbols)))
        return df.sort(["symbol", "datetime"])

    def earliest_minute_date(self):
        return min(self.minute_frames) if self.minute_frames else None

    def get_index_daily(self, *args, **kwargs) -> pl.DataFrame:
        return pl.DataFrame()


def _make_service(
    tmp_path: Path, panel: pl.DataFrame, minute_frames: dict[date, pl.DataFrame],
) -> StrategyBacktestService:
    strat_dir = tmp_path / "strategies"
    strat_dir.mkdir(exist_ok=True)
    (strat_dir / "test_minute_ping.py").write_text(TEST_STRATEGY_SOURCE, encoding="utf-8")
    strategy_engine = StrategyEngine(strategy_dirs=[strat_dir])

    repo = _FakeMinuteRepo(minute_frames)
    bt_engine = BacktestEngine(repo)

    def _load_panel(self, symbols, start, end, feature_plan, asset_type="stock", **kw):
        df = panel.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        if symbols:
            df = df.filter(pl.col("symbol").is_in(list(symbols)))
        keep = set(feature_plan.base_columns) | set(feature_plan.instrument_columns) | {"symbol", "date"}
        return df.select(sorted(c for c in df.columns if c in keep))

    bt_engine.load_panel_for_backtest = _load_panel.__get__(bt_engine)
    return StrategyBacktestService(bt_engine, strategy_engine)


def _config(start: date, end: date, **kw) -> StrategyBacktestConfig:
    defaults = dict(
        strategy_id="test_minute_ping",
        symbols=None,
        start=start,
        end=end,
        exit_fill="open_t+1",
        max_positions=10,
        mode="position",
        holding_days=1,
        overrides={"max_hold_days": 1},
    )
    defaults.update(kw)
    return StrategyBacktestConfig(**defaults)


@pytest.fixture()
def scenario(tmp_path: Path):
    """三个符号 x 三个回测日。面板共 30 个交易日 (指数慢涨, 涨停价按 T-1 收盘 +10%)。

    - 000001.SZ: T1 触发 (close 10.72 > prev 10.19*1.05), T2/T3 不再触发;
    - 000002.SZ: 三天都不触发 (涨幅不足 5%);
    - 600000.SH: T2 触发但触发分钟收盘已达涨停价 → 拒买; T3 正常触发。
    """
    days = _trading_days(30)
    t1, t2, t3 = days[-4], days[-3], days[-2]  # 留一天做 T+1 离场
    symbols = ["000001.SZ", "000002.SZ", "600000.SH"]
    panel = _daily_panel(days, symbols)

    def _prev_close(sym: str, before: date) -> float:
        return panel.filter(
            (pl.col("symbol") == sym) & (pl.col("date") < before)
        ).sort("date").get_column("close")[-1]

    minute_frames = {
        t1: _minute_frame(t1, [
            ("000001.SZ", "09:31", round(_prev_close("000001.SZ", t1) * 1.005, 3)),
            ("000001.SZ", "09:35", round(_prev_close("000001.SZ", t1) * 1.07, 3)),  # 触发
            ("000002.SZ", "09:31", round(_prev_close("000002.SZ", t1) * 1.01, 3)),
            ("600000.SH", "09:31", round(_prev_close("600000.SH", t1) * 1.01, 3)),
        ]),
        t2: _minute_frame(t2, [
            ("000001.SZ", "09:31", round(_prev_close("000001.SZ", t2) * 1.004, 3)),
            ("000002.SZ", "09:31", round(_prev_close("000002.SZ", t2) * 1.01, 3)),
            # 涨停拒买: 触发分钟收盘 = T-1收盘 * 1.10 (主板涨停价, 半进位后相等)
            ("600000.SH", "09:40", round(_prev_close("600000.SH", t2) * 1.10, 3)),
        ]),
        t3: _minute_frame(t3, [
            ("000001.SZ", "09:31", round(_prev_close("000001.SZ", t3) * 1.004, 3)),
            ("000002.SZ", "09:31", round(_prev_close("000002.SZ", t3) * 1.01, 3)),
            ("600000.SH", "09:50", round(_prev_close("600000.SH", t3) * 1.06, 3)),  # 触发
        ]),
    }
    service = _make_service(tmp_path, panel, minute_frames)
    return service, panel, {"t1": t1, "t2": t2, "t3": t3, "t4": days[-1]}, minute_frames


def test_entry_at_trigger_minute_price(scenario):
    service, panel, days, _ = scenario
    result = service.run(_config(days["t1"], days["t3"]))
    assert not result.error, result.error
    entries = [t for t in result.trades if t["symbol"] == "000001.SZ"]
    assert len(entries) == 1
    trade = entries[0]
    # 入场价 = 触发分钟 (09:35) 收盘价, 入场时间戳精确到分钟
    prev_close = panel.filter(
        (pl.col("symbol") == "000001.SZ") & (pl.col("date") < days["t1"])
    ).sort("date").get_column("close")[-1]
    expected_price = round(prev_close * 1.07, 3)
    assert trade["entry_price"] == pytest.approx(expected_price, abs=1e-6)
    assert trade["entry_date"].startswith(f"{days['t1']} 09:35")


def test_daily_window_strictly_before_trigger_day(scenario):
    """因果性: 测试策略拒绝含触发日的日线窗口 — 有信号即证明窗口止于 T-1。"""
    service, _, days, _ = scenario
    result = service.run(_config(days["t1"], days["t3"]))
    assert not result.error, result.error
    assert result.trades, "日线窗口若含触发日, 测试策略会拒绝命中 — 信号归零"


def test_limit_up_entry_rejected(scenario):
    service, panel, days, _ = scenario
    result = service.run(_config(days["t1"], days["t3"]))
    assert not result.error, result.error
    # T2 的 600000.SH 触发分钟收盘 = 涨停价 → 拒买; T3 才有它的成交
    entries_600000 = [t for t in result.trades if t["symbol"] == "600000.SH"]
    assert all(t["entry_date"][:10] == str(days["t3"]) for t in entries_600000)
    execution = result.stats.get("execution", {})
    assert execution.get("buy_limit_up", 0) >= 1
    replay_stats = result.stats.get("minute_replay", {})
    assert replay_stats.get("replayed_days") == 3


def test_missing_partition_day_skipped(tmp_path):
    days = _trading_days(30)
    t1, t2, t3 = days[-4], days[-3], days[-2]
    symbols = ["000001.SZ"]
    panel = _daily_panel(days, symbols)

    def _prev(before: date) -> float:
        return panel.filter(
            (pl.col("symbol") == "000001.SZ") & (pl.col("date") < before)
        ).sort("date").get_column("close")[-1]

    frames = {
        t1: _minute_frame(t1, [("000001.SZ", "09:35", round(_prev(t1) * 1.07, 3))]),
        t3: _minute_frame(t3, [("000001.SZ", "09:35", round(_prev(t3) * 1.06, 3))]),
        # t2 无分区 → 应被跳过, 而不是回退到 t1/t3 的数据
    }
    service = _make_service(tmp_path, panel, frames)
    result = service.run(_config(t1, t3))
    assert not result.error, result.error
    replay_stats = result.stats.get("minute_replay", {})
    assert replay_stats.get("replayed_days") == 2
    assert str(t2) in replay_stats.get("skipped_days", [])
    entry_days = {t["entry_date"][:10] for t in result.trades}
    assert str(t2) not in entry_days


def test_exit_reuses_daily_next_open(scenario):
    """离场复用日K口径: max_hold=1 → 次日开盘卖出。"""
    service, panel, days, _ = scenario
    result = service.run(_config(days["t1"], days["t3"]))
    assert not result.error, result.error
    trade = next(t for t in result.trades if t["symbol"] == "000001.SZ")
    entry_day = date.fromisoformat(trade["entry_date"][:10])
    exit_day = date.fromisoformat(str(trade["exit_date"])[:10])
    assert exit_day > entry_day
    next_open = panel.filter(
        (pl.col("symbol") == "000001.SZ") & (pl.col("date") == exit_day)
    ).get_column("open")[0]
    assert trade["exit_price"] == pytest.approx(next_open, abs=1e-6)


def test_guards(scenario, tmp_path):
    service, panel, days, _ = scenario
    # 信号触发卖出离场口径不支持 (通用校验或分钟分支守卫, 任一拒绝即可)
    result = service.run(_config(days["t1"], days["t3"], exit_fill="signal_next_minute"))
    assert result.error and "分钟" in result.error
    # 无分钟分区 → 明确报错
    empty_service = _make_service(tmp_path, panel, {})
    result = empty_service.run(_config(days["t1"], days["t3"]))
    assert "分钟K" in (result.error or "")
