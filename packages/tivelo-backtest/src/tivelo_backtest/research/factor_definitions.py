"""Import existing v1 factor definitions without application storage or state."""
from __future__ import annotations

import re
from collections.abc import Iterable

from .catalog import (
    BUILTIN_CATALOG,
    FactorCatalog,
    FactorLookup,
    FactorSpec,
    factor_dependencies,
    factor_warmup_bars,
)
from .dsl import compile_formula
from .py_expr import compile_python_expression

CUSTOM_ID_PATTERN = re.compile(r"^uf_[a-z0-9_]{1,40}$")
COMPOSITE_ID_PATTERN = re.compile(r"^cf_[a-z0-9_]{1,40}$")
MAX_COMPOSITE_MEMBERS = 8
STATUSES = frozenset({"draft", "active", "watch", "retired"})

def to_spec(definition: dict, *, catalog: FactorLookup = BUILTIN_CATALOG) -> FactorSpec:
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
            compiled = compile_python_expression(formula, catalog=catalog)
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
                input_windows=compiled.input_windows,
                declared_warmup=max(1, declared_warmup),
                warmup_bars=max(declared_warmup, compiled.warmup_bars),
                direction=str(definition.get("direction", "none")),  # type: ignore[arg-type]
                stability="stable" if status == "active" else "experimental",
            )
        compiled = compile_formula(formula, catalog=catalog)
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
            input_windows=compiled.input_windows,
            warmup_bars=compiled.warmup_bars,
            direction=str(definition.get("direction", "none")),  # type: ignore[arg-type]
            stability="stable" if status == "active" else "experimental",
        )

    if kind != "composite":
        raise ValueError(f"未知 kind: {kind}")
    members_raw = definition.get("members")
    if not isinstance(members_raw, dict) or not (2 <= len(members_raw) <= MAX_COMPOSITE_MEMBERS):
        raise ValueError(f"composite 成员必须是 {2}~{MAX_COMPOSITE_MEMBERS} 个")
    from .dsl import BASE_COLUMNS

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
        if catalog.get(member_id) is None and member_id not in BASE_COLUMNS:
            raise ValueError(f"未知成员因子: {member_id}")
        components.append((member_id, weight))
    dependencies = factor_dependencies(members_raw, catalog=catalog)
    warmup = factor_warmup_bars(members_raw, catalog=catalog)
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


def load_factor_definitions(
    definitions: Iterable[dict], *, catalog: FactorCatalog = BUILTIN_CATALOG,
) -> FactorCatalog:
    """Import an unordered set atomically into a new snapshot.

    This is the legacy DSL/Python expression format, not a sandbox or strict
    execution admission. No file is read and the caller's catalog is untouched
    on success or failure. Unknown external Python input columns retain v1
    semantics; missing DSL factors and cycles are rejected.
    """
    pending = list(definitions)
    ids = [str(definition.get("id", "")) for definition in pending]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate factor definition id")
    result = catalog
    while pending:
        deferred: list[dict] = []
        failures: list[str] = []
        for definition in pending:
            try:
                spec = to_spec(definition, catalog=result)
                result = result.with_factors([spec])
            except ValueError as exc:
                deferred.append(definition)
                failures.append(f"{definition.get('id', '')}: {exc}")
        if len(deferred) == len(pending):
            raise ValueError("unresolved factor definitions: " + "; ".join(failures))
        pending = deferred
    return result
