"""策略回测服务 — 复用 StrategyDef 体系做全周期回测。

核心优化: 向量化 filter_fn，不逐日调用 StrategyEngine.run()。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Callable, Literal

import numpy as np
import polars as pl
from app.backtest.metrics import MetricContext, probabilistic_sharpe_ratio, relative_performance_metrics
from app.backtest.robustness import returns_from_equity_curve
from app.backtest.universe_gating import apply_listing_age_gate
from app.backtest.engine import BacktestEngine, MatcherConfig
from app.strategy.engine import StrategyDef, StrategyEngine

if TYPE_CHECKING:
    import threading

logger = logging.getLogger(__name__)

BENCHMARK_SYMBOL = "000001.INDEX"
BENCHMARK_NAMES = {
    "000001.INDEX": "上证指数",
    "000300.INDEX": "沪深300",
    "000905.INDEX": "中证500",
    "000852.INDEX": "中证1000",
}

# 裸码指数前缀: 000 (上证系列) / 399 (深证系列) / 880 (板块指数) 与
# tencent_quote 等既有口径一致。
_INDEX_CODE_PREFIXES = ("000", "399", "880")


def _infer_benchmark_asset_type(symbol: str) -> str:
    """基准代码的 asset_type 推断 (fail-soft):

    - 显式 ``.INDEX`` 后缀 → index;
    - 显式股票后缀 (.SH/.SZ/.BJ) → stock;
    - 裸码按指数前缀规则 (000/399/880 开头) → index (000001 上证指数口径);
    - 其余/无法识别 → stock (查询落空时降级无基准, 不伪造)。
    """
    from app.data_providers.fquant.symbols import split_symbol

    code, suffix = split_symbol(str(symbol).strip().upper())
    if suffix == "INDEX":
        return "index"
    if suffix in ("SH", "SZ", "BJ"):
        return "stock"
    if code.startswith(_INDEX_CODE_PREFIXES):
        return "index"
    return "stock"


@dataclass
class StrategyBacktestConfig:
    strategy_id: str
    symbols: list[str] | None
    start: date
    end: date
    params: dict | None = None
    overrides: dict | None = None
    # matching 为向后兼容入口; 显式传 entry_fill/exit_fill 时以二者为准。
    matching: Literal["close_t", "open_t+1"] = "open_t+1"
    entry_fill: Literal["close_t", "open_t+1"] | None = None
    exit_fill: Literal["close_t", "open_t+1"] | None = None
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0
    # A 股印花税: 仅卖出单边收取, 2023-08 起默认 0.0005 (万分之五)。
    stamp_tax_pct: float = 0.0005
    max_positions: int = 10
    max_exposure_pct: float = 1.0
    initial_capital: float = 1_000_000.0
    position_sizing: Literal[
        "equal", "score_weight", "equal_vol", "risk_parity", "mean_variance", "max_diversification",
    ] = "equal"
    mode: Literal["position", "full"] = "position"
    holding_days: int = 5
    regime_filter: dict | None = None  # {states: [...], min_score?: float}; None/空 → 不过滤
    # F9 自定义基准: 与 benchmark_run_id 互斥 (API 层校验)。benchmark_symbol
    # 除 4 指数白名单外接受任意标的代码 (6 位裸码/带后缀), 非 index 白名单
    # 走单标的日K加载。
    benchmark_symbol: str = BENCHMARK_SYMBOL
    # 历史 Run 净值基准: run_id。净值由 API 层从 run_store 解析后经
    # run(benchmark_run=...) 传入, config 只持久化 id 本身。
    benchmark_run_id: str | None = None
    risk_free_rate: float = 0.0
    # A1 量能参与率约束: None=关闭; 启用时如 0.10 表示单笔买入不超过
    # min(当日量, participation_volume_window 日均量) 的 10%。透传给 MatcherConfig。
    max_participation_pct: float | None = None
    participation_volume_window: int = 5
    # B6 上市天数门控: 上市未满 N 天的标的不进入回测面板 (买入候选与持仓
    # 均不可能出现)。0=关闭。门控由 run(listing_dates=...) 提供。
    min_listed_days: int = 0

    def __post_init__(self) -> None:
        if self.entry_fill is None:
            self.entry_fill = self.matching
        if self.exit_fill is None:
            self.exit_fill = self.matching
        if self.min_listed_days < 0:
            raise ValueError("min_listed_days 必须为非负整数")

    @staticmethod
    def _regime_filter_allowed_states(rf: dict | None) -> set[str] | None:
        if not rf or not isinstance(rf, dict):
            return None
        states = rf.get("states")
        if not states or not isinstance(states, list):
            return None
        return {s for s in states if isinstance(s, str)}

    @staticmethod
    def _regime_filter_min_score(rf: dict | None) -> float | None:
        if not rf or not isinstance(rf, dict):
            return None
        ms = rf.get("min_score")
        if ms is None:
            return None
        try:
            return float(ms)
        except (TypeError, ValueError):
            return None


@dataclass
class StrategyBacktestResult:
    run_id: str
    config: dict
    stats: dict = field(default_factory=dict)
    equity_curve: list[dict] = field(default_factory=list)
    drawdown_curve: list[dict] = field(default_factory=list)
    benchmark_curve: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    per_symbol_stats: list[dict] = field(default_factory=list)
    attribution: dict | None = None
    strategy_info: dict = field(default_factory=dict)
    # 执行过程告警 (如基准降级), API 层并入 payload warnings。
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    error: str | None = None


class StrategyBacktestService:
    def __init__(
        self,
        engine: BacktestEngine,
        strategy_engine: StrategyEngine,
    ) -> None:
        self.engine = engine
        self.strategy_engine = strategy_engine

    def run(
        self,
        config: StrategyBacktestConfig,
        progress_cb: "Callable[[dict], None] | None" = None,
        cancel_event: "threading.Event | None" = None,
        *,
        panel: "pl.DataFrame | None" = None,
        listing_dates: "pl.DataFrame | None" = None,
        # F9 历史 Run 净值基准: {"run_id": str, "label": str, "equity_curve": [...]}
        # 由 API 层从 run_store 解析传入; None = 走 benchmark_symbol 路径。
        benchmark_run: "dict | None" = None,
    ) -> StrategyBacktestResult:
        t0 = time.perf_counter()
        run_id = uuid.uuid4().hex[:16]
        # 执行过程告警 (基准降级等), 随结果返回由 API 层并入 payload warnings。
        benchmark_warnings: list[str] = []

        def _err(msg: str) -> StrategyBacktestResult:
            return StrategyBacktestResult(
                run_id=run_id,
                config=self._config_to_dict(config),
                error=msg,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
        # fail-closed: benchmark_run_id 必须伴随已解析的 benchmark_run 净值;
        # 缺失说明调用路径 (诊断端点等) 未接 run 基准解析, 不能静默退回默认指数。
        if config.benchmark_run_id and benchmark_run is None:
            return _err(
                f"benchmark_run_id={config.benchmark_run_id} 未提供解析后的 Run 净值，"
                "该调用路径暂不支持历史 Run 基准"
            )

        # 获取策略定义
        try:
            s = self.strategy_engine.get(config.strategy_id)
        except ValueError as e:
            return _err(str(e))

        params = self._normalize_params(config.params or {}, s)
        overrides = config.overrides or {}
        basic_filter = self._effective_basic_filter(s, overrides)
        entry_signals = self._effective_signals(overrides, "entry_signals", s.entry_signals)
        exit_signals = self._effective_signals(overrides, "exit_signals", s.exit_signals)
        stop_loss = self._override_value(overrides, "stop_loss", s.stop_loss)
        take_profit = self._normalize_pct(
            self._override_value(overrides, "take_profit", getattr(s, "take_profit", None)),
            0.01,
            5.0,
        )
        trailing_stop = self._normalize_pct(
            self._override_value(overrides, "trailing_stop", getattr(s, "trailing_stop", None)),
            0.005,
            0.5,
        )
        trailing_take_profit_activate = self._normalize_pct(
            self._override_value(overrides, "trailing_take_profit_activate", getattr(s, "trailing_take_profit_activate", None)),
            0.01,
            2.0,
        )
        trailing_take_profit_drawdown = self._normalize_pct(
            self._override_value(overrides, "trailing_take_profit_drawdown", getattr(s, "trailing_take_profit_drawdown", None)),
            0.005,
            0.5,
        )
        if trailing_take_profit_activate is not None and trailing_take_profit_drawdown is not None:
            trailing_take_profit_drawdown = min(trailing_take_profit_drawdown, trailing_take_profit_activate)
        max_hold_days = self._override_value(overrides, "max_hold_days", s.max_hold_days)
        score_min, score_max = self._normalize_score_range(
            overrides.get("score_min"),
            overrides.get("score_max"),
        )

        timing_ms: dict[str, float] = {}

        def _emit_stage(stage: str, label: str, *, day: int = 0, total: int = 0, date: str = "") -> None:
            if progress_cb is None:
                return
            try:
                progress_cb({
                    "stage": stage,
                    "label": label,
                    "day": day,
                    "total": total,
                    "date": date,
                    "elapsed_ms": (time.perf_counter() - t0) * 1000,
                })
            except Exception:  # noqa: BLE001
                logger.debug("strategy backtest stage callback failed", exc_info=True)

        # 加载面板 (含 warmup + 全量指标 + 信号)。warmup 只用于指标/形态计算, 不参与正式交易。
        # 全量模式: entries 只在正式区间触发, exits 需要 end 之后的尾部数据继续执行策略卖点。
        load_start, load_end, full_horizon_days = self._compute_load_range(s, config, max_hold_days)

        t_load = time.perf_counter()
        _emit_stage("loading", "加载行情面板")
        if panel is None:
            panel = self.engine.load_panel(config.symbols, load_start, load_end)
        timing_ms["load_panel"] = round((time.perf_counter() - t_load) * 1000, 1)
        if panel.is_empty():
            return _err("无数据，请检查日期范围或先运行盘后管道")
        # B6 上市天数门控: 删行实现 (次新股整段不入面板)。空仓起点下无持仓
        # 持续性问题; 门控统计进 stats, 后续 provenance 告警由 API 层按需附加。
        listing_gate_stats: dict = {"enabled": False}
        if config.min_listed_days > 0 and listing_dates is not None and not listing_dates.is_empty():
            try:
                panel, listing_gate_stats = apply_listing_age_gate(
                    panel, listing_dates, config.min_listed_days,
                )
            except ValueError as e:
                return _err(f"上市天数门控失败: {e}")
        elif config.min_listed_days > 0:
            # 请求了门控但没有上市日期数据 → 显式告警, 不静默忽略。
            listing_gate_stats = {
                "enabled": False, "requested": True,
                "reason": "上市日期数据不可用, 门控未生效",
            }
        listing_gate_payload = {"listing_age_gate": listing_gate_stats}
        formal_range = self._date_range_mask(panel, config.start, config.end)
        if not formal_range.any():
            return _err("正式回测区间内无数据")

        t_signal = time.perf_counter()
        _emit_stage("signals", "计算信号与评分")
        # basic_filter 只影响买入候选, 不能删除行情 panel, 否则持仓 mark / 卖出 / full forward return 都会失真。
        basic_mask = pl.Series("_basic", [True] * len(panel), dtype=pl.Boolean)
        if basic_filter and basic_filter.get("enabled", True):
            expr = StrategyEngine._basic_filter_expr(panel, basic_filter)
            if expr is not None:
                try:
                    basic_mask = panel.select(expr.alias("_basic"))["_basic"].fill_null(False).cast(pl.Boolean)
                except Exception as e:  # noqa: BLE001
                    logger.warning("basic_filter mask failed: %s", e)
                    return _err(f"基础过滤计算失败: {e}")

        if getattr(s, "execution_backend", "polars_expr") == "composite":
            # 叠加策略回测: 逐子策略构建 entry/exit/score 掩码后合并。
            # 退出采用来源投影(每个子策略 exit 仅在自己持仓窗口生效, 不串平其他子的仓位)。
            entry_mask, exit_mask, panel = self._build_composite_masks(
                panel, s, overrides, params, basic_mask,
                formal_range=formal_range, load_end=load_end, config=config,
            )
        else:
            # 策略候选层用于评分归一化；entry_signals 只是买点层, 不参与 score universe。
            candidate_filter_mask = self._build_candidate_filter_mask(panel, s, params)
            candidate_mask = basic_mask & candidate_filter_mask
            panel = self._apply_score(panel, s, overrides, universe_mask=candidate_mask)

            entry_mask = self._build_entry_mask_from_candidate(panel, candidate_mask, s, entry_signals)
            entry_mask = entry_mask & formal_range
            entry_mask = entry_mask & self._apply_regime_t1_mask(panel, config)
            raw_exit_mask = self._build_signal_mask(panel, exit_signals, "_exit")
            exit_mask = raw_exit_mask & (self._date_range_mask(panel, config.start, load_end) if config.mode == "full" else formal_range)
        timing_ms["signals_score"] = round((time.perf_counter() - t_signal) * 1000, 1)

        if not entry_mask.any():
            return _err("在指定区间内未产生买入信号")

        # warmup 之后才交给撮合；full mode 保留 end 之后前瞻段用于 shift(-N)。
        sim_end = load_end if config.mode == "full" else config.end
        sim_range = self._date_range_mask(panel, config.start, sim_end)
        sim_panel = panel.filter(sim_range)
        sim_entry_mask = entry_mask.filter(sim_range)
        sim_exit_mask = exit_mask.filter(sim_range)
        if sim_panel.is_empty():
            return _err("正式回测区间内无数据")

        t_sim = time.perf_counter()
        matcher_config = MatcherConfig(
            matching=config.matching,
            entry_fill=config.entry_fill,
            exit_fill=config.exit_fill,
            fees_pct=config.fees_pct,
            stamp_tax_pct=config.stamp_tax_pct,
            slippage_bps=config.slippage_bps,
            stop_loss_pct=stop_loss,
            take_profit_pct=take_profit,
            trailing_stop_pct=trailing_stop,
            trailing_take_profit_activate_pct=trailing_take_profit_activate,
            trailing_take_profit_drawdown_pct=trailing_take_profit_drawdown,
            max_hold_days=max_hold_days,
            max_positions=config.max_positions,
            max_exposure_pct=config.max_exposure_pct,
            score_min=score_min,
            score_max=score_max,
            initial_capital=config.initial_capital,
            position_sizing=config.position_sizing,
            risk_free_rate=config.risk_free_rate,
            max_participation_pct=config.max_participation_pct,
            participation_volume_window=config.participation_volume_window,
        )
        # 撮合 — full 为全候选独立执行；position 为账户级仓位模拟。
        def _sim_progress(evt: dict) -> None:
            if progress_cb is None:
                return
            payload = dict(evt)
            payload.setdefault("stage", "simulate")
            payload.setdefault("label", "撮合模拟")
            payload.setdefault("elapsed_ms", (time.perf_counter() - t0) * 1000)
            progress_cb(payload)

        _emit_stage("simulate", "撮合模拟")
        if config.mode == "full":
            result = self.engine.simulate_independent_candidates(
                sim_panel,
                sim_entry_mask,
                sim_exit_mask,
                matcher_config,
                _sim_progress,
                cancel_event,
            )
        else:
            result = self.engine.simulate_portfolio(
                sim_panel, sim_entry_mask, sim_exit_mask, matcher_config, _sim_progress, cancel_event,
            )
        timing_ms["simulate"] = round((time.perf_counter() - t_sim) * 1000, 1)

        # 检查是否被取消
        if cancel_event is not None and cancel_event.is_set():
            return StrategyBacktestResult(
                run_id=run_id,
                config=self._config_to_dict(config),
                error="cancelled",
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            )

        if result.stats.get("error"):
            return _err(result.stats["error"])

        timing_ms["total"] = round((time.perf_counter() - t0) * 1000, 1)
        result.stats["timing_ms"] = timing_ms
        result.stats["panel_rows"] = int(sim_panel.height)
        # F13 定义指纹: 回测时所用策略定义的指纹随 Run 持久化 (stats 整体落盘),
        # 前端与当前策略列表 def_hash 比对提示「策略定义已变更」。候选执行模式
        # 同样写入 —— 它是定义指纹, 不是时序指标; composite/寻优临时组合亦覆盖。
        result.stats["strategy_def_hash"] = s.def_hash

        is_candidate_execution = result.stats.get("full_kind") == "candidate_execution"
        benchmark_source: dict = {"kind": "none", "label": ""}
        if is_candidate_execution:
            # 全量候选曲线只在退出事件日采样，既不是账户净值也不是连续日频收益。
            # 禁止将其与基准对齐或生成 Alpha/Beta/IR 等时间序列相对指标。
            result.stats["time_series_metrics_available"] = False
            result.stats["curve_semantics"] = "candidate_exit_event_average"
            benchmark_curve: list[dict] = []
        else:
            # benchmark 窗口与组合净值实际区间对齐: position 模式下实际数据覆盖可能窄于
            # 请求区间 (equity 已被钳制在 [start, end] 内)，避免相对指标错位。
            benchmark_start = config.start
            benchmark_end = config.end
            if result.equity_curve:
                equity_dates = [
                    date.fromisoformat(str(row["date"])[:10])
                    for row in result.equity_curve
                    if row.get("date")
                ]
                if equity_dates:
                    benchmark_start = min(equity_dates)
                    benchmark_end = max(equity_dates)
            # F9: 基准解析统一入口 (指数白名单 / 自定义标的 / 历史 Run 净值),
            # 失败降级空曲线 + warning (fail-closed, 不伪造基准序列)。
            benchmark_curve, benchmark_source = self._resolve_benchmark(
                benchmark_start,
                benchmark_end,
                symbol=config.benchmark_symbol,
                benchmark_run=benchmark_run,
                warnings_out=benchmark_warnings,
            )
        # 基准来源标注: 前端据 kind 显示 "指数/自定义标的/历史 Run/不可用"。
        result.stats["benchmark_source"] = benchmark_source if not is_candidate_execution else {
            "kind": "none",
            "label": str(config.benchmark_symbol),
        }
        if not is_candidate_execution:
            # 候选执行模式不产出任何基准/相对指标键 (含 None 值)——键存在性即契约。
            closes = [row["close"] for row in benchmark_curve if row.get("close")]
            if len(closes) >= 2 and closes[0] > 0:
                benchmark_return = closes[-1] / closes[0] - 1
                result.stats["benchmark_return"] = round(float(benchmark_return), 4)
                total_return = result.stats.get("total_return")
                if isinstance(total_return, (int, float)):
                    result.stats["excess"] = round(float(total_return) - benchmark_return, 4)
            result.stats.update(self._relative_stats(
                result.equity_curve, benchmark_curve, config.risk_free_rate,
            ))

        # 构建策略信息
        strategy_info = {
            "id": s.meta.get("id", config.strategy_id),
            "name": s.meta.get("name", config.strategy_id),
            "description": s.meta.get("description", ""),
            "entry_signals": entry_signals,
            "exit_signals": exit_signals,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "trailing_stop": trailing_stop,
            "trailing_take_profit_activate": trailing_take_profit_activate,
            "trailing_take_profit_drawdown": trailing_take_profit_drawdown,
            "max_hold_days": max_hold_days,
            "full_horizon_days": full_horizon_days,
            "score_min": score_min,
            "score_max": score_max,
            "source": s.source,
            "execution_backend": getattr(s, "execution_backend", "polars_expr"),
            "composite_children": (
                [
                    {"id": c.strategy_id, "weight": c.weight}
                    for c in s.composite.children
                ]
                if getattr(s, "execution_backend", "polars_expr") == "composite" and s.composite is not None
                else None
            ),
        }

        # B6 门控统计进 stats (对齐 capacity 等执行诊断口径)。
        result.stats.update(listing_gate_payload)

        # A3 PSR: 仅仓位模拟 (连续日频净值) 才有账户级口径; 候选执行曲线
        # 按退出事件日采样, 不适用。
        if not is_candidate_execution and result.equity_curve:
            mc = MetricContext("daily", risk_free_rate=config.risk_free_rate)
            rets = returns_from_equity_curve(result.equity_curve)
            result.stats["psr"] = probabilistic_sharpe_ratio(rets, mc)

        elapsed = (time.perf_counter() - t0) * 1000
        trades = [self._trade_to_dict(t) for t in result.trades]
        attribution = self._build_trade_attribution(trades, candidate_execution=is_candidate_execution)


        return StrategyBacktestResult(
            run_id=run_id,
            config=self._config_to_dict(config),
            stats=result.stats,
            equity_curve=result.equity_curve,
            drawdown_curve=result.drawdown_curve,
            benchmark_curve=benchmark_curve,
            trades=trades,
            per_symbol_stats=result.per_symbol_stats,
            attribution=attribution,
            strategy_info=strategy_info,
            warnings=benchmark_warnings,
            elapsed_ms=round(elapsed, 1),
        )

    # ── 叠加策略回测: 掩码合并 ──

    def _build_composite_masks(
        self,
        panel: pl.DataFrame,
        s: StrategyDef,
        overrides: dict,
        params: dict,
        basic_mask: pl.Series,
        *,
        formal_range: pl.Series,
        load_end: date,
        config: "StrategyBacktestConfig",
    ) -> tuple[pl.Series, pl.Series, pl.DataFrame]:
        """叠加策略: 逐子策略构建 entry/exit/score → 合并为统一掩码。

        合并语义(与选股 merge_results 同口径):
        - entry: union=OR(各子 entry); intersect=Σ(entries) >= min_confirm
        - exit: 来源投影。每个子策略 i 的 exit 仅在自己 entry 后的持仓窗口内生效,
          避免"B 的退出信号平掉 A 的仓位"。窗口由全局 max_hold 封顶。
        - score: 各子内部按 score 降序排名归一到 [0,1](跨子策略可比), 命中子策略间按权重加权。
        """
        from app.strategy.engine import _parse_composite_children

        assert s.composite is not None

        override_children = overrides.get("children")
        if isinstance(override_children, list) and override_children:
            children = _parse_composite_children(override_children).children
        else:
            children = s.composite.children

        child_ids = [c.strategy_id for c in children]
        child_weights = [c.weight for c in children]
        merge_mode = str(params.get("merge_mode") or "union")
        min_confirm = int(params.get("min_confirm") or 0)

        # 退出投影窗口: 与撮合层 max_hold 一致; 无上限时取 250 交易日封顶。
        max_hold = self._override_value(overrides, "max_hold_days", s.max_hold_days)
        composite_max_hold = int(max_hold) if max_hold else 250
        composite_max_hold = max(composite_max_hold, 1)

        shared_bf = self._effective_basic_filter(s, overrides)
        override_loader = getattr(self.strategy_engine, "_override_loader", None)

        entry_masks: list[pl.Series] = []
        exit_masks: list[pl.Series] = []
        child_scores: list[pl.Series] = []

        for idx, cid in enumerate(child_ids):
            child_def = self.strategy_engine.get(cid)
            child_override: dict = {}
            if override_loader is not None:
                try:
                    loaded = override_loader(cid)
                    if isinstance(loaded, dict):
                        child_override = dict(loaded)
                except Exception:  # noqa: BLE001
                    pass
            if shared_bf:
                child_override["basic_filter"] = shared_bf
            child_params = self._normalize_params({}, child_def)

            # 候选层 AND composite basic_filter; entry = 候选 AND 买点信号。
            candidate = self._build_candidate_filter_mask(panel, child_def, child_params) & basic_mask
            child_entry_signals = self._effective_signals(
                child_override, "entry_signals", child_def.entry_signals
            )
            child_exit_signals = self._effective_signals(
                child_override, "exit_signals", child_def.exit_signals
            )
            entry_m = self._build_entry_mask_from_candidate(
                panel, candidate, child_def, child_entry_signals
            )
            exit_m = self._build_signal_mask(panel, child_exit_signals, f"_cexit_{idx}")

            # 子策略 score: 复用 _apply_score(子策略自身口径), 取 score 列。
            scored = self._apply_score(panel, child_def, child_override, universe_mask=candidate)
            child_scores.append(
                scored["score"] if "score" in scored.columns
                else pl.Series(f"_cscore_{idx}", [0.0] * len(panel))
            )
            entry_masks.append(entry_m)
            exit_masks.append(exit_m)

        # ── 合并 entry ──
        if merge_mode == "intersect":
            effective_min = max(min_confirm, 1) if min_confirm and min_confirm > 0 else len(child_ids)
            hit_count = entry_masks[0].cast(pl.Int32)
            for m in entry_masks[1:]:
                hit_count = hit_count + m.cast(pl.Int32)
            merged_entry = hit_count >= effective_min
        else:
            merged_entry = entry_masks[0]
            for m in entry_masks[1:]:
                merged_entry = merged_entry | m

        # ── 合并 exit (来源投影) ──
        merged_exit = self._merge_exit_with_projection(
            panel, entry_masks, exit_masks, composite_max_hold
        )

        # ── 合并 score (排名归一加权) ──
        composite_score = self._composite_ranked_score(
            panel, child_scores, entry_masks, child_weights
        )
        panel = panel.with_columns(composite_score.alias("score"))

        entry_mask = merged_entry & formal_range
        # composite 入场同样受 regime T-1 过滤约束(与普通策略口径一致)。
        entry_mask = entry_mask & self._apply_regime_t1_mask(panel, config)
        full_or_formal = (
            self._date_range_mask(panel, config.start, load_end)
            if config.mode == "full" else formal_range
        )
        exit_mask = merged_exit & full_or_formal
        return entry_mask, exit_mask, panel

    @staticmethod
    def _hold_window_mask(panel: pl.DataFrame, entry_mask: pl.Series, max_hold: int) -> pl.Series:
        """计算持仓窗口掩码: entry 后 max_hold 个 bar 内为 True(同 symbol)。

        panel 须按 [symbol, date] 排序(回测面板保证)。实现用前向填充 last-entry 行号:
        hold[t] = True 当且仅当存在 t'<=t 使 entry[t']=True 且 t-t' < max_hold。
        """
        if max_hold <= 0:
            max_hold = 1
        work = panel.select("symbol").with_columns(
            entry_mask.cast(pl.Boolean).alias("_e"),
            pl.int_range(pl.len()).over("symbol").alias("_rn"),
        )
        work = work.with_columns(
            pl.when(pl.col("_e"))
            .then(pl.col("_rn"))
            .otherwise(None)
            .forward_fill()
            .over("symbol")
            .alias("_last_entry")
        )
        hold_expr = (
            pl.col("_last_entry").is_not_null()
            & ((pl.col("_rn") - pl.col("_last_entry")) < max_hold)
        )
        return work.select(hold_expr.alias("_hold"))["_hold"].fill_null(False).cast(pl.Boolean)

    def _merge_exit_with_projection(
        self,
        panel: pl.DataFrame,
        entry_masks: list[pl.Series],
        exit_masks: list[pl.Series],
        max_hold: int,
    ) -> pl.Series:
        """退出来源投影: 每个子策略的 exit 仅在自己持仓窗口内生效, 再 OR 合并。"""
        n = len(panel)
        merged = pl.Series("_exit_merged", [False] * n, dtype=pl.Boolean)
        for entry_m, exit_m in zip(entry_masks, exit_masks, strict=True):
            hold = self._hold_window_mask(panel, entry_m, max_hold)
            merged = merged | (exit_m & hold)
        return merged

    @staticmethod
    def _composite_ranked_score(
        panel: pl.DataFrame,
        child_scores: list[pl.Series],
        entry_masks: list[pl.Series],
        child_weights: list[float],
    ) -> pl.Series:
        """各子内部按 score 降序排名归一到 [0,1](跨子策略可比), 命中子策略间按权重加权。

        单候选/无 score 用中性分 0.5。结果 *100 对齐 _apply_score 的 0~100 量纲。
        rank/over 必须作用在 DataFrame 列上, 不能对游离 Series 做窗口, 否则多日分组会长度错位。
        """
        if not child_scores:
            return pl.Series("score", [0.0] * len(panel), dtype=pl.Float64)
        work = panel.select(pl.col("date"))
        weight_sum: pl.Expr = pl.lit(0.0)
        blended: pl.Expr = pl.lit(0.0)
        for idx, (score_col, entry_m) in enumerate(zip(child_scores, entry_masks, strict=True)):
            w = float(child_weights[idx]) if idx < len(child_weights) else 1.0
            s_name, e_name, h_name, r_name, c_name = (
                f"_s{idx}", f"_e{idx}", f"_hs{idx}", f"_rk{idx}", f"_hc{idx}",
            )
            work = work.with_columns(
                score_col.alias(s_name),
                entry_m.cast(pl.Boolean).alias(e_name),
            ).with_columns(
                pl.when(pl.col(e_name)).then(pl.col(s_name)).otherwise(None).alias(h_name),
            ).with_columns(
                pl.col(h_name).rank(method="ordinal", descending=True).over("date").alias(r_name),
                pl.col(h_name).is_not_null().sum().over("date").alias(c_name),
            )
            norm = pl.when(pl.col(e_name)).then(
                pl.when(pl.col(c_name) > 1).then(
                    1.0 - (pl.col(r_name).cast(pl.Float64) - 1.0) / (pl.col(c_name).cast(pl.Float64) - 1.0)
                ).otherwise(pl.lit(0.5))
            ).otherwise(0.0)
            weight_sum = weight_sum + pl.when(pl.col(e_name)).then(pl.lit(w)).otherwise(0.0)
            blended = blended + pl.when(pl.col(e_name)).then(norm * w).otherwise(0.0)
        safe = pl.when(weight_sum > 0).then(weight_sum).otherwise(pl.lit(1.0))
        return work.select((blended / safe * 100.0).fill_null(0.0).fill_nan(0.0).alias("score"))["score"]

    # ── 全量模拟 (选股能力统计, 不建组合不算净值) ──

    @staticmethod
    def _compute_load_range(
        s: "StrategyDef",
        config: "StrategyBacktestConfig",
        max_hold_days: int | None,
    ) -> "tuple[date, date, int]":
        """计算 panel 加载区间 (含 warmup) 与 full 模式前瞻天数。

        warmup 只用于指标/形态计算, 不参与正式交易。
        full 模式: entries 只在正式区间触发, exits 需要 end 之后尾部数据继续执行卖点。
        """
        warmup_days = max(120, int(max(s.lookback_days or 1, 1) * 1.5))
        load_start = config.start - timedelta(days=warmup_days)
        full_horizon_days = max(int(max(max_hold_days or config.holding_days or 5, 1)), 1)
        load_end = config.end
        if config.mode == "full":
            fwd_buffer = full_horizon_days + 5  # 多取几天, 容错停牌缺口/open_t+1
            load_end = config.end + timedelta(days=fwd_buffer * 2)
        return load_start, load_end, full_horizon_days

    def compute_load_range(self, config: "StrategyBacktestConfig") -> "tuple[date, date]":
        """计算 config 对应的 panel 加载区间 (含 warmup)。

        供参数网格预加载共享 panel: 所有 grid scenario 的 load 区间相同
        (仅 params 不同, 不影响 warmup/horizon), 故可复用同一份 panel。
        """
        s = self.strategy_engine.get(config.strategy_id)
        max_hold_days = self._override_value(config.overrides or {}, "max_hold_days", s.max_hold_days)
        load_start, load_end, _ = self._compute_load_range(s, config, max_hold_days)
        return load_start, load_end


    # ── 向量化信号生成 ──

    @staticmethod
    def _date_range_mask(panel: pl.DataFrame, start: date, end: date) -> pl.Series:
        return panel.select(
            ((pl.col("date") >= start) & (pl.col("date") <= end)).alias("_range")
        )["_range"].fill_null(False).cast(pl.Boolean)

    def _build_candidate_filter_mask(
        self,
        panel: pl.DataFrame,
        s: StrategyDef,
        params: dict,
    ) -> pl.Series:
        """生成策略候选层 mask。filter_history/filter 决定候选池, 不包含 entry_signals。"""
        false_mask = pl.Series("_candidate_filter", [False] * len(panel), dtype=pl.Boolean)
        true_mask = pl.Series("_candidate_filter", [True] * len(panel), dtype=pl.Boolean)

        history_failed = False
        # 优先: filter_history_fn 策略 (涨停/反包等多日形态, 与选股路径共用同一逻辑)
        if s.filter_history_fn:
            try:
                hit_df = s.filter_history_fn(panel, params)
                if hit_df is None or hit_df.is_empty():
                    return false_mask
                # 命中行 (symbol,date) → 转 panel 等长布尔 mask
                hits = hit_df.select(["symbol", "date"]).unique()
                marked = (
                    panel.select(["symbol", "date"])
                    .join(
                        hits.with_columns(pl.lit(True).alias("_hit")),
                        on=["symbol", "date"],
                        how="left",
                    )
                )
                return marked["_hit"].fill_null(False).cast(pl.Boolean)
            except Exception as e:
                history_failed = True
                logger.warning("strategy filter_history_fn failed: %s", e)
                # 失败则回退到 filter_fn (若存在)

        # 策略 filter_fn: 候选层 (filter_history 不可用或失败时)
        if s.filter_fn:
            try:
                expr = s.filter_fn(panel, params)
                if expr is not None:
                    result = panel.select(expr.alias("_candidate_filter"))
                    if not result.is_empty():
                        return result["_candidate_filter"].fill_null(False).cast(pl.Boolean)
            except Exception as e:
                logger.warning("strategy filter_fn failed: %s", e)
                return false_mask

        if history_failed:
            return false_mask

        # 没有策略候选层时, 由 entry_signals 直接决定买点。
        return true_mask

    def _build_entry_mask_from_candidate(
        self,
        panel: pl.DataFrame,
        candidate_mask: pl.Series,
        s: StrategyDef,
        entry_signals: list[str],
    ) -> pl.Series:
        """向量化生成买入掩码：候选层 AND 买点层；无买点时只用策略候选层。"""
        signal_mask = self._build_signal_mask(panel, entry_signals, "_entry_signal")
        if entry_signals:
            return candidate_mask & signal_mask
        if s.filter_history_fn or s.filter_fn:
            return candidate_mask
        return pl.Series("_entry", [False] * len(panel), dtype=pl.Boolean)

    def _build_entry_mask(
        self,
        panel: pl.DataFrame,
        s: StrategyDef,
        params: dict,
        entry_signals: list[str],
    ) -> pl.Series:
        """兼容旧调用: 候选层 AND 买点层。"""
        candidate_mask = self._build_candidate_filter_mask(panel, s, params)
        return self._build_entry_mask_from_candidate(panel, candidate_mask, s, entry_signals)

    @staticmethod
    def _build_signal_mask(panel: pl.DataFrame, signals: list[str], name: str) -> pl.Series:
        """向量化合并信号列，多个信号 OR。支持内置 signal_ 与自定义 csg_ 前缀。"""
        masks: list[pl.Series] = []
        for sig in signals:
            # csg_ (自定义信号) 直接用；否则按 signal_ 解析
            col = sig if (sig.startswith("signal_") or sig.startswith("csg_")) else f"signal_{sig}"
            if col in panel.columns:
                masks.append(panel[col].fill_null(False).cast(pl.Boolean))

        if not masks:
            return pl.Series(name, [False] * len(panel), dtype=pl.Boolean)

        combined = masks[0]
        for m in masks[1:]:
            combined = combined | m
        return combined

    def _build_benchmark_curve(
        self,
        start: date,
        end: date,
        symbol: str = BENCHMARK_SYMBOL,
    ) -> list[dict]:
        benchmark_symbol = symbol if symbol in BENCHMARK_NAMES else BENCHMARK_SYMBOL
        try:
            df = self.engine.repo.get_index_daily(
                benchmark_symbol,
                start,
                end,
                columns=["date", "close"],
            )
        except Exception as e:
            logger.warning("load benchmark %s failed: %s", benchmark_symbol, e)
            return []

        if df.is_empty() or "close" not in df.columns:
            return []

        df = df.filter(pl.col("close").is_not_null() & (pl.col("close") > 0)).sort("date")
        if df.is_empty():
            return []

        return [
            {
                "date": str(row["date"])[:10],
                "value": round(float(row["close"]), 4),
                "close": round(float(row["close"]), 4),
                "name": BENCHMARK_NAMES[benchmark_symbol],
                "symbol": benchmark_symbol,
            }
            for row in df.iter_rows(named=True)
            if row["close"] is not None
        ]

    def _resolve_benchmark(
        self,
        start: date,
        end: date,
        *,
        symbol: str,
        benchmark_run: dict | None,
        warnings_out: list[str],
    ) -> tuple[list[dict], dict]:
        """基准解析统一入口 (F9)。

        优先级: benchmark_run (历史 Run 净值) > 指数白名单 > 自定义标的。
        任一路径加载失败/无数据 → 降级空曲线 + warning 注明基准代码
        (与既有缺失行为一致, 不伪造基准序列)。
        返回 (benchmark_curve, benchmark_source); source = {kind, label},
        kind ∈ {"index", "symbol", "run", "none"}。
        """
        if benchmark_run is not None:
            run_id = str(benchmark_run.get("run_id") or "")
            label = str(benchmark_run.get("label") or run_id)
            curve = self._build_run_benchmark_curve(start, end, benchmark_run)
            if not curve:
                warnings_out.append(
                    f"benchmark_unavailable: 基准 Run {run_id} 净值在回测区间内无可用数据，已降级为无基准对比"
                )
                return [], {"kind": "none", "label": label}
            return curve, {"kind": "run", "label": label}

        if symbol in BENCHMARK_NAMES:
            curve = self._build_benchmark_curve(start, end, symbol)
            if not curve:
                warnings_out.append(
                    f"benchmark_unavailable: 基准 {symbol} 数据缺失或加载失败，已降级为无基准对比"
                )
                return [], {"kind": "none", "label": symbol}
            return curve, {"kind": "index", "label": BENCHMARK_NAMES[symbol]}

        curve = self._build_custom_symbol_benchmark_curve(start, end, symbol)
        if not curve:
            warnings_out.append(
                f"benchmark_unavailable: 基准 {symbol} 无数据或加载失败，已降级为无基准对比"
            )
            return [], {"kind": "none", "label": symbol}
        return curve, {"kind": "symbol", "label": symbol}

    def _build_custom_symbol_benchmark_curve(
        self,
        start: date,
        end: date,
        symbol: str,
    ) -> list[dict]:
        """非白名单标的基准: asset_type 推断 (指数规则→index, 否则 stock) 后单标的日K加载。

        裸码按 exchange_of 补交易所后缀; 推断失败的 code 原样查询 (查不到则由
        调用方降级)。名称无法本地解析, 以代码本身作为展示 label。
        """
        from app.data_providers.fquant.symbols import canonical_index_symbol, exchange_of, split_symbol

        raw = str(symbol).strip().upper()
        asset_type = _infer_benchmark_asset_type(raw)
        query_symbol = raw
        code, suffix = split_symbol(raw)
        if asset_type == "index":
            query_symbol = canonical_index_symbol(raw)
        elif not suffix and code:
            # 裸码补后缀; 无法推断交易所时原样查询 (get_daily 按 symbol 精确匹配)。
            market = exchange_of(code)
            query_symbol = f"{code}.{market}" if market else raw

        try:
            if asset_type == "index":
                df = self.engine.repo.get_index_daily(
                    query_symbol, start, end, columns=["date", "close"],
                )
            else:
                df = self.engine.repo.get_daily(
                    query_symbol, start, end, columns=["date", "close"],
                )
        except Exception as e:
            logger.warning("load benchmark %s (%s) failed: %s", query_symbol, asset_type, e)
            return []

        if df.is_empty() or "close" not in df.columns:
            return []
        df = df.filter(pl.col("close").is_not_null() & (pl.col("close") > 0)).sort("date")
        if df.is_empty():
            return []
        return [
            {
                "date": str(row["date"])[:10],
                "value": round(float(row["close"]), 4),
                "close": round(float(row["close"]), 4),
                "name": raw,
                "symbol": query_symbol,
            }
            for row in df.iter_rows(named=True)
            if row["close"] is not None
        ]

    @staticmethod
    def _build_run_benchmark_curve(start: date, end: date, benchmark_run: dict) -> list[dict]:
        """历史 Run 净值基准: equity_curve 采样到 [start, end] 窗口。

        曲线形状与其他基准一致 (date/value/close/name/symbol); 收益与相对指标
        均为比值口径, 资金量纲不参与计算, 不做额外归一。
        """
        run_id = str(benchmark_run.get("run_id") or "")
        label = str(benchmark_run.get("label") or run_id)
        points: dict[str, float] = {}
        for row in benchmark_run.get("equity_curve") or []:
            d = str(row.get("date") or "")[:10]
            v = row.get("value", row.get("close"))
            if len(d) == 10 and isinstance(v, (int, float)) and float(v) > 0:
                points[d] = float(v)
        start_iso, end_iso = start.isoformat(), end.isoformat()
        return [
            {
                "date": d,
                "value": round(points[d], 4),
                "close": round(points[d], 4),
                "name": label,
                "symbol": run_id,
            }
            for d in sorted(points)
            if start_iso <= d <= end_iso
        ]
    @staticmethod
    def _relative_stats(
        equity_curve: list[dict],
        benchmark_curve: list[dict],
        risk_free_rate: float = 0.0,
    ) -> dict[str, float | None]:
        portfolio = {
            str(row.get("date"))[:10]: float(row["value"])
            for row in equity_curve
            if row.get("date") and isinstance(row.get("value"), (int, float)) and float(row["value"]) > 0
        }
        benchmark = {
            str(row.get("date"))[:10]: float(row.get("close", row.get("value")))
            for row in benchmark_curve
            if row.get("date")
            and isinstance(row.get("close", row.get("value")), (int, float))
            and float(row.get("close", row.get("value"))) > 0
        }
        dates = sorted(set(portfolio) & set(benchmark))
        portfolio_returns = [
            portfolio[current] / portfolio[previous] - 1.0
            for previous, current in zip(dates, dates[1:])
        ]
        benchmark_returns = [
            benchmark[current] / benchmark[previous] - 1.0
            for previous, current in zip(dates, dates[1:])
        ]
        return relative_performance_metrics(
            portfolio_returns,
            benchmark_returns,
            MetricContext("daily", risk_free_rate=risk_free_rate),
        )

    # ── 工具 ──

    @staticmethod
    def _effective_basic_filter(s: StrategyDef, overrides: dict) -> dict:
        basic_filter = dict(s.basic_filter or {})
        override_filter = overrides.get("basic_filter")
        if isinstance(override_filter, dict):
            basic_filter.update(override_filter)
        return basic_filter

    @staticmethod
    def _effective_signals(overrides: dict, key: str, default: list[str]) -> list[str]:
        value = overrides.get(key)
        if isinstance(value, list):
            return [str(v) for v in value if v]
        return list(default or [])

    @staticmethod
    def _override_value(overrides: dict, key: str, default):
        if key in overrides:
            return overrides.get(key)
        return default

    @staticmethod
    def _normalize_pct(value, min_value: float, max_value: float) -> float | None:
        if value is None or value == "":
            return None
        try:
            pct = abs(float(value))
        except (TypeError, ValueError):
            return None
        return min(max(pct, min_value), max_value)

    @staticmethod
    def _normalize_score_range(min_value, max_value) -> tuple[float | None, float | None]:
        def _bound(value) -> float | None:
            if value is None or value == "":
                return None
            try:
                score = float(value)
            except (TypeError, ValueError):
                return None
            if not np.isfinite(score):
                return None
            return min(max(score, 0.0), 100.0)

        score_min = _bound(min_value)
        score_max = _bound(max_value)
        if score_min is not None and score_max is not None and score_min > score_max:
            score_min, score_max = score_max, score_min
        return score_min, score_max

    @staticmethod
    def _normalize_params(params: dict, s: StrategyDef) -> dict:
        normalized = dict(params)
        for param in s.meta.get("params", []):
            pid = param.get("id")
            if not pid:
                continue
            value = normalized.get(pid, param.get("default"))
            p_type = param.get("type")
            if p_type in {"float", "int"}:
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    num = float(param.get("default", 0) or 0)
                if param.get("min") is not None:
                    num = max(num, float(param["min"]))
                if param.get("max") is not None:
                    num = min(num, float(param["max"]))
                normalized[pid] = int(num) if p_type == "int" else num
            elif p_type == "select" and param.get("options"):
                normalized[pid] = value if value in param["options"] else param.get("default")
            elif p_type == "bool":
                if isinstance(value, bool):
                    normalized[pid] = value
                elif isinstance(value, str):
                    normalized[pid] = value.lower() == "true"
                else:
                    normalized[pid] = bool(param.get("default", False))
            else:
                normalized[pid] = value
        return normalized

    @staticmethod
    def _trade_to_dict(t) -> dict:
        return {
            "symbol": t.symbol,
            "name": t.name,
            "entry_date": str(t.entry_date) if isinstance(t.entry_date, date) else str(t.entry_date),
            "exit_date": str(t.exit_date) if isinstance(t.exit_date, date) else str(t.exit_date),
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "pnl_pct": t.pnl_pct,
            "duration": t.duration,
            "exit_reason": t.exit_reason,
            "shares": t.shares,
            "lots": t.lots,
            "position_pct": t.position_pct,
            "entry_value": t.entry_value,
            "exit_value": t.exit_value,
            "pnl_amount": t.pnl_amount,
            "entry_score": getattr(t, "entry_score", None),
            "entry_signal_date": str(t.entry_signal_date) if getattr(t, "entry_signal_date", None) is not None else None,
            "exit_signal_date": str(t.exit_signal_date) if getattr(t, "exit_signal_date", None) is not None else None,
            "blocked_exit_days": getattr(t, "blocked_exit_days", 0),
            "cause_tag": getattr(t, "cause_tag", "strategy_outcome"),
            "mae_pct": getattr(t, "mae_pct", None),
            "mfe_pct": getattr(t, "mfe_pct", None),
        }

    def _build_trade_attribution(
        self,
        trades: list[dict],
        *,
        candidate_execution: bool,
    ) -> dict:
        """生成可审计的交易窗口行业归因；分类不可用时安全降级。"""
        from app.backtest.attribution_report import (
            build_trade_industry_brinson_report,
            fama_french_unavailable_report,
        )

        if candidate_execution:
            return {
                "status": "unavailable",
                "reason": "candidate_execution_not_portfolio_attribution",
                "scope": "候选独立执行不代表资金受约束的账户组合，不生成行业归因",
                "classification_note": "候选样本曲线按退出事件日聚合，非账户净值",
                "input_trades": len(trades),
                "classified_trades": 0,
                "capital_coverage": None,
                "warnings": ["候选独立执行不是可交易账户组合；行业归因不可用"],
                "brinson": None,
                "fama_french": fama_french_unavailable_report(),
            }

        try:
            from app.services.market_overview_builder import symbol_dimension_map

            repo = self.engine.repo
            if repo is None:
                raise RuntimeError("backtest repository unavailable")
            industry_map = symbol_dimension_map(repo, "industry", level=2)
            if not isinstance(industry_map, dict):
                raise TypeError("industry mapping must be a dict")
            return build_trade_industry_brinson_report(trades, industry_map)
        except Exception as exc:  # noqa: BLE001
            logger.warning("trade-window industry attribution unavailable: %s", exc)
            report = build_trade_industry_brinson_report(trades, {})
            report["warnings"].append("行业分类映射不可用；行业归因未计算")
            return report

    def _apply_regime_t1_mask(self, panel: pl.DataFrame, config: "StrategyBacktestConfig") -> pl.Series:
        """T-1 环境入场过滤: 缺数据 fail-open, 只 mask entry 不动 exit。

        返回与 panel 行对齐的 Boolean Series。任何异常 → 全 True(fail-open),
        绝不因 regime 数据缺失而阻断回测。
        """
        rf = config.regime_filter
        allowed = StrategyBacktestConfig._regime_filter_allowed_states(rf)
        min_score = StrategyBacktestConfig._regime_filter_min_score(rf)
        if allowed is None and min_score is None:
            return pl.Series([True] * len(panel), dtype=pl.Boolean)
        try:
            from app.services.regime_builder import regime_t1_entry_mask
            data_dir = self.engine.repo.store.data_dir
            mask = regime_t1_entry_mask(panel, data_dir, allowed_states=allowed, min_score=min_score)
            if mask is None:
                return pl.Series([True] * len(panel), dtype=pl.Boolean)
            return mask.cast(pl.Boolean).fill_null(True)
        except Exception as e:  # noqa: BLE001
            logger.warning("regime T-1 mask failed (fail-open): %s", e)
            return pl.Series([True] * len(panel), dtype=pl.Boolean)

    @staticmethod
    def _config_to_dict(c: StrategyBacktestConfig) -> dict:
        score_min, score_max = StrategyBacktestService._normalize_score_range(
            (c.overrides or {}).get("score_min"),
            (c.overrides or {}).get("score_max"),
        )
        return {
            "strategy_id": c.strategy_id,
            "symbols": c.symbols,
            "entry_fill": c.entry_fill,
            "exit_fill": c.exit_fill,
            "start": str(c.start),
            "end": str(c.end),
            "params": c.params,
            "overrides": c.overrides,
            "score_min": score_min,
            "score_max": score_max,
            "matching": c.matching,
            "mode": c.mode,
            "holding_days": c.holding_days,
            "regime_filter": c.regime_filter,
            "fees_pct": c.fees_pct,
            "stamp_tax_pct": c.stamp_tax_pct,
            "slippage_bps": c.slippage_bps,
            "max_positions": c.max_positions,
            "max_exposure_pct": c.max_exposure_pct,
            "initial_capital": c.initial_capital,
            "position_sizing": c.position_sizing,
            "risk_free_rate": c.risk_free_rate,
            "benchmark_symbol": c.benchmark_symbol,
            "benchmark_run_id": c.benchmark_run_id,
            "max_participation_pct": c.max_participation_pct,
            "participation_volume_window": c.participation_volume_window,
            "min_listed_days": c.min_listed_days,
        }

    @staticmethod
    def _apply_score(
        panel: pl.DataFrame,
        s: StrategyDef,
        overrides: dict | None,
        universe_mask: pl.Series | None = None,
    ) -> pl.DataFrame:
        scoring = s.meta.get("scoring", {})
        scoring_overrides = (overrides or {}).get("scoring")
        if scoring_overrides:
            scoring = {**scoring, **scoring_overrides}

        work = panel
        has_universe = universe_mask is not None and len(universe_mask) == len(panel)
        if has_universe:
            work = work.with_columns(universe_mask.rename("_score_universe"))

        def _value_in_universe(col: str) -> pl.Expr:
            if has_universe:
                return pl.when(pl.col("_score_universe")).then(pl.col(col)).otherwise(None)
            return pl.col(col)

        def _finish(df: pl.DataFrame) -> pl.DataFrame:
            return df.drop("_score_universe") if "_score_universe" in df.columns else df

        if scoring:
            total_weight = sum(scoring.values())
            if total_weight > 0:
                score_parts: list[pl.Expr] = []
                for col, weight in scoring.items():
                    if col not in work.columns:
                        continue
                    w = weight / total_weight
                    value = _value_in_universe(col)
                    col_min = value.min().over("date")
                    col_max = value.max().over("date")
                    col_range = col_max - col_min
                    normalized = pl.when(col_range > 0).then(
                        (pl.col(col) - col_min) / col_range
                    ).otherwise(pl.lit(0.5))
                    if has_universe:
                        normalized = pl.when(pl.col("_score_universe")).then(normalized).otherwise(0.0)
                    score_parts.append(normalized * w)
                if score_parts:
                    score_expr = score_parts[0]
                    for part in score_parts[1:]:
                        score_expr = score_expr + part
                    return _finish(work.with_columns((score_expr * 100).fill_null(0).alias("score")))

        order_by = s.meta.get("order_by")
        if order_by and order_by != "score" and order_by in work.columns:
            direction = 1 if s.meta.get("descending", True) else -1
            score_expr = pl.col(order_by).fill_null(0) * direction
            if has_universe:
                score_expr = pl.when(pl.col("_score_universe")).then(score_expr).otherwise(0.0)
            return _finish(work.with_columns(score_expr.alias("score")))
        return _finish(work.with_columns(pl.lit(0.0).alias("score")))
