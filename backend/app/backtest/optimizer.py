"""多轴策略寻优 — 训练期打分 + 独立留出确认 + DSR/PBO。

设计约束:
- 只按训练窗打分, 留出窗不得进入排序
- 默认不展开策略参数网格 (细调走 parameter_grid)
- 笛卡尔积超预算时按稳定哈希抽样, 标记 truncated
- 全市场/板块/行业池保留幸存者偏差告警
- 无 AI / 无订单语义
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal

import numpy as np

from app.backtest.parameter_grid import Objective, score_scenario
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestResult, StrategyBacktestService
from app.backtest.runtime import build_runtime
from app.json_safe import json_safe

logger = logging.getLogger(__name__)

DEFAULT_YEARS = 8
DEFAULT_TRAIN_RATIO = 0.75
DEFAULT_MAX_SCENARIOS = 120
HARD_MAX_SCENARIOS = 240
DEFAULT_TOP_K = 8
DEFAULT_MIN_TRADES = 10
DEFAULT_HOLDING_DAYS = (5, 10, 20)
DEFAULT_INDUSTRY_TOP_N = 8
PER_SYMBOL_MAX = 8
MAX_COMBO_STRATEGIES = 8
OPTIMIZER_MAX_WORKERS = 2
CSCV_BLOCKS = 8
_EULER_GAMMA = 0.5772156649015329
_SAFE_ID_RE = re.compile(r"^so-[0-9a-f]{8,16}$")

BOARD_LABELS = {
    "main": "沪深主板",
    "gem": "创业板",
    "star": "科创板",
    "bj": "北交所",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    if p <= 0.0:
        return float("-inf")
    if p >= 1.0:
        return float("inf")
    lo, hi = -12.0, 12.0
    for _ in range(64):
        mid = 0.5 * (lo + hi)
        if _norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def classify_board(symbol: str) -> str | None:
    """与横截面/市场概览一致的上市板块划分。"""
    s = (symbol or "").strip().upper()
    if not s:
        return None
    if s.endswith(".BJ"):
        return "bj"
    prefix = s[:3]
    if prefix in {"300", "301"}:
        return "gem"
    if prefix in {"688", "689"}:
        return "star"
    if s.endswith(".SH") or s.endswith(".SZ"):
        return "main"
    return None


def split_train_holdout(
    start: date,
    end: date,
    *,
    train_ratio: float = DEFAULT_TRAIN_RATIO,
) -> tuple[date, date, date, date]:
    """返回 (train_start, train_end, holdout_start, holdout_end)。两窗不重叠。"""
    if end < start:
        raise ValueError("end 不得早于 start")
    ratio = min(0.9, max(0.5, float(train_ratio)))
    span = (end - start).days
    if span < 60:
        raise ValueError("寻优窗口至少需要 60 个日历日")
    train_days = max(30, int(round(span * ratio)))
    if train_days >= span:
        train_days = span - 30
    train_end = start + timedelta(days=train_days)
    holdout_start = train_end + timedelta(days=1)
    if holdout_start > end:
        raise ValueError("留出窗为空, 请加长区间或降低训练占比")
    return start, train_end, holdout_start, end


def calendar_phases(start: date, end: date) -> list[dict[str, str]]:
    """按自然年切开的报告阶段 (不用于选参)。"""
    phases: list[dict[str, str]] = []
    year = start.year
    while year <= end.year:
        phase_start = start if year == start.year else date(year, 1, 1)
        phase_end = end if year == end.year else date(year, 12, 31)
        if phase_start <= phase_end:
            phases.append({
                "id": str(year),
                "label": f"{year}年",
                "start": phase_start.isoformat(),
                "end": phase_end.isoformat(),
            })
        year += 1
    return phases


def resolve_window(
    *,
    end: date | None,
    years: int,
    earliest: date | None,
    latest: date | None,
) -> tuple[date, date, list[str]]:
    """冻结 8 年窗, 用数据上下限夹逼。"""
    warnings: list[str] = []
    resolved_end = end or latest or date.today()
    if latest is not None and resolved_end > latest:
        resolved_end = latest
        warnings.append("end_clamped_to_latest")
    span_years = max(1, min(15, int(years)))
    resolved_start = date(resolved_end.year - span_years, resolved_end.month, resolved_end.day)
    if earliest is not None and resolved_start < earliest:
        resolved_start = earliest
        warnings.append("start_clamped_to_earliest")
    if resolved_start >= resolved_end:
        raise ValueError("数据窗口不足以覆盖寻优区间")
    return resolved_start, resolved_end, warnings


@dataclass(frozen=True)
class UniverseSpec:
    universe_id: str
    label: str
    kind: str
    symbols: tuple[str, ...] | None


def build_universes(
    *,
    symbols: list[str] | None,
    include_all_a: bool,
    boards: list[str],
    industries: list[str],
    industry_map: dict[str, list[str]] | None,
    per_symbol: bool,
    industry_top_n: int = DEFAULT_INDUSTRY_TOP_N,
) -> list[UniverseSpec]:
    """从请求轴展开股票池。industry_map 为 symbol→行业列表。"""
    out: list[UniverseSpec] = []
    custom = tuple(dict.fromkeys(s.strip().upper() for s in (symbols or []) if s and s.strip()))

    if include_all_a:
        out.append(UniverseSpec("all_a", "全A", "all_a", None))

    wanted_boards = [b for b in boards if b in BOARD_LABELS]
    if wanted_boards:
        source = custom or ()
        # 无自定义标的时, 板块池在执行期按全市场过滤; 这里只登记轴。
        for board in wanted_boards:
            members = tuple(s for s in source if classify_board(s) == board) if source else None
            out.append(UniverseSpec(
                f"board:{board}",
                BOARD_LABELS[board],
                "board",
                members if source else None,
            ))

    if industries:
        imap = industry_map or {}
        inverted: dict[str, list[str]] = {}
        for sym, names in imap.items():
            if "." not in str(sym):
                continue
            for name in names:
                if name:
                    inverted.setdefault(str(name), []).append(str(sym).upper())
        for name in industries:
            members = tuple(dict.fromkeys(inverted.get(name, [])))
            out.append(UniverseSpec(f"industry:{name}", name, "industry", members or None))
    elif industry_top_n > 0 and industry_map:
        inverted: dict[str, list[str]] = {}
        for sym, names in industry_map.items():
            if "." not in str(sym):
                continue
            for name in names:
                if name:
                    inverted.setdefault(str(name), []).append(str(sym).upper())
        ranked = sorted(inverted.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:industry_top_n]
        for name, members in ranked:
            out.append(UniverseSpec(
                f"industry:{name}",
                name,
                "industry",
                tuple(dict.fromkeys(members)),
            ))

    if custom and not per_symbol:
        digest = hashlib.md5(",".join(custom).encode()).hexdigest()[:8]
        out.append(UniverseSpec(f"symbols:{digest}", f"自定义 {len(custom)} 只", "symbols", custom))

    if per_symbol:
        if len(custom) == 0:
            raise ValueError("按个股展开需要提供 symbols")
        if len(custom) > PER_SYMBOL_MAX:
            raise ValueError(f"按个股展开最多 {PER_SYMBOL_MAX} 只, 收到 {len(custom)}")
        for sym in custom:
            out.append(UniverseSpec(f"symbol:{sym}", sym, "symbol", (sym,)))

    if not out:
        raise ValueError("至少选择一个股票池轴")
    return out


@dataclass
class SearchScenario:
    scenario_id: str
    strategy_id: str
    strategy_label: str
    universe: UniverseSpec
    holding_days: int
    matching: str
    config: StrategyBacktestConfig


@dataclass
class SearchScenarioResult:
    scenario_id: str
    strategy_id: str
    universe_id: str
    universe_label: str
    universe_kind: str
    holding_days: int
    matching: str
    train_stats: dict = field(default_factory=dict)
    holdout_stats: dict | None = None
    score: float | None = None
    rank: int = 0
    admitted: bool = False
    error: str | None = None
    elapsed_ms: float = 0.0
    train_n_obs: int = 0
    phases: list[dict] = field(default_factory=list)
    strategy_label: str = ""
    params: dict = field(default_factory=dict)


@dataclass
class SearchExperiment:
    experiment_id: str
    config_hash: str
    objective: str
    start: str
    end: str
    train_end: str
    holdout_start: str
    requested_count: int
    scenario_count: int
    max_scenarios: int
    truncated: bool
    status: str = "pending"
    scenarios: list[SearchScenarioResult] = field(default_factory=list)
    recommended_ids: list[str] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    ensemble: dict | None = None
    warnings: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    completed: int = 0
    total: int = 0
    runtime: dict = field(default_factory=dict)
    param_grid: dict | None = None


    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "scenarios": [asdict(s) for s in self.scenarios],
        }

    @staticmethod
    def from_dict(d: dict) -> SearchExperiment:
        payload = dict(d)
        known = {f.name for f in fields(SearchExperiment)}
        scenario_fields = {f.name for f in fields(SearchScenarioResult)}
        scenarios = [
            SearchScenarioResult(**{k: v for k, v in item.items() if k in scenario_fields})
            for item in payload.pop("scenarios", [])
        ]
        payload = {k: v for k, v in payload.items() if k in known}
        return SearchExperiment(**payload, scenarios=scenarios)



def _search_current(sc: SearchScenario) -> str:
    return f"{sc.strategy_label or sc.strategy_id} · {sc.universe.label} · {sc.holding_days}日 · {sc.matching}"


def _set_search_runtime(
    experiment: SearchExperiment,
    *,
    stage: str,
    label: str,
    current: str = "",
    completed: int | None = None,
    total: int | None = None,
    failed: int = 0,
    ok: int = 0,
    last_elapsed_ms: float = 0.0,
) -> None:
    experiment.runtime = build_runtime(
        stage=stage,
        label=label,
        current=current,
        completed=experiment.completed if completed is None else completed,
        total=experiment.total if total is None else total,
        failed=failed,
        ok=ok,
        started_at=experiment.created_at,
        last_elapsed_ms=last_elapsed_ms,
    )
    experiment.updated_at = experiment.runtime["updated_at"]


def _scenario_id(strategy_id: str, universe_id: str, holding_days: int, matching: str, params: dict | None = None) -> str:
    raw = f"{strategy_id}|{universe_id}|{holding_days}|{matching}"
    if params:
        raw += "|" + json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return "sc-" + hashlib.md5(raw.encode()).hexdigest()[:12]



def leaf_strategy_ids(strategy_engine, strategy_ids: list[str]) -> list[str]:
    """剔除叠加/临时策略, 只保留可两两配对的叶子。"""
    leaves: list[str] = []
    for sid in strategy_ids:
        try:
            spec = strategy_engine.get(sid)
        except (ValueError, AttributeError):
            continue
        backend = getattr(spec, "execution_backend", "polars_expr")
        source = getattr(spec, "source", "")
        if backend == "composite" or getattr(spec, "ephemeral", False) or source == "ai":
            continue
        leaves.append(sid)
    return list(dict.fromkeys(leaves))


def expand_combo_specs(
    strategy_ids: list[str],
    *,
    merge_mode: str = "union",
) -> list[tuple[str, tuple[str, ...], str, str]]:
    """叶子策略两两并集。返回 (combo_id, children, merge_mode, label)。"""
    leaves = list(dict.fromkeys(strategy_ids))
    if len(leaves) < 2:
        return []
    pairs = list(itertools.combinations(sorted(leaves), 2))
    if len(pairs) > MAX_COMBO_STRATEGIES:
        pairs = sorted(
            pairs,
            key=lambda pair: hashlib.md5("|".join(pair).encode()).hexdigest(),
        )[:MAX_COMBO_STRATEGIES]
    out: list[tuple[str, tuple[str, ...], str, str]] = []
    for left, right in pairs:
        combo_id = "combo:" + hashlib.md5(f"{merge_mode}|{left}|{right}".encode()).hexdigest()[:12]
        label = f"{left} ∪ {right}"
        out.append((combo_id, (left, right), merge_mode, label))
    return out


def make_combo_strategy(combo_id: str, children: tuple[str, ...], *, merge_mode: str, label: str):
    from app.strategy.engine import CompositeChild, CompositeSpec, DEFAULT_BASIC_FILTER, StrategyDef

    child_spec = CompositeSpec(children=tuple(CompositeChild(strategy_id=cid, weight=1.0) for cid in children))
    return StrategyDef(
        meta={
            "id": combo_id,
            "name": label,
            "description": "寻优临时两两叠加, 不写入策略池",
            "tags": ["optimizer", "combo"],
            "params": [
                {"id": "merge_mode", "type": "enum", "default": merge_mode},
                {"id": "min_confirm", "type": "int", "default": 0},
            ],
            "children": [{"strategy_id": cid, "weight": 1.0} for cid in children],
            "asset_types": ["stock"],
        },
        basic_filter={**DEFAULT_BASIC_FILTER},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        alerts=[],
        filter_fn=None,
        filter_history_fn=None,
        lookback_days=1,
        source="composite",
        file_path=None,
        execution_backend="composite",
        composite=child_spec,
        ephemeral=True,
    )


def install_combo_strategies(strategy_engine, specs: list[tuple[str, tuple[str, ...], str, str]]) -> list[str]:
    """把临时组合写入引擎; 无 put_ephemeral 时跳过。"""
    put = getattr(strategy_engine, "put_ephemeral", None)
    installed: list[str] = []
    if put is None:
        return installed
    for combo_id, children, merge_mode, label in specs:
        put(combo_id, make_combo_strategy(combo_id, children, merge_mode=merge_mode, label=label))
        installed.append(combo_id)
    return installed
def _get_param_defs(strategy_engine: Any, strategy_id: str) -> list[dict]:
    """从策略定义中取 params 列表（[{id, type, default, ...}]）。失败返回空。"""
    try:
        spec = strategy_engine.get(strategy_id)
    except Exception:  # noqa: BLE001
        return []
    meta = getattr(spec, "meta", {}) or {}
    return list(meta.get("params", []) or [])


def _validate_and_expand_param_grid(
    strategy_engine: Any,
    raw_grid: dict[str, dict[str, list[Any]]] | None,
    *,
    strategy_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """校验 param_grid 并返回 {sid: [ {param: value, ...}, ... ]}。

    规则:
    - 每策略最多 2 个参数
    - 每参数最多 5 个值
    - 单策略参数笛卡尔积 ≤8
    - 参数名必须存在于该策略的 meta.params 定义
    - 值必须可按定义类型解析（int/float 支持字符串解析；bool 只接受 true/false 字面）
    未提供网格的策略返回 [{}]（单实例）。
    非法直接抛 ValueError（中文原因，供上层转 422）。
    """
    if not raw_grid:
        return {sid: [{}] for sid in strategy_ids}

    out: dict[str, list[dict[str, Any]]] = {}
    for sid in strategy_ids:
        grid = (raw_grid or {}).get(sid) or {}
        defs_list = _get_param_defs(strategy_engine, sid)
        defs = {d.get("id"): d for d in defs_list if d.get("id")}
        if not grid:
            out[sid] = [{}]
            continue
        if len(grid) > 2:
            raise ValueError(f"策略 {sid} 参数个数超过上限 2")
        param_lists: list[list[tuple[str, Any]]] = []
        for pname, values in grid.items():
            if pname not in defs:
                raise ValueError(f"策略 {sid} 不存在参数 {pname}")
            pdef = defs[pname]
            ptype: str = str(pdef.get("type", "float"))
            if len(values) > 5:
                raise ValueError(f"策略 {sid} 参数 {pname} 取值个数超过上限 5")
            coerced: list[Any] = []
            for v in values:
                try:
                    if ptype == "int":
                        if isinstance(v, bool):
                            coerced.append(int(v))
                        else:
                            coerced.append(int(str(v).strip()))
                    elif ptype == "float":
                        if isinstance(v, bool):
                            coerced.append(float(v))
                        else:
                            coerced.append(float(str(v).strip()))
                    elif ptype == "bool":
                        if isinstance(v, bool):
                            coerced.append(bool(v))
                        elif isinstance(v, str):
                            lv = v.strip().lower()
                            if lv == "true":
                                coerced.append(True)
                            elif lv == "false":
                                coerced.append(False)
                            else:
                                raise ValueError("bool")
                        elif v in (0, 1):
                            coerced.append(bool(v))
                        else:
                            raise ValueError("bool")
                    else:
                        # select 或其他：保留原值（字符串化由调用方处理）
                        coerced.append(v)
                except Exception:
                    raise ValueError(f"策略 {sid} 参数 {pname} 值 {v!r} 无法转为定义类型 {ptype}")
            # 去重保序
            seen: set[tuple] = set()
            uniq: list[Any] = []
            for c in coerced:
                key = (type(c).__name__, c)
                if key not in seen:
                    seen.add(key)
                    uniq.append(c)
            param_lists.append([(pname, c) for c in uniq])
        # 笛卡尔
        combos: list[dict[str, Any]] = []
        for prod in itertools.product(*param_lists):
            combos.append({k: v for k, v in prod})
        if len(combos) > 8:
            raise ValueError(f"策略 {sid} 参数组合数超过上限 8")
        out[sid] = combos or [{}]
    # 其余策略补默认单实例
    for sid in strategy_ids:
        if sid not in out:
            out[sid] = [{}]
    return out


def expand_search_scenarios(
    *,
    strategy_ids: list[str],
    universes: list[UniverseSpec],
    holding_days: list[int],
    matchings: list[str],
    base: StrategyBacktestConfig,
    max_scenarios: int = DEFAULT_MAX_SCENARIOS,
    seed: int = 0,
    strategy_labels: dict[str, str] | None = None,
    param_grid_expanded: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[list[SearchScenario], int, bool]:
    """笛卡尔展开；超预算时按稳定哈希抽样。

    新增 param_grid_expanded: {strategy_id: [ {param:val, ...}, ... ] }
    每个策略的实例数 = len(其 param 组合)，无网格则为 1。
    场景 id 包含 params 指纹；config.params 回填具体取值。
    """
    if not strategy_ids:
        raise ValueError("至少选择一个策略")
    holdings = sorted({int(v) for v in holding_days if int(v) >= 1})
    if not holdings:
        raise ValueError("至少选择一个持仓周期")
    matches = [m for m in matchings if m in {"close_t", "open_t+1"}]
    if not matches:
        raise ValueError("成交口径必须是 close_t 或 open_t+1")
    cap = min(HARD_MAX_SCENARIOS, max(1, int(max_scenarios)))
    labels = strategy_labels or {}
    pg = param_grid_expanded or {sid: [{}] for sid in strategy_ids}

    # 构建带参数维的笛卡尔积
    combos: list[tuple] = []
    for sid in strategy_ids:
        pcombos = pg.get(sid) or [{}]
        for pcombo in pcombos:
            combos.extend(itertools.product([sid], [pcombo], universes, holdings, matches))

    requested = len(combos)
    if requested > cap:
        ranked = sorted(
            combos,
            key=lambda item: hashlib.md5(
                f"{seed}|{item[0]}|{json.dumps(item[1], sort_keys=True, ensure_ascii=False, default=str)}|{item[2].universe_id}|{item[3]}|{item[4]}".encode()
            ).hexdigest(),
        )
        combos = ranked[:cap]

    scenarios: list[SearchScenario] = []
    for strategy_id, pcombo, universe, holding, matching in combos:
        sid = _scenario_id(strategy_id, universe.universe_id, holding, matching, pcombo or None)
        cfg_params = dict(base.params or {})
        if pcombo:
            cfg_params.update(pcombo)
        cfg = StrategyBacktestConfig(
            strategy_id=strategy_id,
            symbols=list(universe.symbols) if universe.symbols is not None else None,
            start=base.start,
            end=base.end,
            params=cfg_params or None,
            overrides=base.overrides,
            matching=matching,  # type: ignore[arg-type]
            fees_pct=base.fees_pct,
            slippage_bps=base.slippage_bps,
            max_positions=base.max_positions,
            max_exposure_pct=base.max_exposure_pct,
            initial_capital=base.initial_capital,
            position_sizing=base.position_sizing,
            mode="position",
            holding_days=holding,
            regime_filter=base.regime_filter,
            risk_free_rate=base.risk_free_rate,
        )
        scenarios.append(SearchScenario(
            scenario_id=sid,
            strategy_id=strategy_id,
            strategy_label=labels.get(strategy_id, strategy_id),
            universe=universe,
            holding_days=holding,
            matching=matching,
            config=cfg,
        ))
    return scenarios, requested, requested > cap



def compute_config_hash(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.md5(encoded.encode()).hexdigest()[:16]


def equity_to_returns(curve: list[dict] | None) -> np.ndarray:
    values: list[float] = []
    for point in curve or []:
        if not isinstance(point, dict):
            continue
        try:
            value = float(point.get("value"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            values.append(value)
    if len(values) < 2:
        return np.asarray([], dtype=float)
    arr = np.asarray(values, dtype=float)
    return arr[1:] / arr[:-1] - 1.0


def expected_max_sharpe(n_trials: int) -> float:
    """False Strategy Theorem: N 次零技能试验的期望最大夏普 (标准正态)。"""
    n = max(1, int(n_trials))
    if n == 1:
        return 0.0
    inv_n = 1.0 / n
    return (1.0 - _EULER_GAMMA) * _norm_ppf(1.0 - inv_n) + _EULER_GAMMA * _norm_ppf(1.0 - inv_n / math.e)


def deflated_sharpe_ratio(
    sharpe: float,
    *,
    n_trials: int,
    n_obs: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float | None:
    """Bailey & López de Prado DSR: 观测夏普在多重检验后仍为正的概率。

    ``sharpe`` 必须与 ``n_obs`` 同频 (此处用日频非年化夏普)。
    """
    if n_obs < 3 or n_trials < 1 or not math.isfinite(sharpe):
        return None
    sr_star = expected_max_sharpe(n_trials) / math.sqrt(max(n_obs, 1))
    denom_var = 1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe * sharpe
    if denom_var <= 1e-12:
        return None
    z = (sharpe - sr_star) * math.sqrt(n_obs - 1) / math.sqrt(denom_var)
    if not math.isfinite(z):
        return None
    return round(float(_norm_cdf(z)), 6)


def cscv_pbo(return_series: list[np.ndarray], *, n_blocks: int = CSCV_BLOCKS) -> dict[str, Any]:
    """Combinatorially Symmetric Cross-Validation 估计过拟合概率。"""
    cleaned = [np.asarray(s, dtype=float) for s in return_series if s is not None and len(s) >= n_blocks * 2]
    if len(cleaned) < 2:
        return {"pbo": None, "n_combinations": 0, "n_trials": len(cleaned), "n_blocks": 0, "reason": "insufficient_trials"}
    min_len = min(len(s) for s in cleaned)
    blocks = n_blocks
    while blocks >= 4 and min_len // blocks < 2:
        blocks -= 2
    if blocks < 4:
        return {"pbo": None, "n_combinations": 0, "n_trials": len(cleaned), "n_blocks": blocks, "reason": "series_too_short"}
    block_len = min_len // blocks
    usable = blocks * block_len
    stacked = np.stack([s[:usable].reshape(blocks, block_len).mean(axis=1) for s in cleaned])
    half = blocks // 2
    overfit = 0
    total = 0
    for combo in itertools.combinations(range(blocks), half):
        is_idx = np.asarray(combo, dtype=int)
        oos_idx = np.asarray([i for i in range(blocks) if i not in combo], dtype=int)
        is_score = stacked[:, is_idx].mean(axis=1)
        oos_score = stacked[:, oos_idx].mean(axis=1)
        best = int(np.argmax(is_score))
        oos_rank = int(np.sum(oos_score >= oos_score[best]))
        # 留出期排名落入后一半 → 计为一次过拟合
        if oos_rank > (len(cleaned) + 1) / 2:
            overfit += 1
        total += 1
    return {
        "pbo": round(overfit / total, 6) if total else None,
        "n_combinations": total,
        "n_trials": len(cleaned),
        "n_blocks": blocks,
        "reason": None,
    }


def _finite_stat(stats: dict | None, key: str) -> float | None:
    if not stats:
        return None
    try:
        value = float(stats.get(key))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def eligible_for_holdout(
    stats: dict | None,
    *,
    min_trades: int,
    max_drawdown: float | None,
) -> bool:
    """训练窗是否有足够样本进入留出，不看训练收益正负。"""
    if not stats:
        return False
    trades = _finite_stat(stats, "n_trades")
    if trades is None or trades < min_trades:
        return False
    pending = _finite_stat(stats, "pending_exit_positions")
    if pending is not None and pending > 0:
        return False
    if max_drawdown is not None:
        dd = _finite_stat(stats, "max_drawdown")
        if dd is None or abs(dd) > abs(max_drawdown):
            return False
    return True


def passes_constraints(
    stats: dict | None,
    *,
    min_trades: int,
    max_drawdown: float | None,
) -> bool:
    if not eligible_for_holdout(stats, min_trades=min_trades, max_drawdown=max_drawdown):
        return False
    ret = _finite_stat(stats, "total_return")
    return ret is not None and ret > 0


def phase_returns(curve: list[dict] | None, phases: list[dict[str, str]]) -> list[dict]:
    """按阶段首末净值估算收益; 缺数据的阶段返回 null, 不伪造。"""
    by_date: dict[str, float] = {}
    for point in curve or []:
        if not isinstance(point, dict) or point.get("date") is None:
            continue
        try:
            value = float(point.get("value"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            by_date[str(point["date"])[:10]] = value
    if not by_date:
        return [{"id": p["id"], "label": p["label"], "total_return": None} for p in phases]
    dates = sorted(by_date)
    out: list[dict] = []
    for phase in phases:
        start, end = phase["start"], phase["end"]
        window = [d for d in dates if start <= d <= end]
        if len(window) < 2:
            out.append({"id": phase["id"], "label": phase["label"], "total_return": None})
            continue
        first, last = by_date[window[0]], by_date[window[-1]]
        out.append({
            "id": phase["id"],
            "label": phase["label"],
            "total_return": round(last / first - 1.0, 6),
        })
    return out


def combine_equal_weight(curves: list[list[dict]]) -> list[dict]:
    """多条净值按日期内连接后等权平均 (各曲线先自首点归一)。"""
    series: list[dict[str, float]] = []
    for curve in curves:
        rets_points: dict[str, float] = {}
        first = None
        for point in curve:
            if not isinstance(point, dict) or point.get("date") is None:
                continue
            try:
                value = float(point.get("value"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value) or value <= 0:
                continue
            if first is None:
                first = value
            rets_points[str(point["date"])[:10]] = value / first
        if rets_points:
            series.append(rets_points)
    if not series:
        return []
    dates = sorted(set().union(*[s.keys() for s in series]))
    out: list[dict] = []
    for d in dates:
        vals = [s[d] for s in series if d in s]
        if vals:
            out.append({"date": d, "value": round(float(np.mean(vals)), 6)})
    return out


def _daily_sharpe(returns: np.ndarray) -> float | None:
    if returns.size < 3:
        return None
    std = float(returns.std(ddof=1))
    if std <= 1e-12:
        return None
    return float(returns.mean() / std)


class OptimizerExperimentStore:
    def __init__(self, data_dir: Path | str) -> None:
        self.dir = Path(data_dir) / "research" / "optimizer_experiments"

    def _path(self, experiment_id: str) -> Path:
        if not _SAFE_ID_RE.match(experiment_id):
            raise ValueError(f"invalid experiment_id: {experiment_id!r}")
        return self.dir / f"{experiment_id}.json"

    def save(self, experiment: SearchExperiment) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(experiment.experiment_id)
        payload = json.dumps(
            json_safe(experiment.to_dict()),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
            default=str,
        )
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)

    def load(self, experiment_id: str) -> SearchExperiment:
        path = self._path(experiment_id)
        if not path.exists():
            raise KeyError(experiment_id)
        return SearchExperiment.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def exists(self, experiment_id: str) -> bool:
        try:
            return self._path(experiment_id).exists()
        except ValueError:
            return False


def _run_one(
    service: StrategyBacktestService,
    config: StrategyBacktestConfig,
    cancel_event: threading.Event | None,
) -> StrategyBacktestResult:
    return service.run(config, cancel_event=cancel_event)


def run_search(
    service: StrategyBacktestService,
    store: OptimizerExperimentStore,
    *,
    experiment_id: str,
    config_hash: str,
    objective: Objective,
    scenarios: list[SearchScenario],
    requested_count: int,
    truncated: bool,
    train_start: date,
    train_end: date,
    holdout_start: date,
    holdout_end: date,
    min_trades: int = DEFAULT_MIN_TRADES,
    max_drawdown: float | None = None,
    top_k: int = DEFAULT_TOP_K,
    extra_warnings: list[str] | None = None,
    max_workers: int = OPTIMIZER_MAX_WORKERS,
    progress_cb: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
    param_grid: dict | None = None,
) -> SearchExperiment:
    """训练期全量评估 → DSR/PBO → 留出期只重跑 Top-K。"""
    warnings = list(extra_warnings or [])
    warnings.append("survivorship_bias")
    warnings.append("search_is_not_global_optimum")
    experiment = SearchExperiment(
        experiment_id=experiment_id,
        config_hash=config_hash,
        objective=objective,
        start=train_start.isoformat(),
        end=holdout_end.isoformat(),
        train_end=train_end.isoformat(),
        holdout_start=holdout_start.isoformat(),
        requested_count=requested_count,
        scenario_count=len(scenarios),
        max_scenarios=len(scenarios),
        truncated=truncated,
        status="running",
        warnings=warnings,
        created_at=_now(),
        updated_at=_now(),
        completed=0,
        total=len(scenarios),
    )
    if param_grid is not None:
        experiment.param_grid = param_grid
    _set_search_runtime(experiment, stage="train", label="训练评估", current="正在排队训练场景")
    store.save(experiment)


    train_curves: dict[str, list[dict]] = {}
    train_returns: dict[str, np.ndarray] = {}
    results: dict[str, SearchScenarioResult] = {}

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    workers = max(1, min(int(max_workers), 4))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {}
        for sc in scenarios:
            if _cancelled():
                break
            cfg = sc.config
            cfg.start = train_start
            cfg.end = train_end
            future_map[pool.submit(_run_one, service, cfg, cancel_event)] = sc
        completed = 0
        for fut in as_completed(future_map):
            sc = future_map[fut]
            try:
                raw = fut.result()
            except Exception as exc:  # noqa: BLE001
                raw = StrategyBacktestResult(run_id="err", config={}, error=str(exc))
            result = SearchScenarioResult(
                scenario_id=sc.scenario_id,
                strategy_id=sc.strategy_id,
                strategy_label=sc.strategy_label or sc.strategy_id,
                universe_id=sc.universe.universe_id,
                universe_label=sc.universe.label,
                universe_kind=sc.universe.kind,
                holding_days=sc.holding_days,
                matching=sc.matching,
                train_stats=dict(raw.stats or {}),
                error=raw.error,
                elapsed_ms=raw.elapsed_ms,
                params=dict(sc.config.params or {}),
            )
            if not raw.error:
                result.score = score_scenario(result.train_stats, objective)
                rets = equity_to_returns(raw.equity_curve)
                train_returns[sc.scenario_id] = rets
                train_curves[sc.scenario_id] = list(raw.equity_curve or [])
                result.train_n_obs = int(rets.size)
            results[sc.scenario_id] = result
            completed += 1
            experiment.completed = completed
            experiment.scenarios = list(results.values())
            failed = sum(1 for item in results.values() if item.error)
            _set_search_runtime(
                experiment,
                stage="train",
                label="训练评估",
                current=_search_current(sc),
                failed=failed,
                ok=completed - failed,
                last_elapsed_ms=raw.elapsed_ms,
            )
            if progress_cb is not None:
                progress_cb({**experiment.runtime, "scenario_id": sc.scenario_id})
            store.save(experiment)

    if _cancelled():
        experiment.status = "cancelled"
        experiment.scenarios = list(results.values())
        _set_search_runtime(
            experiment,
            stage="cancelled",
            label="已取消",
            current=_search_current(sc) if results else "",
            failed=sum(1 for item in results.values() if item.error),
            ok=sum(1 for item in results.values() if not item.error),
        )
        store.save(experiment)
        return experiment

    ranked = sorted(
        results.values(),
        key=lambda r: (
            r.score is None,
            -(r.score or 0.0),
            r.scenario_id,
        ),
    )
    for idx, item in enumerate(ranked, start=1):
        item.rank = idx

    best = next((r for r in ranked if r.score is not None), None)
    best_returns = train_returns.get(best.scenario_id) if best else None
    daily_sr = _daily_sharpe(best_returns) if best_returns is not None else None
    skew = float(best_returns.mean() and ((best_returns - best_returns.mean()) ** 3).mean() / (best_returns.std(ddof=1) ** 3)) if best_returns is not None and best_returns.size >= 4 and best_returns.std(ddof=1) > 1e-12 else 0.0
    kurt = float(((best_returns - best_returns.mean()) ** 4).mean() / (best_returns.std(ddof=1) ** 4)) if best_returns is not None and best_returns.size >= 4 and best_returns.std(ddof=1) > 1e-12 else 3.0
    dsr = deflated_sharpe_ratio(
        daily_sr if daily_sr is not None else float("nan"),
        n_trials=max(1, len(train_returns)),
        n_obs=int(best_returns.size) if best_returns is not None else 0,
        skew=skew,
        kurtosis=kurt,
    ) if daily_sr is not None else None
    pbo = cscv_pbo(list(train_returns.values()))
    if len(train_returns) > 70:
        warnings.append("high_trial_count")

    experiment.diagnostics = {
        "dsr": dsr,
        "best_daily_sharpe": None if daily_sr is None else round(daily_sr, 6),
        "n_trials": len(train_returns),
        "expected_max_sharpe": round(expected_max_sharpe(max(1, len(train_returns))), 6),
        "pbo": pbo,
    }

    candidates = [
        r for r in ranked
        if r.error is None and eligible_for_holdout(r.train_stats, min_trades=min_trades, max_drawdown=max_drawdown)
    ][: max(1, int(top_k))]

    holdout_curves: dict[str, list[dict]] = {}
    by_id = {sc.scenario_id: sc for sc in scenarios}
    _set_search_runtime(
        experiment,
        stage="holdout",
        label="留出确认",
        current=f"待确认 {len(candidates)} 个训练候选",
        completed=0,
        total=max(1, len(candidates)),
        failed=sum(1 for item in ranked if item.error),
        ok=sum(1 for item in ranked if not item.error),
    )
    store.save(experiment)
    holdout_done = 0
    for item in candidates:
        if _cancelled():
            break
        sc = by_id[item.scenario_id]
        cfg = sc.config
        cfg.start = holdout_start
        cfg.end = holdout_end
        try:
            raw = _run_one(service, cfg, cancel_event)
        except Exception as exc:  # noqa: BLE001
            item.error = str(exc)
            holdout_done += 1
            _set_search_runtime(
                experiment,
                stage="holdout",
                label="留出确认",
                current=_search_current(sc),
                completed=holdout_done,
                total=len(candidates),
                last_elapsed_ms=0.0,
            )
            store.save(experiment)
            continue
        item.holdout_stats = dict(raw.stats or {})
        item.elapsed_ms += raw.elapsed_ms
        if raw.error:
            item.error = raw.error
        else:
            holdout_curves[item.scenario_id] = list(raw.equity_curve or [])
            item.admitted = passes_constraints(
                item.holdout_stats,
                min_trades=min_trades,
                max_drawdown=max_drawdown,
            )
        holdout_done += 1
        _set_search_runtime(
            experiment,
            stage="holdout",
            label="留出确认",
            current=_search_current(sc),
            completed=holdout_done,
            total=len(candidates),
            last_elapsed_ms=raw.elapsed_ms,
        )
        if progress_cb is not None:
            progress_cb({**experiment.runtime, "scenario_id": item.scenario_id})
        store.save(experiment)

    phases = calendar_phases(train_start, holdout_end)
    for item in ranked:
        train_curve = train_curves.get(item.scenario_id, [])
        holdout_curve = holdout_curves.get(item.scenario_id, [])
        item.phases = phase_returns(train_curve + holdout_curve, phases)

    admitted = [r for r in ranked if r.admitted]
    experiment.recommended_ids = [r.scenario_id for r in admitted]
    if admitted:
        combo_curve = combine_equal_weight([holdout_curves[r.scenario_id] for r in admitted if r.scenario_id in holdout_curves])
        combo_rets = equity_to_returns(combo_curve)
        combo_sr = _daily_sharpe(combo_rets)
        experiment.ensemble = {
            "kind": "equal_weight_holdout",
            "members": [r.scenario_id for r in admitted],
            "n_obs": int(combo_rets.size),
            "daily_sharpe": None if combo_sr is None else round(combo_sr, 6),
            "total_return": None if len(combo_curve) < 2 else round(combo_curve[-1]["value"] / combo_curve[0]["value"] - 1.0, 6),
            "note": "留出期已过门禁候选的归一净值等权, 不是资金约束账户",
        }
    else:
        experiment.ensemble = None
        warnings.append("no_holdout_admitted")

    experiment.warnings = list(dict.fromkeys(warnings))
    experiment.scenarios = ranked
    experiment.status = "completed"
    experiment.completed = len(scenarios)
    _set_search_runtime(
        experiment,
        stage="completed",
        label="已完成",
        current=f"留出通过 {len(admitted)} / {len(candidates)}",
        failed=sum(1 for item in ranked if item.error),
        ok=sum(1 for item in ranked if not item.error),
    )
    experiment.updated_at = _now()
    store.save(experiment)
    return experiment
