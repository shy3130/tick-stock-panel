"""策略引擎 — 加载、执行、评分。

职责: 从文件系统加载策略 Python 模块，执行两阶段过滤(基础+策略)，
     通用评分排序。
不知道: AI、API、前端、配置持久化、回测。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.config import settings
from app.strategy.scoring import (
    SCORING_DIRECTION_LOW,
    effective_scoring,
    effective_scoring_directions,
    materialize_scoring_columns,
    scoring_dependencies,
    scoring_value_expr,
    scoring_warmup_bars,
)

logger = logging.getLogger(__name__)

# 引擎级默认基础过滤 — 策略未定义 BASIC_FILTER 时兜底
DEFAULT_BASIC_FILTER: dict = {
    "price_min": 3,
    "price_max": 300,
    "market_cap_min": 10e8,
    "float_cap_min": None,
    "float_cap_max": None,
    "amount_min": 0.2e8,
    "amount_max": None,
    "turnover_min": None,
    "turnover_max": None,
    "exclude_st": True,
    "exclude_new_days": 30,
    "boards": ["沪主板", "深主板", "创业板", "科创板", "北交所"],
}

# 叠加策略硬上限：子策略数量。控制信号计算成本与字段并集膨胀，避免 OOM。
MAX_COMPOSITE_CHILDREN = 8


def _normalize_param_defs(params: Any) -> list[dict]:
    """把 META["params"] 归一化为标准 list[dict] (每项含 id/label/type/default).

    支持的输入格式:
    - list[dict] (标准): 保持, 补齐缺失的 id/label/type/default 字段
    - dict ({"lookback": 20} 或 {"lookback": {"default": 20, "type": "int"}}):
      按 key 作参数 id 转换
    - list[str] (["lookback", "threshold"]): 每项作 id, default=None
    - 其他类型 / 不可识别项: 丢弃并 warning 记录; 整体异常则返回空 list (降级而非崩溃)

    保证下游 {p["id"]: p["default"] for p in params} 永远不会因格式问题抛 TypeError.
    """
    if params is None:
        return []

    # dict 格式: {"lookback": 20} 或 {"lookback": {"default": 20, "type": "int"}}
    if isinstance(params, dict):
        items: list[dict] = []
        for key, val in params.items():
            if not isinstance(key, str) or not key:
                continue
            if isinstance(val, dict):
                item = {"id": key, **val}
            else:
                item = {"id": key, "default": val}
            items.append(item)
        return [_normalize_param_item(item) for item in items]

    # 期望是 list/tuple, 其他类型直接降级
    if not isinstance(params, (list, tuple)):
        logger.warning("strategy params 非标准格式 (%s), 已降级为空 list", type(params).__name__)
        return []

    result: list[dict] = []
    for i, p in enumerate(params):
        if isinstance(p, str):
            result.append({"id": p, "default": None})
        elif isinstance(p, dict):
            item = _normalize_param_item(p)
            if item:  # 缺 id 等异常项 _normalize_param_item 返回空 dict, 丢弃
                result.append(item)
        else:
            logger.warning("strategy params[%d] 不可识别 (%s), 已丢弃", i, type(p).__name__)
    return result


def _normalize_param_item(item: dict) -> dict:
    """补齐单个参数定义的默认字段, 保证 id/label/type/default 都存在."""
    norm = dict(item)
    if "id" not in norm or not norm["id"]:
        logger.warning("strategy param 定义缺少 id, 已丢弃: %s", item)
        return {}
    norm.setdefault("label", str(norm["id"]))
    norm.setdefault("type", "float")
    norm.setdefault("default", None)
    return norm


def _parse_composite_children(raw: Any) -> CompositeSpec:
    """解析 META["children"] 为 CompositeSpec。

    每项形如 {"strategy_id": "xxx", "weight": 0.4}。仅做结构和权重校验:
    - 非空 list, 每项含合法 strategy_id 与非负 weight
    - 数量 <= MAX_COMPOSITE_CHILDREN (超出在加载期拒绝, 避免信号计算成本爆炸)
    子策略的存在性/非嵌套/asset_types 一致性由 _load_all 两阶段校验保证。
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("composite strategy META['children'] must be a non-empty list")
    if len(raw) > MAX_COMPOSITE_CHILDREN:
        raise ValueError(
            f"composite strategy children count {len(raw)} exceeds limit {MAX_COMPOSITE_CHILDREN}"
        )
    children: list[CompositeChild] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"composite children[{i}] must be a dict")
        cid = item.get("strategy_id")
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"composite children[{i}] missing non-empty 'strategy_id'")
        if cid in seen:
            raise ValueError(f"composite children[{i}] duplicate strategy_id {cid!r}")
        seen.add(cid)
        weight = item.get("weight", 1.0)
        try:
            weight = float(weight)
        except (TypeError, ValueError) as e:
            raise ValueError(f"composite children[{i}] weight must be a number") from e
        if weight < 0:
            raise ValueError(f"composite children[{i}] weight must be >= 0")
        children.append(CompositeChild(strategy_id=cid, weight=weight))
    return CompositeSpec(children=tuple(children))


@dataclass
class StrategyDataContext:
    """一次策略调用所需的标准数据上下文。"""

    asset_type: str
    timeframe: str
    as_of: date
    current: pl.DataFrame | None = None
    history: pl.DataFrame | None = None
    # 仅 1m 分支: 策略声明 META["daily_history_bars"] 时注入的日线 enriched 窗口,
    # 供分钟策略叠加日线维度条件 (如 N 日内涨停过); 未声明时为 None。
    daily_history: pl.DataFrame | None = None
    market: Any | None = None
    cache_key: str | None = None


@dataclass(frozen=True)
class CompositeChild:
    """叠加策略的一个子策略引用。"""

    strategy_id: str
    weight: float


@dataclass(frozen=True)
class CompositeSpec:
    """叠加策略的子策略声明（无业务代码，仅引用与权重）。

    引用合法性与一致性由 _load_all 两阶段校验保证：子策略必须存在、
    非嵌套、asset_types 一致、数量 ≤ MAX_COMPOSITE_CHILDREN。
    """

    children: tuple[CompositeChild, ...]


@dataclass
class StrategyDef:
    """加载后的策略定义（只读数据 + filter 函数引用）"""
    meta: dict
    basic_filter: dict
    entry_signals: list[str]
    exit_signals: list[str]
    stop_loss: float | None
    trailing_stop: float | None
    trailing_take_profit_activate: float | None
    trailing_take_profit_drawdown: float | None
    max_hold_days: int | None
    filter_fn: Callable[[pl.DataFrame, dict], pl.Expr] | None
    filter_history_fn: Callable[[pl.DataFrame, dict], pl.DataFrame] | None
    lookback_days: int
    source: str  # "custom" | "ai" | "composite"
    required_features: frozenset[str] = field(default_factory=frozenset)
    file_path: Path | None = None
    execution_backend: str = "polars_expr"
    matrix_strategy: Any | None = None
    composite: CompositeSpec | None = None  # 仅 backend=="composite" 时非空
    # 仅 backend=="minute_filter" 时非空: 输入为当日分钟K窗口, 输出为命中标的行
    filter_minute_history_fn: Callable[[pl.DataFrame, dict], pl.DataFrame] | None = None
    # 仅 minute_filter: META["daily_history_bars"] 声明需要的日线历史窗口 (0=不需要;
    # >0 时 filter_minute_history 必须接受 daily 关键字, 引擎注入 context.daily_history)
    minute_daily_bars: int = 0


@dataclass
class StrategyResult:
    """策略执行结果"""
    as_of: date
    strategy_id: str
    rows: list[dict] = field(default_factory=list)
    total: int = 0
    elapsed_ms: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)
    entry_signal_hits: list[dict] = field(default_factory=list)
    exit_signal_hits: list[dict] = field(default_factory=list)


@dataclass
class _RealtimeMatrixEntry:
    fingerprint: tuple[Any, ...]
    buffer: Any


class StrategyEngine:
    """策略引擎 — 策略加载 + 执行 + 评分"""

    _module_load_lock = threading.RLock()

    def __init__(
        self,
        strategy_dirs: list[Path] | None = None,
        *,
        override_loader: Callable[[str], dict] | None = None,
    ):
        self._strategies: dict[str, StrategyDef] = {}
        self._load_errors: list[dict] = []  # 加载失败的策略 [{file, error}]
        self._strategy_dirs = strategy_dirs or []
        self._realtime_matrices: dict[str, _RealtimeMatrixEntry] = {}
        self._realtime_matrix_lock = threading.RLock()
        # 可选的 override 加载器: 叠加策略执行时用它查子策略的用户覆盖配置,
        # 保证 composite 内跑子策略与单独跑子策略使用同一口径(CONTRIBUTING §5.1)。
        # None 时(测试/无 data_dir) 子策略用默认参数, 不报错。
        self._override_loader = override_loader
        self._load_all(retain_previous_on_error=False)

    # ================================================================
    # 加载
    # ================================================================

    def _load_all(self, *, retain_previous_on_error: bool) -> bool:
        candidates: dict[str, StrategyDef] = {}
        candidate_paths: dict[str, Path] = {}
        errors: list[dict] = []
        duplicate_ids: set[str] = set()
        for d in self._strategy_dirs:
            if not d.exists():
                continue
            for f in sorted(d.glob("*.py")):
                if f.name.startswith("_"):
                    continue
                try:
                    s = self._load_file(f)
                    strategy_id = str(s.meta["id"])
                    if strategy_id in duplicate_ids:
                        errors.append({
                            "file": str(f),
                            "error": f"duplicate strategy id {strategy_id!r}",
                        })
                        continue
                    if strategy_id in candidates:
                        previous_path = candidate_paths.pop(strategy_id)
                        candidates.pop(strategy_id)
                        duplicate_ids.add(strategy_id)
                        message = f"duplicate strategy id {strategy_id!r}"
                        errors.extend([
                            {"file": str(previous_path), "error": message},
                            {"file": str(f), "error": message},
                        ])
                        continue
                    candidates[strategy_id] = s
                    candidate_paths[strategy_id] = f
                except Exception as e:
                    logger.warning("load strategy %s failed: %s", f.name, e)
                    errors.append({"file": str(f), "error": str(e)})

        # 第二阶段: 校验 composite 引用合法性。
        # _load_file 是 staticmethod, 加载单个文件时无法判断 child 是否存在;
        # 此处已拿到全部 candidates, 可做引用、嵌套、asset_types 与数量校验。
        # 孤儿 composite (引用不合法) 被移出 candidates 并记入 errors,
        # 但不触发整体 reload 失败 —— 不波及其他正常策略(插件隔离原则)。
        for sid in list(candidates):
            strategy = candidates[sid]
            if strategy.execution_backend != "composite" or strategy.composite is None:
                continue
            error = self._validate_composite_references(sid, strategy, candidates)
            if error is not None:
                errors.append({
                    "file": str(strategy.file_path) if strategy.file_path else sid,
                    "error": error,
                })
                candidates.pop(sid, None)
                candidate_paths.pop(sid, None)

        self._load_errors = errors
        if errors and retain_previous_on_error:
            return False

        self._strategies = candidates
        for strategy_id, strategy in candidates.items():
            logger.debug("loaded strategy: %s (%s)", strategy_id, strategy.source)
        return not errors

    def load_errors(self) -> list[dict]:
        """返回最近一次 _load_all 中加载失败的策略 [{file, error}]。"""
        return list(self._load_errors)

    @staticmethod
    def _validate_composite_references(
        sid: str,
        strategy: StrategyDef,
        candidates: dict[str, StrategyDef],
    ) -> str | None:
        """校验 composite 策略的引用合法性。返回错误描述或 None。

        规则(首版硬约束):
        - 每个 child 必须已加载(candidates 中存在)
        - 禁止 composite 嵌套 composite(子策略必须是叶子)
        - child 的 asset_types / timeframes 必须与父 composite 完全一致
        - 数量 <= MAX_COMPOSITE_CHILDREN
        任一不满足返回错误描述, 由 _load_all 移除该孤儿策略(不波及无辜)。
        """
        assert strategy.composite is not None
        children = strategy.composite.children
        if len(children) > MAX_COMPOSITE_CHILDREN:
            return (
                f"composite strategy {sid} children count {len(children)} "
                f"exceeds limit {MAX_COMPOSITE_CHILDREN}"
            )
        parent_assets = list(strategy.meta.get("asset_types", ["stock"]))
        parent_timeframes = list(strategy.meta.get("timeframes", ["1d"]))
        for child in children:
            child_def = candidates.get(child.strategy_id)
            if child_def is None:
                return f"composite strategy {sid} 引用的子策略 {child.strategy_id!r} 不存在"
            if child_def.execution_backend == "composite":
                return (
                    f"composite strategy {sid} 引用的子策略 {child.strategy_id!r} "
                    f"也是叠加策略; 首版禁止嵌套叠加"
                )
            child_assets = list(child_def.meta.get("asset_types", ["stock"]))
            if not set(parent_assets).issubset(set(child_assets)):
                return (
                    f"composite strategy {sid} 的 asset_types {parent_assets} "
                    f"未被子策略 {child.strategy_id!r} 完全支持(支持 {child_assets})"
                )
            child_timeframes = list(child_def.meta.get("timeframes", ["1d"]))
            if not set(parent_timeframes).issubset(set(child_timeframes)):
                return (
                    f"composite strategy {sid} 的 timeframes {parent_timeframes} "
                    f"未被子策略 {child.strategy_id!r} 完全支持(支持 {child_timeframes})"
                )
        return None

    @staticmethod
    def _load_file(path: Path) -> StrategyDef:
        """从 Python 文件加载策略定义"""
        # 纵深防御: 执行前再跑一次 AST 安全校验, 防止策略文件被直接篡改
        # 绕过 API 校验后, 在 exec_module 时执行恶意代码。
        dependency_paths = [
            candidate
            for candidate in path.parent.glob("_*.py")
            if candidate != path
        ]
        dependency_names = frozenset(candidate.stem for candidate in dependency_paths)
        try:
            code = path.read_text(encoding="utf-8")
            from app.strategy.ai_generator import AIStrategyGenerator
            AIStrategyGenerator._validate_safety(
                code,
                extra_allowed_import_modules=dependency_names,
            )
            for dependency_path in dependency_paths:
                AIStrategyGenerator._validate_safety(
                    dependency_path.read_text(encoding="utf-8"),
                    extra_allowed_import_modules=frozenset({
                        "collections.abc",
                        "types",
                        "typing",
                    }),
                    extra_allowed_calls=frozenset({"vars"}),
                )
        except ValueError:
            raise
        except Exception:  # noqa: BLE001
            # 文件读不到/语法错等: 不阻断, 让下方 exec_module 抛原样错误
            pass

        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot load module from {path}")
        mod = importlib.util.module_from_spec(spec)
        with StrategyEngine._module_load_lock:
            previous_module = sys.modules.get(spec.name)
            sys.modules[spec.name] = mod
            inserted_path = str(path.parent)
            sys.path.insert(0, inserted_path)
            try:
                for dependency_name in dependency_names:
                    sys.modules.pop(dependency_name, None)
                spec.loader.exec_module(mod)
            except Exception:
                if previous_module is None:
                    sys.modules.pop(spec.name, None)
                else:
                    sys.modules[spec.name] = previous_module
                raise
            finally:
                try:
                    sys.path.remove(inserted_path)
                except ValueError:
                    pass

        meta = dict(getattr(mod, "META", {}) or {})
        meta.setdefault("id", path.stem)
        meta.setdefault("name", path.stem)
        meta.setdefault("description", "")
        meta.setdefault("tags", [])
        meta.setdefault("params", [])
        meta.setdefault("scoring", {})
        meta.setdefault("order_by", "score")
        meta.setdefault("descending", True)
        meta.setdefault("limit", 100)

        # research/ 目录: 挖掘功能的研究模板 (research_only), 对外按 custom 口径。
        source = "custom"
        normalized_path = str(path).replace("\\", "/")
        if "/ai/" in normalized_path:
            source = "ai"
        elif "/composite/" in normalized_path:
            source = "composite"

        meta.setdefault("asset_types", ["stock"])
        meta.setdefault("timeframes", ["1d"])
        for field_name in ("asset_types", "timeframes"):
            values = meta.get(field_name)
            if (
                not isinstance(values, (list, tuple))
                or not values
                or any(not isinstance(value, str) or not value for value in values)
            ):
                raise ValueError(f"META[{field_name!r}] must be a non-empty string list")
            meta[field_name] = list(dict.fromkeys(values))

        # 归一化 params 为标准 list[dict]: custom/AI 策略的 META["params"] 可能是
        # dict / list[str] 等非标准格式 (LLM 偶发漂移 / 用户手改), 不归一化的话会在
        # _strategy_detail() 的 {p["id"]: p["default"] for p in params} 处抛 TypeError,
        # 导致整个 /api/strategies 列表 500. 降级为空 list 而非崩溃, 策略仍可见可用.
        meta["params"] = _normalize_param_defs(meta.get("params"))

        # 合并默认基础过滤
        bf = {**DEFAULT_BASIC_FILTER}
        strat_bf = getattr(mod, "BASIC_FILTER", None)
        if strat_bf:
            bf.update(strat_bf)
        # meta 里的 basic_filter 也合并（优先级最高）
        meta_bf = meta.get("basic_filter")
        if meta_bf:
            bf.update(meta_bf)

        filter_fn = getattr(mod, "filter", None)
        filter_history_fn = getattr(mod, "filter_history", None)
        filter_minute_history_fn = getattr(mod, "filter_minute_history", None)
        execution_backend = str(
            getattr(
                mod,
                "EXECUTION_BACKEND",
                meta.get(
                    "execution_backend",
                    "python_history_legacy" if filter_history_fn else "polars_expr",
                ),
            )
        )
        valid_backends = {"polars_expr", "matrix_native", "python_history_legacy", "composite", "minute_filter"}
        if execution_backend not in valid_backends:
            raise ValueError(
                f"unsupported execution backend {execution_backend!r}; "
                f"expected one of {sorted(valid_backends)}"
            )

        matrix_strategy = getattr(mod, "MATRIX_STRATEGY", None)
        composite_spec: CompositeSpec | None = None
        minute_daily_bars = 0
        if execution_backend == "matrix_native":
            from app.backtest.matrix import MatrixStrategy

            if matrix_strategy is None:
                raise ValueError("matrix_native strategy must declare MATRIX_STRATEGY")
            if not isinstance(matrix_strategy, MatrixStrategy):
                raise TypeError("MATRIX_STRATEGY must implement MatrixStrategy")
            if filter_fn is not None or filter_history_fn is not None:
                raise ValueError("matrix_native strategy must not declare filter or filter_history")
        elif execution_backend == "polars_expr":
            if filter_fn is None or filter_history_fn is not None:
                raise ValueError("polars_expr strategy must declare only filter")
        elif execution_backend == "composite":
            # 叠加策略是声明式的: 不含业务代码, 仅通过 META["children"] 引用其他策略。
            # 引用合法性(子策略存在/非嵌套/asset_types 一致/数量上限)延后到
            # _load_all 两阶段校验 —— 因为此时注册表尚未加载完, 无法判断 child 是否存在。
            if (
                filter_fn is not None
                or filter_history_fn is not None
                or matrix_strategy is not None
            ):
                raise ValueError(
                    "composite strategy must not declare filter, filter_history or MATRIX_STRATEGY"
                )
            composite_spec = _parse_composite_children(meta.get("children"))
        elif execution_backend == "minute_filter":
            # 分钟形态策略: 只声明 filter_minute_history; 数据源是本地当日分钟K分区
            # (由 ScreenerService.build_strategy_context 的 1m 分支注入), 因此 timeframes
            # 必须且只能是 ["1m"] — 混入 1d 会让日线 context 走错数据路径。
            if (
                filter_minute_history_fn is None
                or filter_fn is not None
                or filter_history_fn is not None
                or matrix_strategy is not None
            ):
                raise ValueError(
                    "minute_filter strategy must declare only filter_minute_history"
                )
            if meta.get("timeframes") != ["1m"]:
                raise ValueError(
                    "minute_filter strategy must declare timeframes == ['1m']"
                )
            # 可选日线历史窗口: 声明 daily_history_bars 时 fn 必须接受 daily 关键字,
            # 引擎会把 context.daily_history (enriched 日线窗口) 注入进来。
            minute_daily_bars = int(meta.get("daily_history_bars") or 0)
            if minute_daily_bars < 0 or minute_daily_bars > 250:
                raise ValueError(
                    "minute_filter daily_history_bars must be within [0, 250]"
                )
            if minute_daily_bars > 0:
                import inspect

                sig = inspect.signature(filter_minute_history_fn)
                if "daily" not in sig.parameters:
                    raise ValueError(
                        "minute_filter daily_history_bars requires "
                        "filter_minute_history to accept a 'daily' keyword"
                    )
        elif filter_history_fn is None or filter_fn is not None:
            raise ValueError("python_history_legacy strategy must declare only filter_history")

        return StrategyDef(
            meta=meta,
            basic_filter=bf,
            entry_signals=getattr(mod, "ENTRY_SIGNALS", []),
            exit_signals=getattr(mod, "EXIT_SIGNALS", []),
            stop_loss=getattr(mod, "STOP_LOSS", None),
            trailing_stop=getattr(mod, "TRAILING_STOP", None),
            trailing_take_profit_activate=getattr(mod, "TRAILING_TAKE_PROFIT_ACTIVATE", None),
            trailing_take_profit_drawdown=getattr(mod, "TRAILING_TAKE_PROFIT_DRAWDOWN", None),
            max_hold_days=getattr(mod, "MAX_HOLD_DAYS", None),
            filter_fn=filter_fn,
            filter_history_fn=filter_history_fn,
            required_features=frozenset(meta.get("required_features", []) or [])
            | frozenset(getattr(mod, "REQUIRED_FEATURES", []) or []),
            lookback_days=int(getattr(mod, "LOOKBACK_DAYS", meta.get("lookback_days", 1)) or 1),
            source=source,
            file_path=path,
            execution_backend=execution_backend,
            matrix_strategy=matrix_strategy,
            composite=composite_spec,
            filter_minute_history_fn=filter_minute_history_fn,
            minute_daily_bars=minute_daily_bars,
        )

    def reload(self) -> None:
        """原子热重载；任一策略失败时保留上一版注册表。"""
        if not self._load_all(retain_previous_on_error=True):
            details = "; ".join(
                f"{item['file']}: {item['error']}" for item in self._load_errors
            )
            raise ValueError(f"strategy reload failed: {details}")
        with self._realtime_matrix_lock:
            self._realtime_matrices.clear()

    # ================================================================
    # 查询
    # ================================================================

    def list_strategies(self, *, include_research: bool = False) -> list[dict]:
        """Return public strategy metadata unless research templates are requested."""
        result = []
        for s in self._strategies.values():
            if s.meta.get("research_only") and not include_research:
                continue
            result.append({
                **s.meta,
                "source": s.source,
                "execution_backend": s.execution_backend,
            })
        return result

    def strategy_definitions(self) -> tuple[StrategyDef, ...]:
        """Return the immutable registry snapshot for framework dependency planning."""
        return tuple(self._strategies.values())

    def get(self, strategy_id: str) -> StrategyDef:
        s = self._strategies.get(strategy_id)
        if not s:
            raise ValueError(f"unknown strategy: {strategy_id}")
        return s

    def has(self, strategy_id: str) -> bool:
        return strategy_id in self._strategies

    def unregister(self, strategy_id: str) -> bool:
        """从运行时注册表移除单个策略, 不重新加载其他策略文件。"""
        if strategy_id not in self._strategies:
            return False
        strategies = dict(self._strategies)
        strategies.pop(strategy_id)
        self._strategies = strategies
        with self._realtime_matrix_lock:
            self._realtime_matrices.clear()
        return True

    def find_dependents(self, strategy_id: str) -> list[str]:
        """返回引用了 strategy_id 作为子策略的所有 composite 策略 id。

        供删除校验使用: 删除被引用的子策略会令 composite 加载失败,
        删除前应阻止(fail-closed)或提示用户先解除引用。策略数量通常很小,
        线性遍历注册表即可, 无需维护反向索引。
        """
        dependents: list[str] = []
        for sid, strategy in self._strategies.items():
            if strategy.execution_backend != "composite" or strategy.composite is None:
                continue
            if any(c.strategy_id == strategy_id for c in strategy.composite.children):
                dependents.append(sid)
        return dependents

    @staticmethod
    def validate_context(strategy: StrategyDef, context: StrategyDataContext) -> None:
        asset_types = strategy.meta.get("asset_types", ["stock"])
        if context.asset_type not in asset_types:
            raise ValueError(
                f"strategy {strategy.meta['id']} does not support asset_type "
                f"{context.asset_type!r}; supported={asset_types}"
            )
        timeframes = strategy.meta.get("timeframes", ["1d"])
        if context.timeframe not in timeframes:
            raise ValueError(
                f"strategy {strategy.meta['id']} does not support timeframe "
                f"{context.timeframe!r}; supported={timeframes}"
            )

    @staticmethod
    def resolve_params(
        strategy: StrategyDef,
        params: dict | None = None,
        overrides: dict | None = None,
    ) -> dict:
        """Resolve one parameter source of truth for every strategy consumer."""
        resolved = {
            item["id"]: item.get("default")
            for item in strategy.meta.get("params", [])
            if isinstance(item, dict) and item.get("id")
        }
        saved = (overrides or {}).get("params")
        if isinstance(saved, dict):
            resolved.update(saved)
        if params:
            resolved.update(params)
        return resolved

    @staticmethod
    def _result_limit(strategy: StrategyDef, overrides: dict | None) -> int | None:
        if overrides and "display_limit" in overrides:
            value = overrides.get("display_limit")
            if value in (None, 0):
                return None
            return max(0, int(value))
        value = strategy.meta.get("limit", 100)
        if value in (None, 0):
            return None
        return max(0, int(value))

    def minute_daily_history_bars(self, strategy_ids: list[str]) -> int:
        """1m 分支需要的日线 enriched 窗口大小: 各 minute_filter 策略声明的
        META["daily_history_bars"] 取 max, 未声明 (纯分钟策略) 为 0。"""
        required = 0
        for strategy_id in strategy_ids:
            strategy = self.get(strategy_id)
            if strategy.execution_backend == "minute_filter":
                required = max(required, strategy.minute_daily_bars)
        return required

    def required_history_bars(
        self,
        strategy_ids: list[str],
        *,
        params_map: dict[str, dict] | None = None,
        overrides_map: dict[str, dict] | None = None,
    ) -> int:
        params_map = params_map or {}
        overrides_map = overrides_map or {}
        required = 1
        for strategy_id in strategy_ids:
            strategy = self.get(strategy_id)
            overrides = overrides_map.get(strategy_id) or {}
            # 叠加条件含「前 N 日」偏移时, 历史窗口需覆盖其回看深度 (as_of + N 个交易日)。
            overlay = self._effective_overlay(overrides)
            if overlay:
                from app.strategy import custom_signals as _custom_signals
                overlay_days = _custom_signals.overlay_max_days(overlay)
                if overlay_days > 0:
                    required = max(required, overlay_days + 1)
            scoring = effective_scoring(strategy.meta.get("scoring"), overrides)
            required = max(required, scoring_warmup_bars(scoring))
            if strategy.execution_backend == "matrix_native":
                params = self.resolve_params(
                    strategy,
                    params_map.get(strategy_id),
                    overrides,
                )
                required = max(
                    required,
                    int(strategy.matrix_strategy.required_warmup_bars(params)) + 1,
                )
            elif strategy.execution_backend == "composite":
                # composite 预热 = 各子策略预热的 max。
                # 子策略已通过加载期校验(非嵌套叶子), 这里展开一层即可。
                if strategy.composite is None:
                    continue
                child_ids = [c.strategy_id for c in strategy.composite.children]
                required = max(
                    required,
                    self.required_history_bars(child_ids, params_map=params_map),
                )
            elif strategy.filter_history_fn:
                # lookback_days 优先取自解析后的参数(默认值/保存覆盖/本次调用),
                # 静态 LOOKBACK_DAYS 兜底。策略可能只把窗口声明为参数
                # (如 AI 生成策略), 此时 strategy.lookback_days 回退到 1,
                # 不解析参数会低估历史需求 → build_strategy_context 跳过加载 → 运行时报错。
                params = self.resolve_params(
                    strategy,
                    params_map.get(strategy_id),
                    overrides_map.get(strategy_id),
                )
                lookback = int(strategy.lookback_days)
                param_lookback = params.get("lookback_days")
                if isinstance(param_lookback, (int, float)) and param_lookback > 0:
                    lookback = max(lookback, int(param_lookback))
                required = max(required, lookback)
        return required

    def prepare_realtime_matrix(
        self,
        context: StrategyDataContext,
        strategy_ids: list[str],
        *,
        params_map: dict[str, dict] | None = None,
        overrides_map: dict[str, dict] | None = None,
    ):
        """Build once, then update only the latest live bar for matrix strategies."""
        from app.backtest.matrix import RealtimeMarketDataMatrix

        current = context.current
        if current is None:
            raise ValueError("realtime matrix context requires current data")
        if current.is_empty() or not strategy_ids:
            return None
        params_map = params_map or {}
        overrides_map = overrides_map or {}
        field_columns: set[str] = set()
        max_warmup = 1
        matrix_ids: list[str] = []
        for strategy_id in strategy_ids:
            strategy = self.get(strategy_id)
            self.validate_context(strategy, context)
            if strategy.execution_backend != "matrix_native":
                continue
            params = self.resolve_params(
                strategy,
                params_map.get(strategy_id),
                overrides_map.get(strategy_id),
            )
            matrix_ids.append(strategy_id)
            max_warmup = max(
                max_warmup,
                int(strategy.matrix_strategy.required_warmup_bars(params)) + 1,
                scoring_warmup_bars(
                    effective_scoring(
                        strategy.meta.get("scoring"),
                        overrides_map.get(strategy_id),
                    )
                ),
            )
            field_columns.update(
                self._matrix_field_columns(
                    strategy,
                    overrides_map.get(strategy_id),
                    params,
                )
            )
        if not matrix_ids:
            return None

        timestamp_col = "datetime" if "datetime" in current.columns else "date"
        if timestamp_col not in current.columns:
            raise ValueError("realtime matrix current data requires date or datetime")
        latest_value = current[timestamp_col].max()
        as_of = latest_value.date() if hasattr(latest_value, "date") else latest_value
        if not isinstance(as_of, date):
            raise ValueError("realtime matrix timestamp cannot be converted to date")
        symbols = tuple(current["symbol"].cast(pl.Utf8).unique().sort().to_list())
        fingerprint = (
            tuple(sorted(field_columns)),
            max_warmup,
            symbols,
        )

        with self._realtime_matrix_lock:
            cache_key = context.cache_key or f"{context.asset_type}:{context.timeframe}"
            entry = self._realtime_matrices.get(cache_key)
            if entry is not None and entry.fingerprint == fingerprint:
                try:
                    entry.buffer.update(current)
                    return entry.buffer.snapshot()
                except ValueError as exc:
                    logger.info("realtime matrix %s invalidated: %s", cache_key, exc)

            history = context.history
            if history is None:
                raise ValueError("matrix strategy realtime context requires history data")
            if history is None or history.is_empty():
                raise ValueError("matrix strategy realtime history is empty")
            if timestamp_col in history.columns:
                history = history.filter(pl.col(timestamp_col) != latest_value)
            elif "date" in history.columns:
                history = history.filter(pl.col("date") != as_of)
            panel = pl.concat([history, current], how="diagonal_relaxed")
            previous_builds = entry.buffer.build_count if entry is not None else 0
            buffer = RealtimeMarketDataMatrix(
                panel,
                field_columns=field_columns,
                build_count=previous_builds + 1,
            )
            self._realtime_matrices[cache_key] = _RealtimeMatrixEntry(
                fingerprint=fingerprint,
                buffer=buffer,
            )
            return buffer.snapshot()

    def realtime_matrix_stats(self, cache_key: str) -> dict[str, int]:
        with self._realtime_matrix_lock:
            entry = self._realtime_matrices.get(cache_key)
            if entry is None:
                return {"generation": 0, "build_count": 0, "update_count": 0}
            return {
                "generation": int(entry.buffer.generation),
                "build_count": int(entry.buffer.build_count),
                "update_count": int(entry.buffer.update_count),
            }

    # ================================================================
    # 执行
    # ================================================================

    def run(
        self,
        strategy_id: str,
        context: StrategyDataContext,
        pool: list[str] | None = None,
        params: dict | None = None,
        overrides: dict | None = None,
    ) -> StrategyResult:
        """执行策略: 基础过滤 → 策略过滤 → 评分排序

        Args:
            strategy_id:        策略 ID
            context:            调用级行情、资产和周期上下文
            pool:               限定股票池
            params:             本次执行显式传入的策略参数
            overrides:          用户覆盖配置 (params/basic_filter/scoring/stop_loss 等)
        """
        t0 = time.perf_counter()

        s = self.get(strategy_id)
        self.validate_context(s, context)
        as_of = context.as_of
        overrides = overrides or {}
        params = self.resolve_params(s, params, overrides)
        entry_signals = self._effective_signals(overrides, "entry_signals", s.entry_signals)
        exit_signals = self._effective_signals(overrides, "exit_signals", s.exit_signals)
        overlay_conditions = self._effective_overlay(overrides)
        # 叠加条件运行期守卫 (保存期已拦, 手改 override 文件时的双保险)
        if overlay_conditions and s.execution_backend in ("composite", "minute_filter"):
            raise ValueError(
                f"strategy {strategy_id}: 叠加条件不支持 {s.execution_backend} 策略"
            )

        if s.execution_backend == "matrix_native":
            return self._run_matrix_strategy(
                strategy_id,
                s,
                as_of,
                pool=pool,
                params=params,
                overrides=overrides,
                context=context,
                started_at=t0,
            )

        if s.execution_backend == "composite":
            return self._run_composite_strategy(
                strategy_id,
                s,
                context,
                pool=pool,
                params=params,
                overrides=overrides,
                started_at=t0,
            )

        scoring = effective_scoring(s.meta.get("scoring"), overrides)
        scoring_directions = effective_scoring_directions(overrides)
        current, history = self._materialize_scoring_frames(
            context.current,
            context.history,
            scoring,
        )

        signal_df = current if current is not None else history
        if signal_df is None:
            signal_df = pl.DataFrame()
        if not signal_df.is_empty() and "date" in signal_df.columns:
            signal_df = signal_df.filter(pl.col("date") == as_of)
        if pool and not signal_df.is_empty():
            signal_df = signal_df.filter(pl.col("symbol").is_in(pool))
        exit_signal_hits = self._collect_signal_hits(signal_df, exit_signals)

        # 普通策略只读目标日期；历史策略读取调用方注入的历史窗口。
        if s.execution_backend == "minute_filter":
            # 分钟策略: 读取调用方注入的当日分钟K窗口。无 date 列, 不按 as_of 过滤,
            # 每个命中行自带最后K线时间戳 (last_datetime)。
            if history is None:
                raise ValueError(f"strategy {strategy_id} requires minute history data")
            if history.is_empty():
                return StrategyResult(
                    as_of=as_of,
                    strategy_id=strategy_id,
                    exit_signal_hits=exit_signal_hits,
                )
            # 盘中信号列注入(csgi_): 实盘扫描与分钟回测共用本路径 — 与监控评估
            # 同一特征构造器, 单点注入保证三处口径一致。
            history = self._inject_intraday_signal_columns(history)
            missing_csgi = [
                name for name in s.required_features
                if name.startswith("csgi_") and name not in history.columns
            ]
            if missing_csgi:
                raise ValueError(
                    "策略引用了未定义的盘中信号: "
                    + ", ".join(sorted(missing_csgi))
                    + " — 请先在「自定义信号」中创建(timeframe=intraday)后再运行"
                )
            if s.minute_daily_bars > 0:
                df = s.filter_minute_history_fn(history, params, daily=context.daily_history)
            else:
                df = s.filter_minute_history_fn(history, params)
            # 基础过滤/展示列 (name/total_shares/change_pct 等) 来自 enriched 快照,
            # 在命中结果上事后联表, 避免把 enriched 列铺到全市场分钟行上。
            if current is not None and not current.is_empty():
                df = self._join_basic_columns(df, current)
        elif s.filter_history_fn:
            if history is None:
                raise ValueError(f"strategy {strategy_id} requires history data")
            df = history
            if df.is_empty():
                return StrategyResult(
                    as_of=as_of,
                    strategy_id=strategy_id,
                    exit_signal_hits=exit_signal_hits,
                )
            # 自定义信号前置校验: REQUIRED_FEATURES 引用的 csg_ 列未注入时,
            # 给出明确指引, 而不是让策略代码抛 polars 缺列错 (500)。
            # 盘中单日路径不在此校验 (该路径对带偏移信号本就优雅降级)。
            missing_csg = [
                name for name in s.required_features
                if name.startswith("csg_") and name not in df.columns
            ]
            if missing_csg:
                raise ValueError(
                    "策略引用了未定义的自定义信号: "
                    + ", ".join(sorted(missing_csg))
                    + " — 请先在「自定义信号」管理中创建对应信号后再运行"
                )
            missing_csgi = [
                name for name in s.required_features
                if name.startswith("csgi_")
            ]
            if missing_csgi:
                raise ValueError(
                    "盘中信号仅可用于分钟策略(timeframes=['1m']), 日线策略不支持: "
                    + ", ".join(sorted(missing_csgi))
                )
            df = s.filter_history_fn(df, params)
            if "date" in df.columns:
                df = df.filter(pl.col("date") == as_of)
        else:
            if current is None:
                raise ValueError(f"strategy {strategy_id} requires current data")
            df = current

        if df.is_empty():
            return StrategyResult(
                as_of=as_of,
                strategy_id=strategy_id,
                exit_signal_hits=exit_signal_hits,
            )

        # 基础过滤: 策略默认 basic_filter 兜底, 用户 override 优先覆盖。
        # 这样策略文件里写的 exclude_st/price_min 等默认值即使前端没保存也能生效。
        bf = dict(s.basic_filter) if s.basic_filter else {}
        if overrides and overrides.get("basic_filter"):
            bf.update(overrides["basic_filter"])

        # Stage 1: 基础过滤（enabled 默认开启; 显式 enabled=false 才跳过）
        if bf and bf.get("enabled", True):
            df = self._apply_basic_filter(df, bf)

        # Pool 过滤
        if pool:
            df = df.filter(pl.col("symbol").is_in(pool))

        # Stage 2: 策略过滤
        if s.filter_fn:
            expr = s.filter_fn(df, params)
            df = df.filter(expr)

        # Stage 2.5: 叠加条件 (每策略 overlay 硬过滤) — 策略过滤之后、评分之前:
        # 评分 min-max 归一化与 limit 截断都在过滤后的候选上进行; exit_signal_hits
        # 已在 :942 过滤前的帧上收集, 已持仓的卖出信号不受影响。
        if overlay_conditions:
            from app.strategy import custom_signals
            try:
                overlay_expr = custom_signals.build_overlay_expr(overlay_conditions)
            except ValueError as e:
                raise ValueError(f"叠加条件编译失败: {e}") from e
            if overlay_expr is not None:
                overlay_days = custom_signals.overlay_max_days(overlay_conditions)
                if overlay_days > 0:
                    # 「前 N 日」偏移需要更早的行来 shift: 候选帧只是当日切片, 改在
                    # 完整历史窗口上求值 as_of 行掩码, 再按 symbol 交集回候选行。
                    if history is None or history.is_empty():
                        raise ValueError(
                            "叠加条件含「前 N 日」偏移, 需要历史数据窗口; "
                            "当前调用未提供 history"
                        )
                    src = history
                else:
                    src = df
                # engine.run 不保证任意注册表因子列在帧上 → 先按条件依赖物化
                src = custom_signals.materialize_factor_columns(
                    src, {"__overlay__": overlay_expr}
                )
                missing = custom_signals.overlay_missing_columns(
                    overlay_conditions, src.columns
                )
                if missing:
                    raise ValueError(
                        "叠加条件引用的列不存在: " + ", ".join(missing)
                        + " — 请检查扩展数据拉取/因子计算是否可用"
                    )
                if overlay_days > 0:
                    # 掩码必须在完整帧上算 (先 filter 到 as_of 会截断 shift 所需的历史行),
                    # 再取 as_of 行读掩码 — 与 matrix 路径同一顺序。
                    masked = src.with_columns(
                        overlay_expr.fill_null(False).alias("__overlay__")
                    )
                    allowed = (
                        masked.filter(pl.col("date") == as_of)
                        .filter(pl.col("__overlay__"))["symbol"]
                        .cast(pl.Utf8)
                        .unique()
                        .to_list()
                    )
                    df = df.filter(pl.col("symbol").cast(pl.Utf8).is_in(allowed))
                else:
                    df = df.filter(overlay_expr.fill_null(False))

        # Stage 3: 评分
        df = self._apply_scoring(df, scoring, scoring_directions)
        entry_signal_hits = self._collect_signal_hits(df, entry_signals)
        if not entry_signals and (
            s.filter_history_fn or s.filter_fn or s.execution_backend == "minute_filter"
        ):
            entry_signal_hits = [
                {"symbol": str(symbol), "signals": []}
                for symbol in df["symbol"].cast(pl.Utf8).unique().to_list()
            ]

        # 排序 + 限制
        limit = self._result_limit(s, overrides)
        order_desc = s.meta.get("descending", True)
        if "score" in df.columns:
            df = df.sort("score", descending=order_desc)
        elif s.meta.get("order_by") and s.meta["order_by"] != "score":
            ob = s.meta["order_by"]
            if ob in df.columns:
                df = df.sort(ob, descending=order_desc)
        if limit is not None:
            df = df.head(limit)

        # 输出
        rows = _sanitize(df.to_dicts())
        elapsed = (time.perf_counter() - t0) * 1000

        scores: dict[str, float] = {}
        if "score" in df.columns:
            for r in df.iter_rows(named=True):
                scores[r["symbol"]] = float(r.get("score") or 0)

        return StrategyResult(
            as_of=as_of,
            strategy_id=strategy_id,
            rows=rows,
            total=len(rows),
            elapsed_ms=elapsed,
            scores=scores,
            entry_signal_hits=entry_signal_hits,
            exit_signal_hits=exit_signal_hits,
        )

    @staticmethod
    def _effective_signals(overrides: dict, key: str, default: list[str]) -> list[str]:
        value = overrides.get(key)
        if isinstance(value, list):
            return [str(signal) for signal in value if signal]
        return list(default or [])

    @staticmethod
    def _effective_overlay(overrides: dict) -> list[dict] | None:
        """叠加条件 override; 归一为非空条件列表或 None (None/[] = 未配置)。"""
        value = (overrides or {}).get("overlay_filter")
        if isinstance(value, list) and value:
            return value
        return None

    @staticmethod
    def _collect_signal_hits(df: pl.DataFrame, signals: list[str]) -> list[dict]:
        if df.is_empty() or not signals or "symbol" not in df.columns:
            return []
        resolved = [
            signal if signal.startswith(("signal_", "csg_")) else f"signal_{signal}"
            for signal in signals
        ]
        available = [
            (signal, column)
            for signal, column in zip(signals, resolved, strict=True)
            if column in df.columns
        ]
        if not available:
            return []
        hit_df = df.filter(pl.any_horizontal(pl.col(column).fill_null(False) for _, column in available))
        return [
            {
                "symbol": str(row["symbol"]),
                "signals": [signal for signal, column in available if row.get(column)],
            }
            for row in hit_df.iter_rows(named=True)
        ]

    def run_all(
        self,
        context: StrategyDataContext,
        params_map: dict | None = None,
        overrides_map: dict | None = None,
        *,
        strategy_ids: list[str] | None = None,
        parallel: bool = True,
    ) -> dict[str, StrategyResult]:
        """批量执行策略；当前数据、历史和矩阵均来自同一个调用上下文。

        parallel=True 时用有界线程池并发执行: 策略对 context 是只读纯函数
        (polars 计算释放 GIL), 并发不改变结果, 逐策略耗时日志不变。composite
        子策略的递归 run_all 以 parallel=False 调用, 保证嵌套时线程总数仍
        不超过 worker 上限, 不随叠加层数放大。
        """
        if context.current is None:
            raise ValueError("strategy run_all context requires current data")
        df = context.current
        params_map = params_map or {}
        overrides_map = overrides_map or {}
        selected_ids = list(self._strategies) if strategy_ids is None else strategy_ids
        selected = [(sid, self.get(sid)) for sid in selected_ids]
        for _, strategy in selected:
            self.validate_context(strategy, context)

        history_strats = [
            (sid, strategy)
            for sid, strategy in selected
            if strategy.filter_history_fn
            or strategy.execution_backend in ("matrix_native", "minute_filter")
        ]
        shared_history = context.history
        if history_strats and shared_history is None:
            raise ValueError("selected strategies require history data")

        shared_matrix = context.market
        if shared_matrix is None:
            shared_matrix = self.build_shared_matrix(
                context, selected, params_map, overrides_map
            )

        results: dict[str, StrategyResult] = {}

        def _execute(sid: str) -> tuple[str, StrategyResult]:
            started = time.perf_counter()
            result = self.run(
                sid,
                replace(
                    context,
                    current=df,
                    history=shared_history,
                    market=shared_matrix,
                ),
                params=params_map.get(sid),
                overrides=overrides_map.get(sid),
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            # >=1s 打 INFO 供热点归因 (哪些策略吃掉了 run_all 的大头), 其余 DEBUG 防噪。
            log_fn = logger.info if elapsed_ms >= 1000 else logger.debug
            log_fn(
                "run_all: strategy %s took %.0fms (total=%d)",
                sid,
                elapsed_ms,
                result.total,
            )
            return sid, result

        workers = min(settings.strategy_run_all_workers, len(selected))
        if parallel and workers > 1:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="strategy-run"
            ) as pool:
                futures = [pool.submit(_execute, sid) for sid, _ in selected]
                # 按原顺序收集: 首个失败策略的异常语义与串行执行一致。
                for future in futures:
                    sid, result = future.result()
                    results[sid] = result
        else:
            for sid, _ in selected:
                sid, result = _execute(sid)
                results[sid] = result

        return results

    def build_shared_matrix(
        self,
        context: StrategyDataContext,
        selected: list[tuple[str, StrategyDef]],
        params_map: dict | None = None,
        overrides_map: dict | None = None,
    ):
        """按所选策略的字段并集构建市场数据矩阵; 无矩阵策略或无历史时返回 None。

        渐进式 run_all (逐策略执行) 也用它一次建好并集矩阵后放入 context.market,
        避免每个 matrix_native 策略重复构建同一份大矩阵 (全市场历史, 秒级)。
        """
        params_map = params_map or {}
        overrides_map = overrides_map or {}
        matrix_strats = [
            (sid, strategy)
            for sid, strategy in selected
            if strategy.execution_backend == "matrix_native"
        ]
        history = context.history
        if not matrix_strats or history is None or history.is_empty():
            return None

        from app.backtest.matrix import build_market_data_matrix

        field_columns: set[str] = set()
        for sid, strategy in matrix_strats:
            field_columns.update(
                self._matrix_field_columns(
                    strategy,
                    overrides_map.get(sid),
                    params_map.get(sid),
                )
            )
        matrix_t0 = time.perf_counter()
        matrix = build_market_data_matrix(history, field_columns=field_columns)
        logger.info(
            "run_all: shared matrix built in %.0fms (fields=%d)",
            (time.perf_counter() - matrix_t0) * 1000,
            len(field_columns),
        )
        return matrix

    @staticmethod
    def _matrix_field_columns(
        strategy: StrategyDef,
        overrides: dict | None = None,
        params: dict | None = None,
    ) -> set[str]:
        fields = set(strategy.matrix_strategy.required_fields())
        # 参数评分字段 (如挖掘策略的因子组合) 需展开为实际数据依赖,
        # 与 backtest._resolve_matrix_native 保持同一语义, 否则虚拟因子
        # (limit_up_count_* -> consecutive_limit_ups) 在矩阵里缺字段。
        parameter_fields = getattr(
            strategy.matrix_strategy,
            "required_fields_for_params",
            None,
        )
        if callable(parameter_fields):
            fields.update(
                scoring_dependencies(
                    {str(name): 1.0 for name in parameter_fields(params or {})}
                )
            )
        basic_filter = dict(strategy.basic_filter or {})
        if (overrides or {}).get("basic_filter"):
            basic_filter.update(overrides["basic_filter"])
        for prefix, field_name in (
            ("market_cap", "total_shares"),
            ("float_cap", "float_shares"),
            ("amount", "amount"),
            ("turnover", "turnover_rate"),
        ):
            if (
                basic_filter.get(f"{prefix}_min") is not None
                or basic_filter.get(f"{prefix}_max") is not None
            ):
                fields.add(field_name)
        scoring = effective_scoring(strategy.meta.get("scoring"), overrides)
        fields.update(scoring_dependencies(scoring))
        order_by = strategy.meta.get("order_by")
        if order_by and order_by != "score":
            fields.add(str(order_by))
        return fields

    def _run_matrix_strategy(
        self,
        strategy_id: str,
        strategy: StrategyDef,
        as_of: date,
        *,
        pool: list[str] | None,
        params: dict,
        overrides: dict,
        context: StrategyDataContext,
        started_at: float,
    ) -> StrategyResult:
        from app.backtest.matrix import (
            MatrixPipelineConfig,
            MatrixStrategyPipeline,
            build_market_data_matrix,
        )

        source_panel = context.history
        market = context.market
        if market is None:
            if source_panel is None:
                raise ValueError(f"matrix strategy {strategy_id} requires history data")
            if source_panel is None or source_panel.is_empty():
                return StrategyResult(as_of=as_of, strategy_id=strategy_id)
            market = build_market_data_matrix(
                source_panel,
                field_columns=self._matrix_field_columns(strategy, overrides, params),
            )

        if source_panel is None or source_panel.is_empty():
            source_panel = context.current
        if source_panel is None or source_panel.is_empty():
            return StrategyResult(as_of=as_of, strategy_id=strategy_id)

        basic_filter = dict(strategy.basic_filter or {})
        if overrides.get("basic_filter"):
            basic_filter.update(overrides["basic_filter"])
        # 策略扫描的运行期过滤同样要按资产类型中和股票专属键 (boards/价格界),
        # 否则 ETF 候选在矩阵掩码阶段被静默清零 (#215); 函数级导入避免
        # engine ↔ backtest.strategy 的模块级循环依赖 (与上方 matrix 导入同模式)
        from app.backtest.strategy import _basic_filter_for_asset
        basic_filter = _basic_filter_for_asset(basic_filter, context.asset_type)
        scoring = effective_scoring(strategy.meta.get("scoring"), overrides)
        asset_mask = None
        if pool:
            pool_set = set(pool)
            asset_mask = np.fromiter(
                (symbol in pool_set for symbol in market.symbols),
                dtype=bool,
                count=len(market.symbols),
            )

        signals = MatrixStrategyPipeline().run(
            strategy.matrix_strategy,
            market,
            params,
            MatrixPipelineConfig(
                basic_filter=basic_filter,
                scoring=scoring,
                scoring_directions=effective_scoring_directions(overrides),
                order_by=strategy.meta.get("order_by"),
                descending=bool(strategy.meta.get("descending", True)),
                asset_mask=asset_mask,
            ),
        )
        target_ids = [
            time_id
            for time_id, label in enumerate(market.timestamp_labels)
            if label[:10] == str(as_of)
        ]
        if not target_ids:
            return StrategyResult(as_of=as_of, strategy_id=strategy_id)
        target_time = target_ids[-1]
        target_frame = self._matrix_target_frame(source_panel, as_of)
        entry_active = signals.entry[target_time]
        exit_active = signals.exit[target_time]
        if asset_mask is not None:
            entry_active = entry_active & asset_mask
            exit_active = exit_active & asset_mask
        # 叠加条件 (每策略 overlay 硬过滤): 在带完整 enriched 列 (ext_*/因子) 的
        # target_frame 上评估, 按符号掩码 entry (只挡入场, exit/持仓卖出不动)。
        # 与 polars 路径 Stage 2.5 同一套条件编译与 fail-closed 语义。
        overlay_conditions = self._effective_overlay(overrides)
        if overlay_conditions:
            from app.strategy import custom_signals
            try:
                overlay_expr = custom_signals.build_overlay_expr(overlay_conditions)
            except ValueError as e:
                raise ValueError(f"叠加条件编译失败: {e}") from e
            if overlay_expr is not None:
                overlay_days = custom_signals.overlay_max_days(overlay_conditions)
                # 「前 N 日」偏移需要更早的行来 shift: 在完整 source_panel 上求值,
                # 再取 as_of 行读掩码; 无偏移时维持现状 (只评估当日切片)。
                eval_frame = target_frame
                if overlay_days > 0:
                    if source_panel is None or source_panel.is_empty():
                        raise ValueError("叠加条件含「前 N 日」偏移, 需要历史数据窗口")
                    eval_frame = source_panel
                eval_frame = custom_signals.materialize_factor_columns(
                    eval_frame, {"__overlay__": overlay_expr}
                )
                missing = custom_signals.overlay_missing_columns(
                    overlay_conditions, eval_frame.columns
                )
                if missing:
                    raise ValueError(
                        "叠加条件引用的列不存在: " + ", ".join(missing)
                        + " — 请检查扩展数据拉取/因子计算是否可用"
                    )
                if overlay_days > 0:
                    masked = eval_frame.with_columns(
                        overlay_expr.fill_null(False).alias("__overlay__")
                    )
                    keep_frame = self._matrix_target_frame(masked, as_of)
                    keep = set(
                        keep_frame.filter(pl.col("__overlay__"))[
                            "symbol"
                        ].cast(pl.Utf8).to_list()
                    )
                else:
                    keep = set(
                        eval_frame.filter(overlay_expr.fill_null(False))[
                            "symbol"
                        ].cast(pl.Utf8).to_list()
                    )
                allowed = np.fromiter(
                    (str(symbol) in keep for symbol in market.symbols),
                    dtype=bool,
                    count=len(market.symbols),
                )
                entry_active = np.where(allowed, entry_active, 0)
        entry_signal_hits = self._matrix_signal_hits(
            entry_active,
            signals.entry_signal_code[target_time],
            signals.entry_signal_ids,
            market.symbols,
        )
        exit_signal_hits = self._matrix_signal_hits(
            exit_active,
            signals.exit_signal_code[target_time],
            signals.exit_signal_ids,
            market.symbols,
        )
        selected_assets = np.flatnonzero(entry_active != 0)
        if selected_assets.size == 0:
            return StrategyResult(
                as_of=as_of,
                strategy_id=strategy_id,
                elapsed_ms=(time.perf_counter() - started_at) * 1000,
                entry_signal_hits=entry_signal_hits,
                exit_signal_hits=exit_signal_hits,
            )

        row_by_symbol = {
            str(row["symbol"]): row
            for row in target_frame.iter_rows(named=True)
        }
        ranked: list[tuple[float, dict]] = []
        for asset_id in selected_assets:
            symbol = market.symbols[int(asset_id)]
            row = row_by_symbol.get(symbol)
            if row is None:
                continue
            score = float(signals.score[target_time, int(asset_id)])
            ranked.append((score, {**row, "score": score}))
        ranked.sort(
            key=lambda item: item[0],
            reverse=bool(strategy.meta.get("descending", True)),
        )
        limit = self._result_limit(strategy, overrides)
        selected_rows = ranked if limit is None else ranked[:limit]
        rows = _sanitize([row for _, row in selected_rows])
        scores = {str(row["symbol"]): float(row.get("score") or 0.0) for row in rows}
        return StrategyResult(
            as_of=as_of,
            strategy_id=strategy_id,
            rows=rows,
            total=len(rows),
            elapsed_ms=(time.perf_counter() - started_at) * 1000,
            scores=scores,
            entry_signal_hits=entry_signal_hits,
            exit_signal_hits=exit_signal_hits,
        )

    def _run_composite_strategy(
        self,
        strategy_id: str,
        strategy: StrategyDef,
        context: StrategyDataContext,
        *,
        pool: list[str] | None = None,
        params: dict | None = None,
        overrides: dict | None = None,
        started_at: float,
    ) -> StrategyResult:
        """叠加策略: 调度各子策略(共享 context)→ 合并结果。

        复用 run_all 共享 current/history/market, 避免各子策略重复加载数据。
        子策略必须已在加载期通过两阶段引用校验(存在/非嵌套/asset_types 一致)。
        """
        from app.strategy import composite as composite_mod

        assert strategy.composite is not None
        overrides = overrides or {}

        # 权重: override.children 优先(META 固化值的轻量覆盖), 否则用 META 声明。
        override_children = overrides.get("children")
        if isinstance(override_children, list) and override_children:
            spec = _parse_composite_children(override_children)
            children = spec.children
        else:
            children = strategy.composite.children

        child_ids = [c.strategy_id for c in children]
        child_weights = [c.weight for c in children]
        merge_mode = str(params.get("merge_mode") or "union")
        min_confirm = int(params.get("min_confirm") or 0)

        # 子策略 override: 先加载各自保存的用户配置(参数/评分/信号等),
        # 再叠加 composite 统一的 basic_filter(计划 §3.3, 保证候选池一致)。
        # 这样 composite 内跑子策略与单独跑子策略使用同一口径。
        shared_basic_filter = overrides.get("basic_filter")
        overrides_map: dict[str, dict] = {}
        for cid in child_ids:
            child_override: dict = {}
            if self._override_loader is not None:
                try:
                    loaded = self._override_loader(cid)
                    if isinstance(loaded, dict):
                        child_override = dict(loaded)
                except Exception:  # noqa: BLE001
                    pass
            if shared_basic_filter:
                child_override["basic_filter"] = shared_basic_filter
            overrides_map[cid] = child_override

        # 共享 context 跑所有子策略。run_all 内部对 matrix_native 子策略会
        # 合并 field_columns 构建超集矩阵, 一次加载。
        child_results = self.run_all(
            context,
            params_map={},
            overrides_map=overrides_map,
            strategy_ids=child_ids,
            # 嵌套调用串行: 父级 worker 已并发, 子级再开池会使线程总数随叠加
            # 层数放大 (4×4×...), 超出并发闸与核数的合理范围。
            parallel=False,
        )
        ordered_results = [child_results[cid] for cid in child_ids]

        merged = composite_mod.merge_results(
            ordered_results,
            child_weights,
            merge_mode,
            min_confirm,
            as_of=context.as_of,
            strategy_id=strategy_id,
        )

        # 构造展示行: 按 symbol 从各子结果取首个命中的行(含 name/价格等展示字段),
        # 融合 score。子策略间 schema 可能不同, 保留首个命中子的字段即可。
        row_by_symbol: dict[str, dict] = {}
        for res in ordered_results:
            for row in res.rows:
                sym = str(row.get("symbol"))
                if sym and sym not in row_by_symbol and sym in merged.scores:
                    row_by_symbol[sym] = row

        order_desc = bool(strategy.meta.get("descending", True))
        ranked_symbols = sorted(
            merged.scores.keys(),
            key=lambda s: merged.scores[s],
            reverse=order_desc,
        )
        limit = self._result_limit(strategy, overrides)
        if limit is not None:
            ranked_symbols = ranked_symbols[:limit]

        rows = _sanitize([
            {**row_by_symbol[sym], "score": merged.scores[sym]}
            for sym in ranked_symbols
            if sym in row_by_symbol
        ])
        scores = {str(row["symbol"]): float(row.get("score") or 0.0) for row in rows}

        return StrategyResult(
            as_of=context.as_of,
            strategy_id=strategy_id,
            rows=rows,
            total=len(rows),
            elapsed_ms=(time.perf_counter() - started_at) * 1000,
            scores=scores,
        )

    @staticmethod
    def _matrix_signal_hits(
        active: np.ndarray,
        codes: np.ndarray,
        signal_ids: tuple[str, ...],
        symbols: tuple[str, ...],
    ) -> list[dict]:
        hits = []
        for asset_id in np.flatnonzero(active != 0):
            code = int(codes[int(asset_id)])
            signals = [signal_ids[code]] if 0 <= code < len(signal_ids) else []
            hits.append({"symbol": symbols[int(asset_id)], "signals": signals})
        return hits

    @staticmethod
    def _matrix_target_frame(panel: pl.DataFrame, as_of: date) -> pl.DataFrame:
        if "datetime" in panel.columns:
            target = panel.filter(pl.col("datetime").cast(pl.Date) == as_of)
            if target.is_empty():
                return target
            latest = target["datetime"].max()
            target = target.filter(pl.col("datetime") == latest)
        elif "date" in panel.columns:
            target = panel.filter(pl.col("date") == as_of)
        else:
            return panel.head(0)
        return target.unique(subset=["symbol"], keep="last")

    # ================================================================
    # 内部: 基础过滤
    # ================================================================

    @staticmethod
    def _basic_filter_expr(df: pl.DataFrame, bf: dict) -> pl.Expr | None:
        """构建基础过滤表达式。回测可复用为买入候选 mask，不删除行情行。"""
        exprs: list[pl.Expr] = []
        if bf.get("price_min") is not None:
            exprs.append(pl.col("close") >= bf["price_min"])
        if bf.get("price_max") is not None:
            exprs.append(pl.col("close") <= bf["price_max"])
        if bf.get("market_cap_min") is not None and "total_shares" in df.columns:
            exprs.append(
                pl.col("close") * pl.col("total_shares") >= bf["market_cap_min"]
            )
        if bf.get("market_cap_max") is not None and "total_shares" in df.columns:
            exprs.append(
                pl.col("close") * pl.col("total_shares") <= bf["market_cap_max"]
            )
        # 流通市值
        if bf.get("float_cap_min") is not None and "float_shares" in df.columns:
            exprs.append(
                pl.col("close") * pl.col("float_shares") >= bf["float_cap_min"]
            )
        if bf.get("float_cap_max") is not None and "float_shares" in df.columns:
            exprs.append(
                pl.col("close") * pl.col("float_shares") <= bf["float_cap_max"]
            )
        if bf.get("amount_min") is not None:
            exprs.append(pl.col("amount") >= bf["amount_min"])
        if bf.get("amount_max") is not None:
            exprs.append(pl.col("amount") <= bf["amount_max"])
        # 换手率
        if bf.get("turnover_min") is not None and "turnover_rate" in df.columns:
            exprs.append(pl.col("turnover_rate") >= bf["turnover_min"])
        if bf.get("turnover_max") is not None and "turnover_rate" in df.columns:
            exprs.append(pl.col("turnover_rate") <= bf["turnover_max"])
        if bf.get("exclude_st") and "name" in df.columns:
            exprs.append(~pl.col("name").str.contains("(?i)ST|\\*ST|退"))
        # 板块过滤
        boards = bf.get("boards")
        if boards and isinstance(boards, list) and len(boards) > 0:
            board_exprs: list[pl.Expr] = []
            for b in boards:
                if b == "沪主板":
                    board_exprs.append(pl.col("symbol").str.starts_with("60"))
                elif b == "深主板":
                    board_exprs.append(
                        pl.col("symbol").str.starts_with("00")
                        | pl.col("symbol").str.starts_with("001")
                    )
                elif b == "创业板":
                    board_exprs.append(
                        pl.col("symbol").str.starts_with("300")
                        | pl.col("symbol").str.starts_with("301")
                    )
                elif b == "科创板":
                    board_exprs.append(pl.col("symbol").str.starts_with("688"))
                elif b == "北交所":
                    board_exprs.append(pl.col("symbol").str.contains(r"\.BJ$"))
            if board_exprs:
                exprs.append(pl.any_horizontal(board_exprs))
        if exprs:
            return pl.all_horizontal(exprs)
        return None

    @staticmethod
    def _apply_basic_filter(df: pl.DataFrame, bf: dict) -> pl.DataFrame:
        """Stage 1: 基础参数过滤"""
        expr = StrategyEngine._basic_filter_expr(df, bf)
        if expr is not None:
            return df.filter(expr)
        return df

    # 分钟策略命中行需要从事后联表补齐的 enriched 列: 基础过滤引用 + 前端展示。
    # close 不在列 — 分钟策略输出的 close 是最后一根分钟K收盘价, 优先于日线快照。
    MINUTE_JOIN_COLUMNS: tuple[str, ...] = (
        "name", "total_shares", "float_shares", "amount",
        "turnover_rate", "change_pct", "pre_close",
    )

    def _user_data_dir(self) -> Path | None:
        """从策略目录推导 data_dir(…/strategies/custom → data_dir)。推不出则跳过注入。"""
        for d in self._strategy_dirs:
            if d.name == "custom" and d.parent.name == "strategies":
                return d.parent.parent
        return None

    def _inject_intraday_signal_columns(self, minute_df: pl.DataFrame) -> pl.DataFrame:
        """向当日分钟K帧注入自定义盘中信号列(csgi_, 当日条件上升沿)。

        单点注入: 实盘分钟扫描与分钟回测 worker 共用本方法, 特征计算与
        监控评估同源(intraday_features), 保证口径一致。无定义/帧为空时原样返回。
        """
        if minute_df is None or minute_df.is_empty() or "datetime" not in minute_df.columns:
            return minute_df
        data_dir = self._user_data_dir()
        if data_dir is None:
            return minute_df
        try:
            from app.strategy import custom_signals
            from app.strategy.intraday_features import build_feature_frame

            definitions = custom_signals.load_intraday_all(data_dir)
            if not definitions:
                return minute_df
            exprs = custom_signals.build_intraday_expressions(definitions)
            if not exprs:
                return minute_df
            frame = build_feature_frame(minute_df)
            if frame.is_empty():
                return minute_df
            evaluated = custom_signals.apply_intraday_edges(frame, exprs).select(
                ["symbol", "datetime", *exprs.keys()]
            )
            return minute_df.join(evaluated, on=["symbol", "datetime"], how="left").with_columns(
                [pl.col(name).fill_null(False).cast(pl.Boolean).alias(name) for name in exprs]
            )
        except Exception as e:
            # 注入失败不阻断策略执行: 未注入列会由 required_features 校验兜底报错
            logger.warning("intraday signal inject failed: %s", e)
            return minute_df

    @staticmethod
    def _join_basic_columns(df: pl.DataFrame, current: pl.DataFrame) -> pl.DataFrame:
        """把 enriched 快照列按 symbol 联到分钟策略输出上, 只补 df 缺失的列。"""
        cols = [
            c for c in StrategyEngine.MINUTE_JOIN_COLUMNS
            if c in current.columns and c not in df.columns
        ]
        if not cols:
            return df
        extra = current.select(["symbol", *cols]).unique(subset=["symbol"], keep="last")
        return df.join(extra, on="symbol", how="left")

    # ================================================================
    # 内部: 评分
    # ================================================================

    @staticmethod
    def _apply_scoring(
        df: pl.DataFrame,
        weights: dict,
        directions: Mapping[str, str] | None = None,
    ) -> pl.DataFrame:
        """通用评分: min-max 归一化 → 加权求和 → 0~100 分"""
        if not weights:
            return df

        executable = [
            (str(col), value, weight)
            for col, weight in weights.items()
            if weight and (value := scoring_value_expr(df.columns, str(col))) is not None
        ]
        total_weight = sum(weight for _, _, weight in executable)
        if total_weight <= 0:
            return df

        score_parts: list[pl.Expr] = []
        for name, value, weight in executable:
            w = weight / total_weight
            col_min = value.min()
            col_range = value.max() - col_min
            normalized = pl.when(col_range > 0).then(
                (value - col_min) / col_range
            ).otherwise(pl.lit(0.5))
            if (directions or {}).get(name) == SCORING_DIRECTION_LOW:
                normalized = 1.0 - normalized
            score_parts.append(normalized * w)

        if not score_parts:
            return df

        score_expr = score_parts[0]
        for part in score_parts[1:]:
            score_expr = score_expr + part
        return df.with_columns((score_expr * 100).alias("score"))

    @staticmethod
    def _materialize_scoring_frames(
        current: pl.DataFrame | None,
        history: pl.DataFrame | None,
        scoring: Mapping[str, Any],
    ) -> tuple[pl.DataFrame | None, pl.DataFrame | None]:
        names = [str(name) for name, weight in scoring.items() if weight]
        if not names:
            return current, history
        if history is None or history.is_empty():
            return (
                materialize_scoring_columns(current, names) if current is not None else None,
                history,
            )

        scored_history = materialize_scoring_columns(history, names)
        if current is None or current.is_empty():
            return current, scored_history
        join_keys = [key for key in ("symbol", "date", "datetime") if key in current.columns and key in scored_history.columns]
        added = [name for name in names if name not in current.columns and name in scored_history.columns]
        if not join_keys or not added:
            return materialize_scoring_columns(current, names), scored_history
        values = scored_history.select([*join_keys, *added]).unique(subset=join_keys, keep="last")
        return current.join(values, on=join_keys, how="left"), scored_history


def _sanitize(rows: list[dict]) -> list[dict]:
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and (v != v or abs(v) == float("inf")):
                r[k] = None
    return rows
