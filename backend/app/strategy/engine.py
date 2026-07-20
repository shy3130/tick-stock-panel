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
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Callable

import numpy as np
import polars as pl

from app.strategy.scoring import scoring_dependencies, scoring_value_expr

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


@dataclass
class StrategyDataContext:
    """一次策略调用所需的标准数据上下文。"""

    asset_type: str
    timeframe: str
    as_of: date
    current: pl.DataFrame | None = None
    history: pl.DataFrame | None = None
    market: Any | None = None
    cache_key: str | None = None


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
    alerts: list[dict]
    filter_fn: Callable[[pl.DataFrame, dict], pl.Expr] | None
    filter_history_fn: Callable[[pl.DataFrame, dict], pl.DataFrame] | None
    lookback_days: int
    source: str  # "builtin" | "custom" | "ai"
    required_features: frozenset[str] = field(default_factory=frozenset)
    file_path: Path | None = None
    execution_backend: str = "polars_expr"
    matrix_strategy: Any | None = None


@dataclass
class StrategyResult:
    """策略执行结果"""
    as_of: date
    strategy_id: str
    rows: list[dict] = field(default_factory=list)
    total: int = 0
    elapsed_ms: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)


@dataclass
class _RealtimeMatrixEntry:
    fingerprint: tuple[Any, ...]
    buffer: Any


class StrategyEngine:
    """策略引擎 — 策略加载 + 执行 + 评分"""

    _module_load_lock = threading.RLock()

    def __init__(self, strategy_dirs: list[Path] | None = None):
        self._strategies: dict[str, StrategyDef] = {}
        self._load_errors: list[dict] = []  # 加载失败的策略 [{file, error}]
        self._strategy_dirs = strategy_dirs or []
        self._realtime_matrices: dict[str, _RealtimeMatrixEntry] = {}
        self._realtime_matrix_lock = threading.RLock()
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

        source = "custom"
        normalized_path = str(path).replace("\\", "/")
        if "/builtin/" in normalized_path:
            source = "builtin"
        elif "/ai/" in normalized_path:
            source = "ai"

        if source == "builtin" and "asset_types" not in meta:
            raise ValueError("builtin strategy META must declare asset_types")
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
        valid_backends = {"polars_expr", "matrix_native", "python_history_legacy"}
        if execution_backend not in valid_backends:
            raise ValueError(
                f"unsupported execution backend {execution_backend!r}; "
                f"expected one of {sorted(valid_backends)}"
            )

        matrix_strategy = getattr(mod, "MATRIX_STRATEGY", None)
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
            alerts=getattr(mod, "ALERTS", []),
            filter_fn=filter_fn,
            filter_history_fn=filter_history_fn,
            required_features=frozenset(meta.get("required_features", []) or [])
            | frozenset(getattr(mod, "REQUIRED_FEATURES", []) or []),
            lookback_days=int(getattr(mod, "LOOKBACK_DAYS", meta.get("lookback_days", 1)) or 1),
            source=source,
            file_path=path,
            execution_backend=execution_backend,
            matrix_strategy=matrix_strategy,
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

    def list_strategies(self) -> list[dict]:
        """返回所有策略的元信息"""
        result = []
        for s in self._strategies.values():
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
            if strategy.execution_backend == "matrix_native":
                params = self.resolve_params(
                    strategy,
                    params_map.get(strategy_id),
                    overrides_map.get(strategy_id),
                )
                required = max(
                    required,
                    int(strategy.matrix_strategy.required_warmup_bars(params)) + 1,
                )
            elif strategy.filter_history_fn:
                required = max(required, int(strategy.lookback_days))
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
            )
            field_columns.update(
                self._matrix_field_columns(strategy, overrides_map.get(strategy_id))
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

        # 普通策略只读目标日期；历史策略读取调用方注入的历史窗口。
        if s.filter_history_fn:
            if context.history is None:
                raise ValueError(f"strategy {strategy_id} requires history data")
            df = context.history
            if df.is_empty():
                return StrategyResult(as_of=as_of, strategy_id=strategy_id)
            df = s.filter_history_fn(df, params)
            if df.is_empty():
                return StrategyResult(as_of=as_of, strategy_id=strategy_id)
            if "date" in df.columns:
                df = df.filter(pl.col("date") == as_of)
        else:
            if context.current is None:
                raise ValueError(f"strategy {strategy_id} requires current data")
            df = context.current
            if df.is_empty():
                return StrategyResult(as_of=as_of, strategy_id=strategy_id)

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

        # Stage 3: 评分
        scoring = s.meta.get("scoring", {})
        scoring_overrides = overrides.get("scoring")
        if scoring_overrides:
            scoring = {**scoring, **scoring_overrides}
        df = self._apply_scoring(df, scoring)

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
        )

    def run_all(
        self,
        context: StrategyDataContext,
        params_map: dict | None = None,
        overrides_map: dict | None = None,
        *,
        strategy_ids: list[str] | None = None,
    ) -> dict[str, StrategyResult]:
        """批量执行策略；当前数据、历史和矩阵均来自同一个调用上下文。"""
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
            if strategy.filter_history_fn or strategy.execution_backend == "matrix_native"
        ]
        shared_history = context.history
        if history_strats and shared_history is None:
            raise ValueError("selected strategies require history data")

        shared_matrix = context.market
        matrix_strats = [
            (sid, strategy)
            for sid, strategy in selected
            if strategy.execution_backend == "matrix_native"
        ]
        if (
            shared_matrix is None
            and matrix_strats
            and shared_history is not None
            and not shared_history.is_empty()
        ):
            from app.backtest.matrix import build_market_data_matrix

            field_columns: set[str] = set()
            for sid, strategy in matrix_strats:
                field_columns.update(
                    self._matrix_field_columns(strategy, overrides_map.get(sid))
                )
            shared_matrix = build_market_data_matrix(
                shared_history,
                field_columns=field_columns,
            )

        results: dict[str, StrategyResult] = {}

        for sid, _ in selected:
            results[sid] = self.run(
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

        return results

    @staticmethod
    def _matrix_field_columns(strategy: StrategyDef, overrides: dict | None = None) -> set[str]:
        fields = set(strategy.matrix_strategy.required_fields())
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
        scoring = dict(strategy.meta.get("scoring", {}) or {})
        scoring.update((overrides or {}).get("scoring") or {})
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
                field_columns=self._matrix_field_columns(strategy, overrides),
            )

        if source_panel is None or source_panel.is_empty():
            source_panel = context.current
        if source_panel is None or source_panel.is_empty():
            return StrategyResult(as_of=as_of, strategy_id=strategy_id)

        basic_filter = dict(strategy.basic_filter or {})
        if overrides.get("basic_filter"):
            basic_filter.update(overrides["basic_filter"])
        scoring = dict(strategy.meta.get("scoring", {}) or {})
        scoring.update(overrides.get("scoring") or {})
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
        selected_assets = np.flatnonzero(signals.entry[target_time] != 0)
        if selected_assets.size == 0:
            return StrategyResult(
                as_of=as_of,
                strategy_id=strategy_id,
                elapsed_ms=(time.perf_counter() - started_at) * 1000,
            )

        target_frame = self._matrix_target_frame(source_panel, as_of)
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
        )

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

    # ================================================================
    # 内部: 评分
    # ================================================================

    @staticmethod
    def _apply_scoring(df: pl.DataFrame, weights: dict) -> pl.DataFrame:
        """通用评分: min-max 归一化 → 加权求和 → 0~100 分"""
        if not weights:
            return df

        executable = [
            (value, weight)
            for col, weight in weights.items()
            if weight and (value := scoring_value_expr(df.columns, str(col))) is not None
        ]
        total_weight = sum(weight for _, weight in executable)
        if total_weight <= 0:
            return df

        score_parts: list[pl.Expr] = []
        for value, weight in executable:
            w = weight / total_weight
            col_min = value.min()
            col_range = value.max() - col_min
            normalized = pl.when(col_range > 0).then(
                (value - col_min) / col_range
            ).otherwise(pl.lit(0.5))
            score_parts.append(normalized * w)

        if not score_parts:
            return df

        score_expr = score_parts[0]
        for part in score_parts[1:]:
            score_expr = score_expr + part
        return df.with_columns((score_expr * 100).alias("score"))


def _sanitize(rows: list[dict]) -> list[dict]:
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and (v != v or abs(v) == float("inf")):
                r[k] = None
    return rows
