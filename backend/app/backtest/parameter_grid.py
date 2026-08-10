"""受限参数网格寻优 — 笛卡尔展开 + 有界并发 + 稳健性配对。

设计约束 (与 AI / 订单域完全隔离):
- 只接受 StrategyDef 数值 params (type in int/float, 有 min/max) 作为网格轴
- 组合上限: 默认 24, 硬上限 36; 超出截断并标记 truncated
- 实验级有界并发 (ThreadPoolExecutor), 共享 panel 避免每 scenario 重载
- 最优 scenario 自动复用 robustness.py 后处理 (bootstrap / MC / exit breakdown)
- 无 AI / 无订单语义 / 无交易事件

公共 API:
  normalize_grid      — 校验 + 排序 + 去重 + 截断, 返回 NormalizedGrid
  expand_scenarios    — 笛卡尔积展开为 list[StrategyBacktestConfig]
  compute_config_hash — 基础配置 + 网格 + objective 的确定性 md5
  score_scenario      — objective 打分 (sharpe / calmar / total_return / risk_adjusted)
  run_grid            — 有界并发执行 + 评分排序 + 最优稳健性 + 原子持久化
  ParameterGridExperimentStore — 原子写 / 启动恢复
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
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal

from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestResult, StrategyBacktestService

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────

DEFAULT_MAX_SCENARIOS = 24
HARD_MAX_SCENARIOS = 36
GRID_MAX_WORKERS = 4

Objective = Literal["sharpe", "calmar", "total_return", "risk_adjusted"]

_SAFE_ID_RE = re.compile(r"^pg-[0-9a-f]{8,16}$")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ================================================================
# 数据模型
# ================================================================

@dataclass(frozen=True)
class NormalizedGrid:
    """校验后的参数网格 (不可变)。

    - grid: 排序 key → 去重排序值; int 参数值为 int
    - int_keys: 哪些 key 是 int 类型 (展开时保持整数)
    - requested_count: 截断前组合数
    - scenario_count: 截断后实际组合数
    - truncated: 是否截断
    """
    grid: dict[str, list]
    int_keys: frozenset[str]
    objective: str
    requested_count: int
    scenario_count: int
    max_scenarios: int
    truncated: bool


@dataclass
class GridScenarioResult:
    """单个 scenario 的结果。"""
    scenario_id: str
    params: dict
    stats: dict = field(default_factory=dict)
    score: float | None = None
    rank: int = 0
    error: str | None = None
    elapsed_ms: float = 0.0


@dataclass
class GridExperiment:
    """完整实验记录 (持久化单元)。"""
    experiment_id: str
    config_hash: str
    strategy_id: str
    objective: str
    base_config: dict
    grid: dict[str, list]
    requested_count: int
    scenario_count: int
    max_scenarios: int
    truncated: bool
    status: str = "pending"  # pending | running | completed | cancelled | failed
    scenarios: list[GridScenarioResult] = field(default_factory=list)
    best_scenario_id: str | None = None
    robustness: dict | None = None
    created_at: str = ""
    updated_at: str = ""
    completed: int = 0
    total: int = 0

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "scenarios": [asdict(s) for s in self.scenarios],
        }

    @staticmethod
    def from_dict(d: dict) -> GridExperiment:
        scenarios = [GridScenarioResult(**s) for s in d.pop("scenarios", [])]
        return GridExperiment(**d, scenarios=scenarios)


# ================================================================
# 纯逻辑: 校验 / 展开 / 哈希 / 打分
# ================================================================

def _is_real_number(v) -> bool:
    """允许 int/float 但拒绝 bool / NaN / Inf。"""
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    f = float(v)
    return math.isfinite(f)


def normalize_grid(
    grid: dict[str, list],
    strategy_params: list[dict],
    objective: str = "risk_adjusted",
    max_scenarios: int = DEFAULT_MAX_SCENARIOS,
) -> NormalizedGrid:
    """校验参数网格: 白名单 / 范围 / 类型 / NaN/Inf / 去重排序 / 截断。

    Raises:
        ValueError: 未知参数 / 非数值参数 / 越界值 / int 参数非整数值 / NaN/Inf
    """
    # 数值参数白名单: type in (int, float) 且有 min 和 max
    numeric_params: dict[str, dict] = {}
    for p in strategy_params:
        pid = p.get("id")
        if not pid:
            continue
        if p.get("type") in ("int", "float") and p.get("min") is not None and p.get("max") is not None:
            numeric_params[pid] = p

    normalized: dict[str, list] = {}
    int_keys: set[str] = set()

    for raw_key, raw_values in grid.items():
        if raw_key not in numeric_params:
            raise ValueError(
                f"参数 '{raw_key}' 不在策略数值参数白名单中"
                f" (允许: {', '.join(sorted(numeric_params)) or '无'})"
            )
        param = numeric_params[raw_key]
        pmin = float(param["min"])
        pmax = float(param["max"])
        is_int = param.get("type") == "int"

        cleaned: list = []
        for v in raw_values:
            if not _is_real_number(v):
                raise ValueError(f"参数 '{raw_key}' 含非法值 (非有限实数): {v!r}")
            fv = float(v)
            if fv < pmin or fv > pmax:
                raise ValueError(f"参数 '{raw_key}' 值 {fv} 超出范围 [{pmin}, {pmax}]")
            if is_int:
                iv = int(round(fv))
                if abs(fv - iv) > 1e-9:
                    raise ValueError(f"参数 '{raw_key}' 为 int 类型, 值 {fv} 不是整数")
                cleaned.append(iv)
            else:
                cleaned.append(round(fv, 8))

        # 去重 + 排序
        seen: set = set()
        deduped = []
        for v in sorted(cleaned):
            key = float(v)
            if key not in seen:
                seen.add(key)
                deduped.append(v)
        if deduped:
            normalized[raw_key] = deduped
            if is_int:
                int_keys.add(raw_key)

    # 排序 key
    sorted_grid = {k: normalized[k] for k in sorted(normalized)}

    # 组合计数 + 截断
    effective_limit = max(1, min(int(max_scenarios), HARD_MAX_SCENARIOS))
    requested = 1
    for vals in sorted_grid.values():
        requested *= len(vals)
    truncated = requested > effective_limit
    scenario_count = min(requested, effective_limit)

    return NormalizedGrid(
        grid=sorted_grid,
        int_keys=frozenset(int_keys),
        objective=objective,
        requested_count=requested,
        scenario_count=scenario_count,
        max_scenarios=effective_limit,
        truncated=truncated,
    )


def expand_scenarios(
    base: StrategyBacktestConfig,
    ng: NormalizedGrid,
) -> list[StrategyBacktestConfig]:
    """笛卡尔积展开为 list[StrategyBacktestConfig] (按 param id 排序, 截断至 scenario_count)。

    空 grid → [base] (单 scenario)。
    """
    if not ng.grid:
        return [replace(base)]

    keys = sorted(ng.grid.keys())
    value_lists = [ng.grid[k] for k in keys]

    scenarios: list[StrategyBacktestConfig] = []
    for combo in itertools.islice(itertools.product(*value_lists), ng.scenario_count):
        params = dict(base.params or {})
        for k, v in zip(keys, combo):
            params[k] = v
        scenarios.append(replace(base, params=params))
    return scenarios


def compute_config_hash(
    base_config: StrategyBacktestConfig,
    grid: dict[str, list],
    objective: str,
) -> str:
    """基础配置 + 网格 + objective 的确定性 md5 (16 hex)。"""
    base_dict = StrategyBacktestService._config_to_dict(base_config)
    payload = json.dumps(
        {
            "base": base_dict,
            "grid": {k: grid[k] for k in sorted(grid)},
            "objective": objective,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.md5(payload.encode()).hexdigest()[:16]


def score_scenario(stats: dict, objective: Objective) -> float:
    """对 scenario stats 打分。确定性, 无随机性。

    - sharpe: stats["sharpe"]
    - total_return: stats["total_return"]
    - calmar: total_return / |max_drawdown|
    - risk_adjusted: sharpe + calmar
    """
    sharpe = float(stats.get("sharpe") or 0.0)
    total_return = float(stats.get("total_return") or 0.0)
    max_dd = float(stats.get("max_drawdown") or 0.0)
    abs_dd = abs(max_dd)

    calmar: float
    if abs_dd > 1e-9:
        calmar = total_return / abs_dd
    else:
        calmar = total_return * 100.0 if total_return > 0 else 0.0

    if objective == "sharpe":
        return round(sharpe, 4)
    if objective == "total_return":
        return round(total_return, 4)
    if objective == "calmar":
        return round(calmar, 4)
    # risk_adjusted (默认)
    return round(sharpe + calmar, 4)


# ================================================================
# 持久化
# ================================================================

class ParameterGridExperimentStore:
    """参数网格实验的原子持久化 (tmp + os.replace)。

    文件路径: {data_dir}/research/parameter_grid_experiments/{experiment_id}.json
    每次保存为完整 JSON 原子覆盖, 支持实验重启后读取。
    """

    def __init__(self, data_dir: Path | str) -> None:
        self.dir = Path(data_dir) / "research" / "parameter_grid_experiments"

    def _path(self, experiment_id: str) -> Path:
        if not _SAFE_ID_RE.match(experiment_id):
            raise ValueError(f"invalid experiment_id: {experiment_id!r}")
        return self.dir / f"{experiment_id}.json"

    def save(self, experiment: GridExperiment) -> None:
        """原子写: tmp 文件 + os.replace。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(experiment.experiment_id)
        payload = json.dumps(experiment.to_dict(), ensure_ascii=False, indent=2, default=str)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)

    def load(self, experiment_id: str) -> GridExperiment:
        path = self._path(experiment_id)
        if not path.exists():
            raise KeyError(experiment_id)
        return GridExperiment.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def exists(self, experiment_id: str) -> bool:
        try:
            return self._path(experiment_id).exists()
        except ValueError:
            return False

    def find_by_config_hash(self, config_hash: str) -> GridExperiment | None:
        """扫描已持久化实验, 返回 config_hash 匹配的最新实验 (供去重)。"""
        if not self.dir.exists():
            return None
        best: GridExperiment | None = None
        for path in self.dir.glob("pg-*.json"):
            try:
                exp = GridExperiment.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                continue
            if exp.config_hash == config_hash:
                if best is None or exp.updated_at > best.updated_at:
                    best = exp
        return best


# ================================================================
# 执行
# ================================================================

def run_grid(
    service: StrategyBacktestService,
    store: ParameterGridExperimentStore,
    base_config: StrategyBacktestConfig,
    scenarios: list[StrategyBacktestConfig],
    ng: NormalizedGrid,
    experiment_id: str,
    config_hash: str,
    max_workers: int = GRID_MAX_WORKERS,
    progress_cb: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> GridExperiment:
    """有界并发执行参数网格实验。

    1. 预加载共享 panel (所有 scenario 同区间, 只 load 一次)
    2. ThreadPoolExecutor 并发执行各 scenario (共享 panel)
    3. 按 objective 打分 + 排序 + rank
    4. 最优 scenario 复用 robustness.py 后处理
    5. 每完成一个 scenario 原子持久化进度

    返回最终 GridExperiment。
    """
    from app.backtest import robustness as rb

    now = _now()
    experiment = GridExperiment(
        experiment_id=experiment_id,
        config_hash=config_hash,
        strategy_id=base_config.strategy_id,
        objective=ng.objective,
        base_config=StrategyBacktestService._config_to_dict(base_config),
        grid={k: ng.grid[k] for k in sorted(ng.grid)},
        requested_count=ng.requested_count,
        scenario_count=ng.scenario_count,
        max_scenarios=ng.max_scenarios,
        truncated=ng.truncated,
        status="running",
        created_at=now,
        updated_at=now,
        total=len(scenarios),
    )
    store.save(experiment)

    # ── 预加载共享 panel ──────────────────────────────
    shared_panel = None
    try:
        load_start, load_end = service.compute_load_range(base_config)
        shared_panel = service.engine.load_panel(base_config.symbols, load_start, load_end)
    except Exception as e:  # noqa: BLE001
        logger.warning("grid shared panel preload failed, falling back to per-scenario load: %s", e)

    keys = sorted(ng.grid.keys())

    def _run_one(idx: int, cfg: StrategyBacktestConfig) -> tuple[GridScenarioResult, StrategyBacktestResult | None]:
        combo_params = {k: (cfg.params or {}).get(k) for k in keys} if keys else dict(cfg.params or {})
        # 排队期间已被取消 → 立即返回, 不启动回测
        if cancel_event is not None and cancel_event.is_set():
            return (
                GridScenarioResult(
                    scenario_id=f"s{idx:04d}",
                    params=combo_params,
                    score=None,
                    error="cancelled",
                ),
                None,
            )
        result = service.run(cfg, cancel_event=cancel_event, panel=shared_panel)
        if result.error:
            sr = GridScenarioResult(
                scenario_id=f"s{idx:04d}",
                params=combo_params,
                stats={},
                score=None,
                error=result.error,
                elapsed_ms=result.elapsed_ms,
            )
        else:
            sr = GridScenarioResult(
                scenario_id=f"s{idx:04d}",
                params=combo_params,
                stats=result.stats,
                score=score_scenario(result.stats, ng.objective),
                elapsed_ms=result.elapsed_ms,
            )
        return sr, result

    results: list[tuple[GridScenarioResult, StrategyBacktestResult | None]] = []
    completed_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(_run_one, i, cfg): i
            for i, cfg in enumerate(scenarios)
        }
        try:
            for fut in as_completed(future_map):
                idx = future_map[fut]
                try:
                    results.append(fut.result())
                except Exception as e:  # noqa: BLE001
                    results.append((
                        GridScenarioResult(
                            scenario_id=f"s{idx:04d}",
                            params={},
                            score=None,
                            error=str(e),
                        ),
                        None,
                    ))
                completed_count += 1
                experiment.completed = completed_count
                experiment.scenarios = [sr for sr, _ in sorted(results, key=lambda x: x[0].scenario_id)]
                experiment.updated_at = _now()
                if progress_cb:
                    progress_cb({
                        "type": "scenario_done",
                        "completed": completed_count,
                        "total": experiment.total,
                        "scenario_id": results[-1][0].scenario_id,
                    })
                store.save(experiment)
        finally:
            # 取消未完成的 future (排队中的, _run_one 内部会检查 cancel_event)
            for f in future_map:
                if not f.done():
                    f.cancel()

    cancelled = cancel_event is not None and cancel_event.is_set()

    # ── 排序 + rank (确定性: score desc → scenario_id asc) ──
    valid = [sr for sr, _ in results if sr.error is None and sr.score is not None]
    errored = [sr for sr, _ in results if sr.error is not None]
    valid.sort(key=lambda sr: (-(sr.score or 0.0), sr.scenario_id))
    for rank, sr in enumerate(valid, 1):
        sr.rank = rank

    all_sorted = sorted(results, key=lambda x: x[0].scenario_id)
    experiment.scenarios = [sr for sr, _ in all_sorted]
    experiment.completed = len(results)
    experiment.status = "cancelled" if cancelled else "completed"

    # ── 最优 scenario 稳健性后处理 ──
    if valid:
        best_sr = valid[0]
        experiment.best_scenario_id = best_sr.scenario_id
        best_result: StrategyBacktestResult | None = None
        for sr, res in all_sorted:
            if sr.scenario_id == best_sr.scenario_id:
                best_result = res
                break
        if best_result is not None and best_result.equity_curve:
            rets = rb.returns_from_equity_curve(best_result.equity_curve)
            robustness: dict = {}
            if len(rets) >= 2:
                robustness["bootstrap"] = rb.bootstrap_sharpe_ci(rets, n_boot=1000, seed=42)
                robustness["mc_permutation"] = rb.mc_permutation_pvalue(rets, n_perm=1000, seed=42)
            robustness["exit_breakdown"] = rb.exit_reason_breakdown(best_result.trades)
            experiment.robustness = robustness

    experiment.updated_at = _now()
    store.save(experiment)
    return experiment
