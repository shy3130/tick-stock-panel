"""策略引擎 — 加载、执行、评分。

职责: 从文件系统加载策略 Python 模块，执行两阶段过滤(基础+策略)，
     通用评分排序。
不知道: AI、API、前端、配置持久化、回测。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

import polars as pl

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


def canonical_def_hash(payload: Any) -> str:
    """规范化 JSON（sorted keys、紧凑分隔符）→ sha256 前 12 位。

    composite 策略与 csg_ 自定义信号的定义指纹共用此口径：同一份定义
    无论键序/子项声明顺序如何书写，指纹一致；定义内容变化则指纹变化。
    """
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _file_content_hash(path: Path) -> str:
    """文件型策略（builtin/custom/ai 目录 .py）→ 文件内容 sha256 前 12 位。

    读取失败（文件被移走/权限）返回空串：指纹未知时不参与版本比对，
    不伪造一个"看起来有效"的值（fail-closed）。
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError as e:
        logger.warning("strategy def hash: cannot read %s: %s", path, e)
        return ""


def _parse_composite_children(raw: Any) -> "CompositeSpec":
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
    alerts: list[dict]
    filter_fn: Callable[[pl.DataFrame, dict], pl.Expr] | None
    filter_history_fn: Callable[[pl.DataFrame, dict], pl.DataFrame] | None
    lookback_days: int
    source: str  # "builtin" | "custom" | "ai" | "composite"
    file_path: Path | None = None
    execution_backend: str = "polars_expr"
    composite: CompositeSpec | None = None  # 仅 backend=="composite" 时非空
    ephemeral: bool = False  # 寻优临时组合, 不写盘、不进 list_strategies
    # F13 定义指纹 (sha256 前 12 位): 文件型策略取文件内容; composite (含寻优
    # 临时组合) 取规范化 JSON。回测 Run 持久化该值, 前端与当前列表比对提示
    # 「策略定义已变更」。空串 = 指纹未知 (无文件也无 composite 声明)。
    def_hash: str = ""

    def __post_init__(self) -> None:
        # 调用方显式传入的非空 def_hash 优先; 未传时按定义来源推导。
        if self.def_hash:
            return
        if self.execution_backend == "composite" and self.composite is not None:
            self.def_hash = canonical_def_hash({
                "children": [
                    {"strategy_id": child.strategy_id, "weight": child.weight}
                    for child in sorted(self.composite.children, key=lambda c: c.strategy_id)
                ],
            })
        elif self.file_path is not None:
            self.def_hash = _file_content_hash(self.file_path)


@dataclass
class StrategyResult:
    """策略执行结果"""
    as_of: date
    strategy_id: str
    rows: list[dict] = field(default_factory=list)
    total: int = 0
    elapsed_ms: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)


class StrategyEngine:
    """策略引擎 — 策略加载 + 执行 + 评分"""

    def __init__(self, enriched_loader: Callable[[date], pl.DataFrame],
                 enriched_history_loader: Callable[[date, int], pl.DataFrame] | None = None,
                 strategy_dirs: list[Path] | None = None,
                 *,
                 override_loader: Callable[[str], dict] | None = None):
        """
        Args:
            enriched_loader: (date) -> pl.DataFrame, 加载指定日期的 enriched 数据
            strategy_dirs:   策略文件搜索目录列表
            override_loader: 可选的 override 加载器: 叠加策略执行时用它查子策略的
                用户覆盖配置, 保证 composite 内跑子策略与单独跑子策略使用同一口径。
                None 时(测试/无 data_dir) 子策略用默认参数, 不报错。
        """
        self._loader = enriched_loader
        self._history_loader = enriched_history_loader
        self._strategies: dict[str, StrategyDef] = {}
        self._strategy_dirs = strategy_dirs or []
        self._load_errors: list[dict] = []  # 加载失败的策略 [{file, error}]
        self._override_loader = override_loader
        self._load_all()

    # ================================================================
    # 加载
    # ================================================================

    def _load_all(self) -> None:
        candidates: dict[str, StrategyDef] = {}
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
                        previous = candidates.pop(strategy_id)
                        duplicate_ids.add(strategy_id)
                        errors.extend([
                            {"file": str(previous.file_path or f), "error": f"duplicate strategy id {strategy_id!r}"},
                            {"file": str(f), "error": f"duplicate strategy id {strategy_id!r}"},
                        ])
                        continue
                    candidates[strategy_id] = s
                except Exception as e:
                    logger.warning("load strategy %s failed: %s", f.name, e)
                    errors.append({"file": str(f), "error": str(e)})

        # 第二阶段: 校验 composite 引用合法性。
        # _load_file 加载单个文件时无法判断 child 是否存在; 此处已拿到全部
        # candidates, 可做引用、嵌套、asset_types 与数量校验。孤儿 composite
        # (引用不合法) 被移出 candidates 并记入 errors, 但不触发整体 reload
        # 失败 —— 不波及其他正常策略(插件隔离原则)。
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

        self._load_errors = errors
        self._strategies = candidates
        for strategy_id, strategy in candidates.items():
            logger.debug("loaded strategy: %s (%s)", strategy_id, strategy.source)

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
        - child 的 asset_types 必须完全覆盖父 composite 的 asset_types(子集关系)
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
        return None

    @staticmethod
    def _load_file(path: Path) -> StrategyDef:
        """从 Python 文件加载策略定义"""
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot load module from {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        meta = getattr(mod, "META", {})
        meta.setdefault("id", path.stem)
        meta.setdefault("name", path.stem)
        meta.setdefault("description", "")
        meta.setdefault("tags", [])
        meta.setdefault("params", [])
        meta.setdefault("scoring", {})
        meta.setdefault("order_by", "score")
        meta.setdefault("descending", True)
        meta.setdefault("limit", 100)

        # 合并默认基础过滤
        bf = {**DEFAULT_BASIC_FILTER}
        strat_bf = getattr(mod, "BASIC_FILTER", None)
        if strat_bf:
            bf.update(strat_bf)
        # meta 里的 basic_filter 也合并（优先级最高）
        meta_bf = meta.get("basic_filter")
        if meta_bf:
            bf.update(meta_bf)

        # 执行后端: 默认 polars_expr; composite 为声明式引用子策略。
        execution_backend = str(
            getattr(mod, "EXECUTION_BACKEND", meta.get("execution_backend", "polars_expr"))
        )
        filter_fn = getattr(mod, "filter", None)
        filter_history_fn = getattr(mod, "filter_history", None)

        composite_spec: CompositeSpec | None = None
        if execution_backend == "composite":
            # composite 仅通过 META.children 声明引用, 不得携带业务 filter。
            if filter_fn is not None or filter_history_fn is not None:
                raise ValueError(
                    "composite strategy must not declare filter/filter_history"
                )
            composite_spec = _parse_composite_children(meta.get("children"))

        source = "custom"
        if execution_backend == "composite":
            source = "composite"
        elif "builtin" in str(path).replace("\\", "/"):
            source = "builtin"
        elif "/ai/" in str(path).replace("\\", "/") or "\\ai\\" in str(path):
            source = "ai"

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
            lookback_days=int(getattr(mod, "LOOKBACK_DAYS", meta.get("lookback_days", 1)) or 1),
            source=source,
            file_path=path,
            execution_backend=execution_backend,
            composite=composite_spec,
        )

    def reload(self) -> None:
        """热重载所有策略"""
        self._load_all()

    # ================================================================
    # 查询
    # ================================================================

    def list_strategies(self) -> list[dict]:
        """返回所有策略的元信息"""
        result = []
        for s in self._strategies.values():
            if getattr(s, "ephemeral", False):
                continue
            result.append({**s.meta, "source": s.source})
        return result

    def get(self, strategy_id: str) -> StrategyDef:
        s = self._strategies.get(strategy_id)
        if not s:
            raise ValueError(f"unknown strategy: {strategy_id}")
        return s

    def has(self, strategy_id: str) -> bool:
        return strategy_id in self._strategies

    def put_ephemeral(self, strategy_id: str, strategy: StrategyDef) -> None:
        """注册不落盘的临时组合策略, 仅供寻优回测 get()。"""
        if not strategy_id.startswith("combo:"):
            raise ValueError("ephemeral id must start with combo:")
        if not getattr(strategy, "ephemeral", False):
            raise ValueError("strategy.ephemeral must be True")
        if strategy.file_path is not None:
            raise ValueError("ephemeral strategy must not have file_path")
        existing = self._strategies.get(strategy_id)
        if existing is not None and not getattr(existing, "ephemeral", False):
            raise ValueError("cannot overwrite persistent strategy")
        if strategy.execution_backend != "composite" or strategy.composite is None:
            raise ValueError("ephemeral optimizer entries must be composites")
        error = self._validate_composite_references(strategy_id, strategy, self._strategies)
        if error is not None:
            raise ValueError(error)
        self._strategies[strategy_id] = strategy

    def find_dependents(self, strategy_id: str) -> list[str]:
        """返回引用了 strategy_id 作为子策略的所有 composite 策略 id。

        供删除校验使用: 删除被引用的子策略会令 composite 加载失败,
        删除前应阻止(fail-closed)或提示用户先解除引用。
        """
        dependents: list[str] = []
        for sid, strategy in self._strategies.items():
            if strategy.execution_backend != "composite" or strategy.composite is None:
                continue
            if any(c.strategy_id == strategy_id for c in strategy.composite.children):
                dependents.append(sid)
        return dependents

    # ================================================================
    # 执行
    # ================================================================

    def run(
        self,
        strategy_id: str,
        as_of: date,
        pool: list[str] | None = None,
        params: dict | None = None,
        overrides: dict | None = None,
        precomputed: pl.DataFrame | None = None,
        precomputed_history: pl.DataFrame | None = None,
    ) -> StrategyResult:
        """执行策略: 基础过滤 → 策略过滤 → 评分排序

        Args:
            strategy_id:        策略 ID
            as_of:              选股日期
            pool:               限定股票池
            params:             策略参数 (用户在设置面板调的值)
            overrides:          用户覆盖配置 (basic_filter/scoring/stop_loss 等)
            precomputed:        已加载的 enriched 数据 (run_all 场景复用)
            precomputed_history: 已加载的历史窗口数据 (run_all 场景复用)
        """
        t0 = time.perf_counter()

        s = self.get(strategy_id)
        params = params or {}
        overrides = overrides or {}
        # 叠加策略: 调度各子策略后合并(复用现有 run 链路, 共享数据加载)。
        if s.execution_backend == "composite":
            return self._run_composite_strategy(
                strategy_id, s, as_of,
                pool=pool, params=params, overrides=overrides,
                precomputed=precomputed, precomputed_history=precomputed_history,
            )

        # 加载数据。普通策略只读目标日期；声明 filter_history 的策略读取历史窗口。
        if s.filter_history_fn:
            if precomputed_history is not None and not precomputed_history.is_empty():
                df = precomputed_history
            elif self._history_loader:
                df = self._history_loader(as_of, max(1, s.lookback_days))
            else:
                logger.warning("strategy %s requires history loader", strategy_id)
                return StrategyResult(as_of=as_of, strategy_id=strategy_id)
            if df.is_empty():
                return StrategyResult(as_of=as_of, strategy_id=strategy_id)
            df = s.filter_history_fn(df, params)
            if df.is_empty():
                return StrategyResult(as_of=as_of, strategy_id=strategy_id)
            if "date" in df.columns:
                df = df.filter(pl.col("date") == as_of)
        elif precomputed is not None and not precomputed.is_empty():
            df = precomputed
        else:
            df = self._loader(as_of)
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
        limit = s.meta.get("limit", 100)
        order_desc = s.meta.get("descending", True)
        if "score" in df.columns:
            df = df.sort("score", descending=order_desc)
        elif s.meta.get("order_by") and s.meta["order_by"] != "score":
            ob = s.meta["order_by"]
            if ob in df.columns:
                df = df.sort(ob, descending=order_desc)
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

    def run_all(self, as_of: date, params_map: dict | None = None,
                overrides_map: dict | None = None) -> dict[str, StrategyResult]:
        """批量执行所有策略 (enriched 只加载一次，基础过滤按策略分组缓存，历史数据共享)"""
        df = self._loader(as_of)
        params_map = params_map or {}
        overrides_map = overrides_map or {}

        # 历史策略: 找最大 lookback，一次加载共享
        history_strats = [(sid, s) for sid, s in self._strategies.items() if s.filter_history_fn]
        if history_strats and self._history_loader:
            max_lookback = max(s.lookback_days for _, s in history_strats)
            shared_history = self._history_loader(as_of, max(1, max_lookback))
        else:
            shared_history = None

        # 按 basic_filter hash 分组，避免重复过滤
        bf_cache: dict[str, pl.DataFrame] = {}
        results: dict[str, StrategyResult] = {}

        for sid, strat in self._strategies.items():
            try:
                bf_key = _dict_hash(strat.basic_filter)
                if bf_key not in bf_cache:
                    if strat.basic_filter.get("enabled", True):
                        bf_cache[bf_key] = self._apply_basic_filter(df, strat.basic_filter)
                    else:
                        bf_cache[bf_key] = df
                base = bf_cache[bf_key]

                # composite 传原始 enriched, 各子策略各自应用 basic_filter;
                # 普通策略传已按自身 basic_filter 过滤的 base。
                precomputed = df if strat.execution_backend == "composite" else base
                # 从已过滤的 base 执行 (filter_history 策略使用共享历史)
                results[sid] = self.run(
                    sid, as_of,
                    params=params_map.get(sid),
                    overrides=overrides_map.get(sid),
                    precomputed=precomputed,
                    precomputed_history=shared_history,
                )
            except Exception as e:
                logger.warning("run strategy %s failed: %s", sid, e)

        return results

    def _run_composite_strategy(
        self,
        strategy_id: str,
        strategy: StrategyDef,
        as_of: date,
        *,
        pool: list[str] | None = None,
        params: dict | None = None,
        overrides: dict | None = None,
        precomputed: pl.DataFrame | None = None,
        precomputed_history: pl.DataFrame | None = None,
    ) -> StrategyResult:
        """叠加策略选股: 调度各子策略(共享数据)→ 合并结果。

        复用 run 链路: 各子策略与单独跑使用同一过滤/评分口径。子策略 override
        先加载各自保存的用户配置, 再叠加 composite 统一的 basic_filter(保证候选池一致)。
        子策略必须已在加载期通过两阶段引用校验(存在/非嵌套/asset_types 一致)。
        """
        from app.strategy import composite as composite_mod

        assert strategy.composite is not None
        t0 = time.perf_counter()
        overrides = overrides or {}
        params = params or {}

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

        # 加载基础数据一次: 普通子策略用 precomputed(raw enriched), 历史子策略
        # 用 precomputed_history。若调用方未提供则按需自行加载。
        if precomputed is not None and not precomputed.is_empty():
            base_df = precomputed
        else:
            base_df = self._loader(as_of)

        shared_history = precomputed_history
        history_children = [
            cid for cid in child_ids
            if self.get(cid).filter_history_fn is not None
        ]
        if history_children and (shared_history is None or shared_history.is_empty()):
            if self._history_loader is not None:
                max_lb = max(self.get(cid).lookback_days for cid in history_children)
                shared_history = self._history_loader(as_of, max(1, max_lb))

        # Composite 的基础过滤是其候选池契约：默认声明先应用，再由运行期
        # override 局部覆盖；随后完整传给每个 child，防止 child 自己的默认
        # basic filter 悄悄改变 composite 的并集/交集语义。
        shared_basic_filter = dict(strategy.basic_filter)
        if overrides.get("basic_filter"):
            shared_basic_filter.update(overrides["basic_filter"])

        ordered_results: list[StrategyResult] = []
        for cid in child_ids:
            child_override: dict = {}
            if self._override_loader is not None:
                try:
                    loaded = self._override_loader(cid)
                    if isinstance(loaded, dict):
                        child_override = dict(loaded)
                except Exception:  # noqa: BLE001
                    pass
            child_override["basic_filter"] = shared_basic_filter
            ordered_results.append(
                self.run(
                    cid, as_of,
                    pool=pool,
                    params={},
                    overrides=child_override,
                    precomputed=base_df,
                    precomputed_history=shared_history,
                )
            )

        elapsed_ms = (time.perf_counter() - t0) * 1000
        merged = composite_mod.merge_results(
            ordered_results,
            child_weights,
            merge_mode,
            min_confirm,
            as_of=as_of,
            strategy_id=strategy_id,
            elapsed_ms=elapsed_ms,
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
        limit = int(strategy.meta.get("limit", 100) or 100)
        ranked_symbols = ranked_symbols[:limit]

        rows = _sanitize([
            {**row_by_symbol[sym], "score": merged.scores[sym]}
            for sym in ranked_symbols
            if sym in row_by_symbol
        ])
        scores = {str(row["symbol"]): float(row.get("score") or 0.0) for row in rows}

        return StrategyResult(
            as_of=as_of,
            strategy_id=strategy_id,
            rows=rows,
            total=len(rows),
            elapsed_ms=elapsed_ms,
            scores=scores,
        )

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
        total_weight = sum(weights.values())
        if total_weight <= 0:
            return df

        score_parts: list[pl.Expr] = []
        for col, weight in weights.items():
            if col not in df.columns:
                continue
            w = weight / total_weight
            col_min = pl.col(col).min()
            col_range = pl.col(col).max() - col_min
            normalized = pl.when(col_range > 0).then(
                (pl.col(col) - col_min) / col_range
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


def _dict_hash(d: dict) -> str:
    """用于 basic_filter 分组缓存"""
    return str(sorted(d.items()))
