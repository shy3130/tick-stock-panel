"""分钟策略回测回放器 — 逐交易日回放 filter_minute_history 产生入场信号。

与实盘策略 (ScreenerService 1m context) 走同一条 StrategyEngine.run 执行路径,
消除回测/实盘偏差。语义铁律:

- 分钟侧: 传入当日全量分钟分区, 策略函数自身因果 (第 m 根只用 <=m 的K线);
- 日线侧: T 日的日线条件窗口只含 T-1 及更早的完成态日K — 与实盘盘中行为一致
  (当日成形K不进窗口), 杜绝未来函数;
- 按交易日精确对日: 缺分钟分区的日子显式跳过, 不做"回退最近分区" (那是实盘语义)。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from app.price_limits import is_risk_warning_name, price_limit_pct
from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine

# 日线面板列: 基础行情 + 涨停/炸板信号 (策略日线窗口契约) + 基础过滤/展示列。
# raw_close 用于涨停价计算 (分钟价是未复权真实价, 涨停规则定义在原始价上)。
MINUTE_DAILY_PANEL_COLUMNS = frozenset({
    "open", "high", "low", "close", "volume", "amount",
    "raw_close", "raw_high", "raw_low",
    "turnover_rate",
    "signal_limit_up", "signal_limit_down", "signal_broken_limit_up",
})
MINUTE_INSTRUMENT_COLUMNS = frozenset({"name", "total_shares", "float_shares"})


def minute_replay_feature_plan(daily_bars: int):
    """分钟回测的日线面板加载计划。

    execution_backend 用 polars_expr 走"按需计算信号"路径: enriched 分区只落
    基础列, 涨停/炸板信号由 load_panel_for_backtest 的 compute_limit_signals
    按 signal_columns 需求现算 (matrix_native 路径会跳过通用信号计算)。
    """
    # 函数级导入规避与 strategy.py 的循环依赖 (strategy 顶层导入本模块)。
    from app.backtest.strategy import ResolvedFeaturePlan

    return ResolvedFeaturePlan(
        base_columns=MINUTE_DAILY_PANEL_COLUMNS,
        intermediate_columns=frozenset(),
        indicator_columns=frozenset(),
        signal_columns=frozenset({
            "signal_limit_up", "signal_limit_down", "signal_broken_limit_up",
        }),
        matrix_columns=frozenset(),
        instrument_columns=MINUTE_INSTRUMENT_COLUMNS,
        warmup_bars=max(daily_bars, 1),
        full_feature_fallback=False,
        execution_backend="polars_expr",
    )


def minute_panel_start(start: date, daily_bars: int) -> date:
    """日线面板加载起点: 覆盖首个回测日的 daily_bars 交易日窗口。

    N 个交易日约需 N*2 自然日 (周末/节假日), 再留 warmup 余量。
    """
    calendar_days = max(daily_bars, 1) * 2 + 30
    return start - timedelta(days=calendar_days)


def _trigger_hhmm(value) -> str:
    """从 last_datetime 提取北京时间 "HH:MM" 触发分钟。

    分区 datetime 为 UTC 存储 (tz-aware 或 naive-UTC), 统一折算到北京时区。
    """
    from app.market_time import CN_TZ

    if hasattr(value, "astimezone"):
        if value.tzinfo is None:
            from datetime import timezone

            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(CN_TZ).strftime("%H:%M")
    text = str(value or "")
    if len(text) >= 16 and text[13] == ":":
        return text[11:16]
    return text[-5:] if text else ""


def _scalar_limit_up_price(prev_close: float, limit_pct: float) -> float:
    """与 polars_limit_price 同口径的标量涨停价 (整数分半进位)。"""
    cents = int(prev_close * 100 + 0.5)
    numerator = round((1 + limit_pct) * 100)
    return ((cents * numerator + 50) // 100) / 100


@dataclass
class MinuteReplayHit:
    """一个盘中入场信号: 触发分钟收盘买入。"""

    trade_date: date
    symbol: str
    # 已按当日 复权close/原始close 比例折算到复权价系的入场价, 与日线出场价同尺度。
    entry_price: float
    trigger_time: str  # "HH:MM" — 触发分钟K的时间戳
    score: float = 0.0


@dataclass
class MinuteReplayResult:
    hits: list[MinuteReplayHit] = field(default_factory=list)
    skipped_days: list[date] = field(default_factory=list)
    replayed_days: int = 0
    strategy_matches: int = 0
    buy_limit_up: int = 0
    elapsed_ms: float = 0.0


class MinuteSignalReplayer:
    """逐交易日回放分钟策略, 产出与实盘策略同源的入场命中。"""

    def __init__(self, engine, strategy_engine: StrategyEngine) -> None:
        # engine: BacktestEngine — 只用其 repo (分钟分区读取)。
        self.engine = engine
        self.strategy_engine = strategy_engine

    def replay(
        self,
        strategy: StrategyDef,
        *,
        panel: pl.DataFrame,
        start: date,
        end: date,
        params: dict,
        overrides: dict,
        pool: list[str] | None = None,
        symbols: list[str] | None = None,
        progress_cb: Callable[[dict], None] | None = None,
        cancel_event=None,
    ) -> MinuteReplayResult:
        t0 = time.perf_counter()
        result = MinuteReplayResult()
        repo = self.engine.repo
        if panel.is_empty():
            return result

        universe = symbols if symbols else panel.get_column("symbol").unique().to_list()
        daily_bars = int(strategy.minute_daily_bars or 0)

        # 面板交易日序列 (升序) — 日线窗口切片与缺分区日判定的基准。
        panel_dates = panel.get_column("date").unique().sort().to_list()
        date_to_window: dict[date, tuple[date, date]] = {}
        for i, day in enumerate(panel_dates):
            window_start = panel_dates[max(0, i - daily_bars)] if daily_bars > 0 else day
            date_to_window[day] = (window_start, day)

        # 逐分区日回放: 只回放 [start, end] 内有分钟分区的交易日。
        minute_days = repo.list_minute_dates(start, end, "stock")
        minute_day_set = set(minute_days)
        replay_days = [day for day in panel_dates if start <= day <= end]
        result.skipped_days = [day for day in replay_days if day not in minute_day_set]
        total = len(minute_days)

        # 逐标的的 T-1 原始收盘/复权收盘查表 (涨停价与复权折算用)。
        prev_raw_close: dict[str, float] = {}
        prev_name: dict[str, str] = {}
        adj_factor: dict[str, float] = {}

        for i, day in enumerate(minute_days):
            if cancel_event is not None and cancel_event.is_set():
                break
            if progress_cb is not None:
                progress_cb({
                    "day": i + 1,
                    "total": max(total, 1),
                    "date": str(day),
                })

            history = repo.get_minute_by_dates(universe, [day], "stock")
            if history.is_empty():
                result.skipped_days.append(day)
                continue

            # 日线窗口: 截至 T-1 的完成态日K (index of last panel date < day)。
            prior = [d for d in panel_dates if d < day]
            if not prior:
                # 面板起点之前的分区日 (窗口数据不足), 策略按数据不足自然不命中。
                daily_history = pl.DataFrame()
                current = pl.DataFrame()
            else:
                last_prior = prior[-1]
                window_start, _ = date_to_window[last_prior]
                daily_history = panel.filter(
                    (pl.col("date") >= window_start) & (pl.col("date") <= last_prior)
                ) if daily_bars > 0 else pl.DataFrame()
                current = panel.filter(pl.col("date") == last_prior)

                # T-1 收盘/名称 + T 日复权因子查表。
                _refresh_day_lookups(prev_raw_close, prev_name, current, prior)
                day_rows = panel.filter(pl.col("date") == day).select(
                    "symbol", "close", "raw_close",
                )
                adj_factor.clear()
                adj_factor.update(_adj_factors(day_rows))

            context = StrategyDataContext(
                asset_type="stock",
                timeframe="1m",
                as_of=day,
                current=current if not current.is_empty() else None,
                history=history,
                daily_history=daily_history if not daily_history.is_empty() else None,
            )
            try:
                run_result = self.strategy_engine.run(
                    strategy.meta.get("id", ""),
                    context,
                    pool,
                    params,
                    overrides,
                )
            except ValueError:
                # 单日执行失败 (如窗口缺列) 记为跳过, 不中断整个回放。
                result.skipped_days.append(day)
                continue

            result.replayed_days += 1
            result.strategy_matches += len(run_result.rows)
            for row in run_result.rows:
                symbol = row.get("symbol")
                close = row.get("close")
                if not symbol or close is None or float(close) <= 0:
                    continue
                raw_close = float(close)
                name = prev_name.get(str(symbol), "")
                prev = prev_raw_close.get(str(symbol))
                # 涨停拒买: 触发分钟收盘已达当日涨停价 (按 T-1 原始收盘 + 板块规则)。
                if prev is not None and prev > 0:
                    limit_up = _scalar_limit_up_price(
                        prev, price_limit_pct(str(symbol), day, is_risk_warning=is_risk_warning_name(name)),
                    )
                    if raw_close >= limit_up - 1e-9:
                        result.buy_limit_up += 1
                        continue
                trigger = row.get("last_datetime")
                trigger_time = _trigger_hhmm(trigger)
                result.hits.append(MinuteReplayHit(
                    trade_date=day,
                    symbol=str(symbol),
                    entry_price=raw_close * adj_factor.get(str(symbol), 1.0),
                    trigger_time=trigger_time,
                    score=float(run_result.scores.get(str(symbol), 0.0) or 0.0),
                ))

        result.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return result


def _refresh_day_lookups(
    prev_raw_close: dict[str, float],
    prev_name: dict[str, str],
    prior_snapshot: pl.DataFrame,
    prior: list[date],
) -> None:
    """从 T-1 快照刷新逐标的原始收盘与名称查表 (涨停价/ST 判定用)。"""
    if prior_snapshot.is_empty():
        return
    frame = prior_snapshot
    if "raw_close" not in frame.columns:
        frame = frame.with_columns(pl.col("close").alias("raw_close"))
    if "name" not in frame.columns:
        frame = frame.with_columns(pl.lit("").alias("name"))
    prev_raw_close.clear()
    prev_name.clear()
    for symbol, raw_close, name in frame.select("symbol", "raw_close", "name").iter_rows():
        prev_raw_close[str(symbol)] = float(raw_close) if raw_close is not None else 0.0
        prev_name[str(symbol)] = str(name or "")


def _adj_factors(day_rows: pl.DataFrame) -> dict[str, float]:
    """T 日 复权close/原始close 比例: 把分钟原始价折算到复权价系。"""
    factors: dict[str, float] = {}
    if day_rows.is_empty() or "raw_close" not in day_rows.columns:
        return factors
    for symbol, close, raw_close in day_rows.select("symbol", "close", "raw_close").iter_rows():
        if close and raw_close and float(raw_close) > 0:
            factors[str(symbol)] = float(close) / float(raw_close)
    return factors
