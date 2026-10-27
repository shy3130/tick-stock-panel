"""单策略依赖包 (bundle) — 导出/检查/导入的服务层。

一个策略往往不止一个 .py: ``META.scoring`` / 模块 ``SCORING`` 引用因子定义,
ENTRY/EXIT_SIGNALS 引用自定义信号 (信号又依赖因子), 用户调参存在
``strategy_overrides/{id}.json``。本模块把这些依赖收成闭包, 打成 ZIP「策略包」:
条目路径与 config_backup 的 ``_classify`` 白名单形态完全一致, 导入侧直接复用
``apply_archive`` 的校验/写盘/导入即冒烟/热重载管线, 不另起一套。

红线: 策略包只允许携带 strategies / strategy_overrides / custom_signals /
custom_factors 四类, 其余路径 (自选股/偏好/密钥…) 一律拒绝 — 策略包不能
变成配置走私通道。
"""
from __future__ import annotations

import ast
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from app.services.config_backup import (
    _CATEGORY_LABELS,
    BACKUP_FORMAT,
    BACKUP_VERSION,
    MAX_ARCHIVE_BYTES,
    MAX_ENTRIES,
    _app_version,
    _classify,
    _read_entry,
    _safe_rel,
)

logger = logging.getLogger(__name__)

BUNDLE_FORMAT = "tick-strategy-bundle"
BUNDLE_VERSION = 1
# 包内允许的类别 (config_backup._classify 类别键的受控子集)
BUNDLE_CATEGORIES: tuple[str, ...] = (
    "strategies",
    "strategy_overrides",
    "custom_signals",
    "custom_factors",
)


class StrategyBundleError(ValueError):
    """包形状/依赖错误 — API 层转 400, 不与引擎异常混淆。"""


@dataclass
class BundleSummary:
    """导出闭包的结果摘要 (进 manifest, 供 UI 展示)。"""

    strategy_id: str
    label: str
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ── 依赖闭包 (导出侧) ────────────────────────────────────────────────


def _strategy_rel_path(data_dir: Path, sdef: Any) -> str | None:
    """StrategyDef → 包内相对路径; 内置策略 (app 包内) 返回 None。"""
    if sdef.file_path is None:
        return None
    try:
        rel = sdef.file_path.relative_to(data_dir)
    except ValueError:
        return None
    if rel.parts and rel.parts[0] == "strategies":
        return rel.as_posix()
    return None


def _signal_lookup(data_dir: Path) -> dict[str, dict]:
    """自定义信号定义的多键索引: id 与输出列名 (csg_*/显式 column) 都能命中。"""
    from app.strategy import custom_signals

    by_key: dict[str, dict] = {}
    for sig in custom_signals.load_all(data_dir):
        sid = sig.get("id")
        if not sid:
            continue
        by_key.setdefault(str(sid), sig)
        try:
            by_key.setdefault(custom_signals.resolve_column(sig), sig)
        except ValueError:
            continue
    return by_key


def _match_signal(ref: str, lookup: dict[str, dict]) -> dict | None:
    """策略引用名 → 信号定义。引用是列名口径 (signal_/csg_ 前缀或裸 id)。"""
    candidates = [ref]
    if ref.startswith("signal_"):
        candidates.append(ref[len("signal_"):])
    if ref.startswith(("csg_", "csgi_")):
        candidates.append(ref[4:])
    elif not ref.startswith(("signal_", "csg_", "csgi_")):
        # 引擎对裸名自动补 signal_ 前缀 (engine._collect_signal_hits 同口径)
        candidates.append(f"signal_{ref}")
    for key in candidates:
        hit = lookup.get(key)
        if hit is not None:
            return hit
    return None


def collect_dependencies(
    data_dir: Path, engine: Any, strategy_id: str
) -> tuple[list[tuple[str, bytes]], BundleSummary]:
    """策略 → (条目列表, 摘要)。条目 = [(包内相对路径, 文件字节)]。

    闭包范围: 策略 .py + 其引用的自定义信号/因子 (递归) + 用户调参 override
    + composite 子策略 (递归)。内置因子列/内置信号名不打包 (任何安装自带);
    找不到的自定义引用 (csg_/uf_) 记 warnings, 不阻断导出 — 如实告知。
    """
    from app import definitions
    from app.factors import store as factor_store
    from app.strategy import config as strategy_config

    try:
        sdef = engine.get(strategy_id)
    except ValueError as e:
        raise StrategyBundleError(str(e)) from e

    label = str(sdef.meta.get("name") or strategy_id)
    summary = BundleSummary(strategy_id=strategy_id, label=label)
    entries: dict[str, bytes] = {}

    factor_defs = {str(d.get("id")): d for d in factor_store.load_all(data_dir)}
    signal_lookup = _signal_lookup(data_dir)
    known_builtin_cols: frozenset[str] | None = None

    included_factors: set[str] = set()
    included_signals: set[str] = set()

    def _add_factor(factor_id: str) -> None:
        if factor_id in included_factors:
            return
        definition = factor_defs.get(factor_id)
        if definition is None:
            return
        included_factors.add(factor_id)
        entries[f"user_data/custom_factors/{factor_id}.json"] = json.dumps(
            definition, ensure_ascii=False, indent=2
        ).encode("utf-8")
        # composite 成员递归在 definition_dependencies 内已展开为列名;
        # 只要是自定义因子就继续收集, 内置列在 _add_factor_ref 里被跳过。
        try:
            deps = definitions.definition_dependencies("factor", definition)
        except ValueError as e:
            summary.warnings.append(f"因子 {factor_id} 定义编译失败, 依赖未展开: {e}")
            return
        for dep in sorted(deps):
            _add_factor_ref(dep)

    def _add_factor_ref(ref: str) -> None:
        nonlocal known_builtin_cols
        if ref in factor_defs:
            _add_factor(ref)
            return
        if known_builtin_cols is None:
            known_builtin_cols = definitions._known_input_columns()
        if ref not in known_builtin_cols:
            summary.warnings.append(
                f"引用因子列 {ref} 不在因子库 (可能已删除或为未知列), 未打包"
            )

    def _add_signal(sig: dict) -> None:
        sid = str(sig.get("id"))
        if sid in included_signals:
            return
        included_signals.add(sid)
        entries[f"user_data/custom_signals/{sid}.json"] = json.dumps(
            sig, ensure_ascii=False, indent=2
        ).encode("utf-8")
        for dep in sorted(definitions.definition_dependencies("signal", sig)):
            _add_factor_ref(dep)

    def _add_strategy(sid: str, root: bool) -> None:
        try:
            child = engine.get(sid)
        except ValueError:
            if root:
                raise
            summary.warnings.append(f"composite 子策略 {sid} 不存在, 未打包")
            return
        rel = _strategy_rel_path(data_dir, child)
        if rel is None:
            if root:
                raise StrategyBundleError(f"策略 {sid} 是内置策略, 无需打包导出")
            summary.warnings.append(
                f"composite 子策略 {sid} 是内置策略 (任何安装自带), 未打包"
            )
        elif rel not in entries:
            path = data_dir / rel
            entries[rel] = path.read_bytes()

        # 因子引用: META.scoring + 模块 SCORING + override 里的 scoring
        factor_refs = {str(k) for k in (child.meta.get("scoring") or {})}

        factor_refs.update(child.scoring_columns)

        # 信号引用: 模块常量 + override 增改
        signal_refs = list(child.entry_signals) + list(child.exit_signals)
        override = strategy_config.load_override(data_dir, sid) or {}
        signal_refs += [str(r) for r in (override.get("entry_signals") or [])]
        signal_refs += [str(r) for r in (override.get("exit_signals") or [])]
        ov_scoring = override.get("scoring")
        if isinstance(ov_scoring, dict):
            factor_refs.update(str(k) for k in ov_scoring)

        for ref in sorted(factor_refs):
            _add_factor_ref(ref)
        for ref in signal_refs:
            hit = _match_signal(ref, signal_lookup)
            if hit is not None:
                _add_signal(hit)
            elif ref.startswith(("csg_", "csgi_")):
                summary.warnings.append(
                    f"引用自定义信号 {ref} 不在信号库 (可能已删除), 未打包"
                )
            # signal_/裸名未命中 → 内置信号命名空间, 任何安装自带, 静默跳过

        # 用户调参 (换机/分享场景里比默认值重要)
        ov_path = data_dir / "user_data" / "strategy_overrides" / f"{sid}.json"
        if ov_path.is_file():
            entries[f"user_data/strategy_overrides/{sid}.json"] = ov_path.read_bytes()

        # composite 子策略递归 (只允许一层, 与引擎非嵌套约束一致)
        if child.composite is not None:
            for c in child.composite.children:
                _add_strategy(c.strategy_id, root=False)

    _add_strategy(strategy_id, root=True)

    counts: dict[str, int] = {}
    for rel in entries:
        category = _classify(PurePosixPath(rel), data_dir)
        if category is not None:
            counts[category[0]] = counts.get(category[0], 0) + 1
    summary.counts = counts
    return sorted(entries.items()), summary


def build_bundle(
    data_dir: Path, engine: Any, strategy_id: str
) -> tuple[bytes, str, BundleSummary]:
    """构建策略包 ZIP: 依赖条目 + manifest。返回 (zip 字节, 文件名, 摘要)。"""
    items, summary = collect_dependencies(data_dir, engine, strategy_id)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    manifest = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "created_at": now.isoformat(),
        "app_version": _app_version(),
        "strategy_id": summary.strategy_id,
        "label": summary.label,
        "counts": summary.counts,
        "warnings": summary.warnings,
        "entries": [
            {"path": rel, "kind": _classify(PurePosixPath(rel), data_dir)[0]} for rel, _ in items
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, payload in items:
            zf.writestr(rel, payload)
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue(), f"tsp-strategy-{summary.strategy_id}.zip", summary


# ── 包解析与检查 (导入侧) ────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedBundle:
    manifest: dict[str, Any]
    entries: list[tuple[str, str, Path]]  # (zip 内路径, 类别, 本机目标绝对路径)


def parse_bundle(data: bytes) -> ParsedBundle:
    """校验包形状: ZIP 可读/上限/manifest 合法/条目全部落在 4 类白名单内。"""
    if not data:
        raise StrategyBundleError("空文件")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise StrategyBundleError(f"策略包超过 {MAX_ARCHIVE_BYTES // 1024 // 1024}MB 上限")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise StrategyBundleError("不是有效的策略包 ZIP 文件") from e
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            raise StrategyBundleError(f"策略包条目数超过上限 ({len(infos)} > {MAX_ENTRIES})")
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError as e:
            raise StrategyBundleError("策略包缺少 manifest.json") from e
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise StrategyBundleError(f"manifest.json 解析失败: {e}") from e
        if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT:
            raise StrategyBundleError("不是本应用的策略包文件")
        if manifest.get("version") != BUNDLE_VERSION:
            raise StrategyBundleError(
                f"不支持的策略包版本 {manifest.get('version')!r} (当前支持 {BUNDLE_VERSION})"
            )

        from app.services.config_backup import _data_dir

        data_dir = _data_dir()
        entries: list[tuple[str, str, Path]] = []
        offenders: list[str] = []
        for info in infos:
            if info.filename == "manifest.json":
                continue
            rel = _safe_rel(info.filename)
            cls = _classify(rel, data_dir) if rel is not None else None
            if cls is None or cls[0] not in BUNDLE_CATEGORIES:
                offenders.append(info.filename)
                continue
            entries.append((info.filename, cls[0], cls[1]))
        if offenders:
            raise StrategyBundleError(
                "策略包包含不允许的条目 (只允许策略/调参/信号/因子): " + ", ".join(offenders[:5])
            )
        if not any(kind == "strategies" for _, kind, _ in entries):
            raise StrategyBundleError("策略包里没有策略文件")
    return ParsedBundle(manifest=manifest, entries=entries)


def _strategy_refs_from_code(code: str) -> tuple[set[str], list[str]]:
    """AST 提取策略源码的引用: (因子引用集合, 信号引用列表)。

    只看顶层赋值字面量 (META.scoring / SCORING / ENTRY_SIGNALS / EXIT_SIGNALS),
    与引擎 getattr(mod, …) 同口径; 不执行任何代码。
    """
    factor_refs: set[str] = set()
    signal_refs: list[str] = []

    def _collect(target_names: tuple[str, ...], node: ast.AST) -> None:
        if not isinstance(node, ast.Assign):
            return
        names = {
            t.id for t in node.targets if isinstance(t, ast.Name) and t.id in target_names
        }
        if not names:
            return
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            return
        if "META" in names:
            scoring = value.get("scoring") if isinstance(value, dict) else None
            if isinstance(scoring, dict):
                factor_refs.update(str(k) for k in scoring)
        if "SCORING" in names and isinstance(value, dict):
            factor_refs.update(str(k) for k in value)
        if names & {"ENTRY_SIGNALS", "EXIT_SIGNALS"} and isinstance(value, (list, tuple)):
            signal_refs.extend(str(v) for v in value if v)

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return factor_refs, signal_refs
    for node in tree.body:
        _collect(("META", "SCORING", "ENTRY_SIGNALS", "EXIT_SIGNALS"), node)
    return factor_refs, signal_refs


def inspect_bundle(data: bytes) -> dict[str, Any]:
    """导入预览: 条目清单 (create/conflict) + 缺失依赖预警 (不落盘)。"""
    from app.factors import store as factor_store

    parsed = parse_bundle(data)
    # 目标路径由 _classify 基于 settings.data_dir 拼出; 取 strategies 条目
    # (strategies/<source>/x.py) 向上两级得到 data_dir。
    data_dir = None
    for _, kind, target in parsed.entries:
        if kind == "strategies":
            data_dir = target.parent.parent
            break
    if data_dir is None:
        raise StrategyBundleError("策略包里没有策略文件")

    items = []
    bundle_factor_ids: set[str] = set()
    bundle_signal_keys: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name, kind, target in parsed.entries:
            payload = _read_entry(zf, name)
            items.append(
                {
                    "path": name,
                    "kind": kind,
                    "id": PurePosixPath(name).stem,
                    "action": "conflict" if target.exists() else "create",
                    "bytes": len(payload),
                }
            )
            if kind == "custom_factors":
                bundle_factor_ids.add(PurePosixPath(name).stem)
            if kind == "custom_signals":
                bundle_signal_keys.add(PurePosixPath(name).stem)
        # 缺失依赖: 包内策略引用 × (包内定义 ∪ 本机定义 ∪ 内置列)
        local_factor_ids = {str(d.get("id")) for d in factor_store.load_all(data_dir)}
        local_signal_keys = set(_signal_lookup(data_dir))
        from app import definitions

        builtin_cols = definitions._known_input_columns()
        missing: list[str] = []
        for name, kind, _ in parsed.entries:
            if kind != "strategies":
                continue
            try:
                code = _read_entry(zf, name).decode("utf-8")
            except UnicodeDecodeError:
                continue
            factor_refs, signal_refs = _strategy_refs_from_code(code)
            for ref in sorted(factor_refs):
                if ref not in bundle_factor_ids and ref not in local_factor_ids and ref not in builtin_cols:
                    missing.append(f"因子 {ref} 不在包内也不在本机, 导入后策略无法评分")
            for ref in signal_refs:
                # 合成索引的值是空 dict (只按键命中), 必须用 is None 判未命中
                if ref.startswith(("csg_", "csgi_")) and (
                    _match_signal(ref, {k: {} for k in bundle_signal_keys | local_signal_keys})
                    is None
                ):
                    missing.append(f"信号 {ref} 不在包内也不在本机")

    return {
        "strategy_id": parsed.manifest.get("strategy_id"),
        "label": parsed.manifest.get("label"),
        "created_at": parsed.manifest.get("created_at"),
        "counts": parsed.manifest.get("counts", {}),
        "warnings": parsed.manifest.get("warnings", []),
        "entries": items,
        "missing_deps": sorted(set(missing)),
    }


def apply_bundle(data: bytes, *, allow_overwrite: bool = False) -> dict[str, Any]:
    """导入策略包: 形状校验后把 manifest 换成备份格式, 原样复用 apply_archive。"""
    from app.services import config_backup

    parsed = parse_bundle(data)
    category_by_name = {name: kind for name, kind, _ in parsed.entries}
    counts: dict[str, int] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf, io.BytesIO() as buf:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            for info in zf.infolist():
                if info.is_dir() or info.filename == "manifest.json":
                    continue
                out.writestr(info.filename, _read_entry(zf, info.filename))
                category = category_by_name.get(info.filename)
                if category:
                    counts[category] = counts.get(category, 0) + 1
            backup_manifest = {
                "format": BACKUP_FORMAT,
                "version": BACKUP_VERSION,
                "created_at": parsed.manifest.get("created_at"),
                "app_version": parsed.manifest.get("app_version", ""),
                "include_secrets": False,
                "categories": [
                    {"key": k, "label": _CATEGORY_LABELS[k], "count": counts[k]}
                    for k in BUNDLE_CATEGORIES
                    if k in counts
                ],
            }
            out.writestr("manifest.json", json.dumps(backup_manifest, ensure_ascii=False))
        return config_backup.apply_archive(
            buf.getvalue(), allow_overwrite=allow_overwrite, categories=BUNDLE_CATEGORIES
        )
