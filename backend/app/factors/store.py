"""自定义/复合因子存储 (P3) — data/user_data/custom_factors/*.json。

持久化收口到统一契约 Store (app/definitions.py); 单文件损坏只禁用该因子并
告警, 不影响启动 (对齐 CONTRIBUTING 第 4 节插件隔离要求)。生命周期状态:
draft → active → watch → retired (P4 状态机, 存储字段就绪, 迁移逻辑见巡检设计)。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from app.definitions import DefinitionKind, JsonDefinitionStore, register_kind
from app.factors.dsl import compile_formula
from app.factors.py_expr import compile_python_expression
from app.factors.registry import FactorSpec, factor_dependencies, get_factor, register_factor

logger = logging.getLogger(__name__)

CUSTOM_ID_PATTERN = re.compile(r"^uf_[a-z0-9_]{1,40}$")
COMPOSITE_ID_PATTERN = re.compile(r"^cf_[a-z0-9_]{1,40}$")
MAX_COMPOSITE_MEMBERS = 8
STATUSES = frozenset({"draft", "active", "watch", "retired"})

_STORE = JsonDefinitionStore("user_data/custom_factors", label="custom factor")


def load_all(data_dir: Path) -> list[dict]:
    """读取全部自定义/复合因子定义; 损坏文件跳过。"""
    return _STORE.load_all(data_dir)


def save_one(data_dir: Path, definition: dict) -> None:
    _STORE.save_one(data_dir, definition)


def delete_one(data_dir: Path, factor_id: str) -> bool:
    return _STORE.delete_one(data_dir, factor_id)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def to_spec(definition: dict) -> FactorSpec:
    """定义 → FactorSpec; 校验失败抛 ValueError (调用方 fail-closed)。

    custom: 依赖/预热由 DSL 编译推导 (编译失败即拒绝注册)。
    composite: 依赖 = 成员递归展开; 预热 = 成员最大值; 循环引用拒绝。
    """
    kind = str(definition.get("kind", "custom"))
    factor_id = str(definition.get("id", ""))
    label = str(definition.get("label", "")).strip()
    if not label:
        raise ValueError("label 不能为空")
    pattern = COMPOSITE_ID_PATTERN if kind == "composite" else CUSTOM_ID_PATTERN
    if not pattern.match(factor_id):
        raise ValueError(f"id 必须匹配 {pattern.pattern}")
    status = str(definition.get("status", "draft"))
    if status not in STATUSES:
        raise ValueError(f"status 必须是 {sorted(STATUSES)} 之一")

    if kind == "custom":
        formula = str(definition.get("formula", ""))
        lang = str(definition.get("lang", "dsl")).strip().lower()
        if lang not in ("dsl", "python"):
            raise ValueError(f"未知 lang: {lang} (可选 dsl / python)")
        if lang == "python":
            # T2 表达式级 Python (P3): 依赖由 ctx 构造期登记; 显式 warmup
            # 只允许增大 (推断值是安全下限, 少算会失真)。
            compiled = compile_python_expression(formula)
            if not compiled.ok:
                first = compiled.errors[0]
                raise ValueError(f"表达式无效 [{first.code}]: {first.message}")
            declared_warmup = int(definition.get("warmup_bars", 0) or 0)
            return FactorSpec(
                id=factor_id,
                label=label,
                group=str(definition.get("group", "自定义")),
                formula_text=formula,
                kind="custom",
                lang="python",
                version=int(definition.get("version", 1)),
                dependencies=compiled.dependencies,
                warmup_bars=max(declared_warmup, compiled.warmup_bars),
                direction=str(definition.get("direction", "none")),  # type: ignore[arg-type]
                stability="stable" if status == "active" else "experimental",
            )
        compiled = compile_formula(formula)
        if not compiled.ok:
            first = compiled.errors[0]
            raise ValueError(f"公式无效 [{first.code}]: {first.message}")
        return FactorSpec(
            id=factor_id,
            label=label,
            group=str(definition.get("group", "自定义")),
            formula_text=formula,
            kind="custom",
            version=int(definition.get("version", 1)),
            dependencies=frozenset(compiled.dependencies),
            warmup_bars=compiled.warmup_bars,
            direction=str(definition.get("direction", "none")),  # type: ignore[arg-type]
            stability="stable" if status == "active" else "experimental",
        )

    if kind != "composite":
        raise ValueError(f"未知 kind: {kind}")
    members_raw = definition.get("members")
    if not isinstance(members_raw, dict) or not (2 <= len(members_raw) <= MAX_COMPOSITE_MEMBERS):
        raise ValueError(f"composite 成员必须是 {2}~{MAX_COMPOSITE_MEMBERS} 个")
    from app.factors.dsl import BASE_COLUMNS

    components: list[tuple[str, float]] = []
    for member_id, weight in members_raw.items():
        member_id = str(member_id)
        if member_id == factor_id:
            raise ValueError("composite 不能引用自身")
        try:
            weight = float(weight)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"成员 {member_id} 权重必须是数字") from exc
        if not weight:
            raise ValueError(f"成员 {member_id} 权重不能为 0")
        # 成员 = 注册表因子 或 enriched 基准列 (已物化, 可直接参与组合)
        if get_factor(member_id) is None and member_id not in BASE_COLUMNS:
            raise ValueError(f"未知成员因子: {member_id}")
        components.append((member_id, weight))
    # 环检测沿 components 链走 (依赖已展开, 看不到链路成员)
    seen = {factor_id}
    frontier = [member_id for member_id, _ in components]
    while frontier:
        current = frontier.pop()
        if current in seen:
            raise ValueError("composite 成员存在循环引用")
        seen.add(current)
        current_spec = get_factor(current)
        if current_spec is not None and current_spec.kind == "composite":
            frontier.extend(member_id for member_id, _ in current_spec.components)
    dependencies = factor_dependencies([member_id for member_id, _ in components])
    warmup = max(
        ((get_factor(member_id).warmup_bars if get_factor(member_id) else 1) for member_id, _ in components),
        default=1,
    )
    formula_text = " + ".join(
        f"{weight:g}*zscore({member_id})" for member_id, weight in components
    )
    return FactorSpec(
        id=factor_id,
        label=label,
        group=str(definition.get("group", "组合")),
        formula_text=formula_text,
        kind="composite",
        version=int(definition.get("version", 1)),
        dependencies=dependencies,
        warmup_bars=warmup,
        direction=str(definition.get("direction", "none")),  # type: ignore[arg-type]
        components=tuple(components),
        stability="stable" if status == "active" else "experimental",
    )


def register_definition(definition: dict) -> FactorSpec:
    """定义 → spec → 注册 (重复 id 版本未升时由注册表拒绝)。"""
    spec = to_spec(definition)
    register_factor(spec)
    return spec


def load_into_registry(data_dir: Path) -> list[str]:
    """启动期把存储中的因子注册进注册表; 单个失败只跳过并告警。

    多轮加载: composite 成员可能引用尚未加载的 custom/其他 composite (文件按
    字母序加载, cf_* 先于 uf_*), 失败的 composite 延后重试, 覆盖链式引用;
    重试用尽仍失败的只告警不阻塞启动。
    """
    loaded: list[str] = []
    pending = list(load_all(data_dir))
    for round_index in range(3):
        deferred: list[dict] = []
        for definition in pending:
            try:
                register_definition(definition)
                loaded.append(str(definition["id"]))
            except ValueError as exc:
                if round_index < 2 and str(definition.get("kind")) == "composite":
                    deferred.append(definition)
                else:
                    logger.warning("custom factor 注册失败 %s: %s", definition.get("id"), exc)
            except Exception as exc:
                logger.warning("custom factor 注册失败 %s: %s", definition.get("id"), exc)
        if not deferred:
            break
        pending = deferred
    return loaded


# ── 统一定义契约注册 (P1b) ───────────────────────────────
_FACTOR_ID_PATTERN = re.compile(r"^(uf|cf)_[a-z0-9_]{1,40}$")


def _validate_definition(definition: dict) -> None:
    """统一契约校验器 = to_spec (id/label/DSL/复合成员全量校验, 非法抛 ValueError)。"""
    to_spec(definition)


def _definition_dependencies(definition: dict) -> frozenset[str]:
    """因子定义 → 输入列 (统一契约的依赖提取器) = DSL/复合成员编译依赖, 与 to_spec 同源。"""
    return to_spec(definition).dependencies


register_kind(DefinitionKind(
    kind="factor",
    store=_STORE,
    id_pattern=_FACTOR_ID_PATTERN,
    validate=_validate_definition,
    dependencies=_definition_dependencies,
))
