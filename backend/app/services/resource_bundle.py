"""策略、信号和因子的传输服务: 共用依赖闭包、ZIP 校验与资源预检。

旧策略包接口在模块末尾适配格式和返回结构, 逐文件导入仍复用配置备份管线;
当前资源导入保留整批预检、失败回滚语义。
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import logging
import re
import stat
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from app import definitions
from app.factors import registry, store
from app.services import config_backup
from app.strategy import custom_signals
from app.strategy.ai_generator import AIStrategyGenerator, find_meta_assignment

logger = logging.getLogger(__name__)
RESOURCE_FORMAT = "tick-resource-bundle"
RESOURCE_VERSION = 1
_IMPORT_LOCK = threading.RLock()
_KIND_BY_CATEGORY = {"strategies": "strategy", "custom_signals": "signal", "custom_factors": "factor", "strategy_overrides": "settings"}


class ResourceBundleError(ValueError):
    """用户可修正的资源包、冲突或依赖错误。"""


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
    by_key: dict[str, dict] = {}
    for sig in custom_signals.load_all(data_dir):
        sid = sig.get("id")
        if not sid:
            continue
        by_key.setdefault(str(sid), sig)
        try:
            by_key.setdefault(custom_signals.resolve_column(sig), sig)
            by_key.setdefault(custom_signals.intraday_column_name(str(sid)), sig)
        except ValueError:
            continue
    return by_key


def _match_signal(ref: str, lookup: dict[str, dict]) -> dict | None:
    """策略引用名 → 信号定义。引用是列名口径 (signal_/csg_ 前缀或裸 id)。"""
    candidates = [ref]
    if ref.startswith("signal_"):
        candidates.append(ref[len("signal_"):])
    if ref.startswith("csgi_"):
        candidates.append(ref[5:])
    elif ref.startswith("csg_"):
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
    return collect_resources(data_dir, engine, [{"kind": "strategy", "id": strategy_id}])


def collect_resources(
    data_dir: Path, engine: Any, roots: list[dict[str, str]]
) -> tuple[list[tuple[str, bytes]], BundleSummary]:
    """多策略/信号/因子共享同一闭包收集器; 文件和共享依赖只收一次。"""
    from app.strategy import config as strategy_config

    if not roots:
        raise StrategyBundleError("请至少选择一个资源")
    strategy_id = roots[0]["id"]
    summary = BundleSummary(strategy_id=strategy_id, label=strategy_id)
    entries: dict[str, bytes] = {}

    factor_defs = {str(d.get("id")): d for d in store.load_all(data_dir)}
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
        if definition.get("kind") == "composite":
            # 编译后的依赖是已展开的输入列, 仍须保留成员定义本身。
            for member in definition.get("members", {}):
                _add_factor_ref(str(member))
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
        deps = definitions.definition_dependencies("signal", sig)
        if sig.get("timeframe") == "intraday":
            from app.strategy.intraday_features import INTRADAY_FEATURES

            deps -= frozenset(INTRADAY_FEATURES)
        for dep in sorted(deps):
            _add_factor_ref(dep)

    def _add_strategy(sid: str, root: bool) -> None:
        try:
            child = engine.get(sid)
        except ValueError as exc:
            if root:
                raise StrategyBundleError(str(exc)) from exc
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
        else:
            return
        if root and sid == strategy_id:
            summary.label = str(child.meta.get("name") or sid)

        # 同目录辅助模块由引擎白名单加载, 必须随策略一起迁移。
        if rel is not None:
            for helper in (data_dir / rel).parent.glob("_*.py"):
                entries[helper.relative_to(data_dir).as_posix()] = helper.read_bytes()

        # 因子引用: META.scoring + 模块 SCORING + override 里的 scoring
        factor_refs = {str(k) for k in (child.meta.get("scoring") or {})}

        factor_refs.update(child.scoring_columns)
        factor_refs.update(ref for ref in child.required_features if ref.startswith(("uf_", "cf_", "ext_")))
        code = entries.get(rel, b"").decode("utf-8") if rel else ""
        native_signals = child.execution_backend == "matrix_native"
        code_factors, code_signals = _strategy_refs_from_code(code, include_signal_outputs=not native_signals)
        factor_refs.update(code_factors)
        helper_signals: list[str] = []
        if rel is not None:
            for helper in (data_dir / rel).parent.glob("_*.py"):
                helper_factors, helper_signal_refs = _strategy_refs_from_code(helper.read_text(encoding="utf-8"))
                factor_refs.update(helper_factors)
                helper_signals.extend(helper_signal_refs)

        # 信号引用: 模块常量 + override 增改
        # 矩阵策略的进出场名称是 compute_signals 输出标签, 不是信号库输入。
        signal_refs = code_signals + helper_signals
        if not native_signals:
            signal_refs += list(child.entry_signals) + list(child.exit_signals)
        signal_refs.extend(ref for ref in child.required_features if ref.startswith(("csg_", "csgi_", "signal_")))
        override = strategy_config.load_override(data_dir, sid) or {}
        if not native_signals:
            signal_refs += [str(r) for r in (override.get("entry_signals") or [])]
            signal_refs += [str(r) for r in (override.get("exit_signals") or [])]
        ov_scoring = override.get("scoring")
        if isinstance(ov_scoring, dict):
            factor_refs.update(str(k) for k in ov_scoring)
        for condition in override.get("overlay_filter") or []:
            if isinstance(condition, dict):
                factor_refs.add(str(condition.get("left", "")))
                right = condition.get("right")
                if isinstance(right, str) and right.startswith("field:"):
                    factor_refs.add(right[6:])
        factor_refs.discard("")

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
            children = override.get("children")
            child_ids = [str(c["strategy_id"]) for c in children] if isinstance(children, list) and children else [c.strategy_id for c in child.composite.children]
            for child_id in child_ids:
                _add_strategy(child_id, root=False)

    for ref in roots:
        kind, rid = ref["kind"], ref["id"]
        if kind == "strategy":
            _add_strategy(rid, root=True)
        elif kind == "factor" and rid in factor_defs:
            _add_factor(rid)
        elif kind == "signal" and rid in signal_lookup:
            _add_signal(signal_lookup[rid])
        else:
            raise StrategyBundleError(f"资源不存在: {kind}:{rid}")

    counts: dict[str, int] = {}
    for rel in entries:
        category = config_backup._classify(PurePosixPath(rel), data_dir)
        if category is not None:
            counts[category[0]] = counts.get(category[0], 0) + 1
    summary.counts = counts
    return sorted(entries.items()), summary


@dataclass(frozen=True)
class ParsedBundle:
    manifest: dict[str, Any]
    entries: list[tuple[str, str, Path]]  # (zip 内路径, 类别, 本机目标绝对路径)


def parse_bundle(data: bytes, *, allow_resources: bool = False) -> ParsedBundle:
    """校验包形状: ZIP 可读/上限/manifest 合法/条目全部落在 4 类白名单内。"""
    if not data:
        raise StrategyBundleError("空文件")
    if len(data) > config_backup.MAX_ARCHIVE_BYTES:
        raise StrategyBundleError(f"策略包超过 {config_backup.MAX_ARCHIVE_BYTES // 1024 // 1024}MB 上限")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise StrategyBundleError("不是有效的策略包 ZIP 文件") from e
    with zf:
        infos = _zip_entries(zf)
        try:
            manifest = json.loads(config_backup._read_entry(zf, "manifest.json"))
        except KeyError as e:
            raise StrategyBundleError("策略包缺少 manifest.json") from e
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise StrategyBundleError(f"manifest.json 解析失败: {e}") from e
        formats = {BUNDLE_FORMAT, RESOURCE_FORMAT} if allow_resources else {BUNDLE_FORMAT}
        if not isinstance(manifest, dict) or manifest.get("format") not in formats:
            raise StrategyBundleError("不是本应用的策略包文件")
        if manifest.get("version") != BUNDLE_VERSION:
            raise StrategyBundleError(
                f"不支持的策略包版本 {manifest.get('version')!r} (当前支持 {BUNDLE_VERSION})"
            )

        data_dir = config_backup._data_dir()
        entries: list[tuple[str, str, Path]] = []
        offenders: list[str] = []
        for info in infos:
            if info.filename == "manifest.json":
                continue
            rel = config_backup._safe_rel(info.filename)
            cls = config_backup._classify(rel, data_dir) if rel is not None else None
            if cls is None or cls[0] not in BUNDLE_CATEGORIES:
                offenders.append(info.filename)
                continue
            entries.append((info.filename, cls[0], cls[1]))
        if offenders:
            raise StrategyBundleError(
                "策略包包含不允许的条目 (只允许策略/调参/信号/因子): " + ", ".join(offenders[:5])
            )
        if manifest.get("format") == BUNDLE_FORMAT and not any(kind == "strategies" for _, kind, _ in entries):
            raise StrategyBundleError("策略包里没有策略文件")
    return ParsedBundle(manifest=manifest, entries=entries)


def _strategy_refs_from_code(code: str, *, include_signal_outputs: bool = True) -> tuple[set[str], list[str]]:
    """AST 提取策略源码的引用: (因子引用集合, 信号引用列表)。

    只看顶层赋值字面量 (META.scoring / SCORING / ENTRY_SIGNALS / EXIT_SIGNALS),
    与引擎 getattr(mod, …) 同口径; 不执行任何代码。矩阵策略可排除进出场输出标签,
    但 REQUIRED_FEATURES、META.required_features 和源码中的显式列输入始终保留。
    """
    factor_refs: set[str] = set()
    signal_refs: list[str] = []

    def _collect(target_names: tuple[str, ...], node: ast.AST) -> None:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            return
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = {
            t.id for t in targets if isinstance(t, ast.Name) and t.id in target_names
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
            if isinstance(value, dict):
                for ref in value.get("required_features") or []:
                    if isinstance(ref, str) and ref.startswith(("uf_", "cf_", "ext_")):
                        factor_refs.add(ref)
                    elif isinstance(ref, str) and ref.startswith(("csg_", "csgi_", "signal_")):
                        signal_refs.append(ref)
        if "SCORING" in names and isinstance(value, dict):
            factor_refs.update(str(k) for k in value)
        if include_signal_outputs and names & {"ENTRY_SIGNALS", "EXIT_SIGNALS"} and isinstance(value, (list, tuple)):
            signal_refs.extend(str(v) for v in value if v)

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return factor_refs, signal_refs
    for node in tree.body:
        _collect(("META", "SCORING", "ENTRY_SIGNALS", "EXIT_SIGNALS"), node)
    for node in ast.walk(tree):
        # 明确声明的输入列和代码中的自定义列引用; 不猜测动态拼接的列名。
        values: list[str] = []
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if targets and any(isinstance(t, ast.Name) and t.id == "REQUIRED_FEATURES" for t in targets):
            try:
                value = ast.literal_eval(node.value)
                if isinstance(value, (set, list, tuple)):
                    values = [v for v in value if isinstance(v, str)]
            except (ValueError, SyntaxError):
                pass
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "col":
            values.extend(a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "matrix_feature":
            values.extend(a.value for a in node.args[1:2] if isinstance(a, ast.Constant) and isinstance(a.value, str))
        for value in values:
            if value.startswith(("uf_", "cf_", "ext_")):
                factor_refs.add(value)
            elif value.startswith(("csg_", "csgi_", "signal_")):
                signal_refs.append(value)
    return factor_refs, signal_refs


def _archive(items: dict[str, bytes], manifest: dict) -> bytes:
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in sorted(items.items()):
                archive.writestr(name, payload)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return buffer.getvalue()


def _json(payload: bytes) -> dict:
    try:
        doc = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ResourceBundleError(f"JSON 无法解析: {exc}") from exc
    if not isinstance(doc, dict):
        raise ResourceBundleError("资源定义必须是 JSON 对象")
    return doc


def _hash(payload: bytes | None) -> str:
    return hashlib.sha256(payload).hexdigest() if payload is not None else "absent"


def _same(left: bytes, right: bytes, suffix: str) -> bool:
    if suffix == ".json":
        try:
            return json.loads(left) == json.loads(right)
        except (ValueError, UnicodeDecodeError):
            return False
    return left == right


def _zip(items: dict[str, bytes], roots: list[str], inputs: dict[str, list[str]] | None = None) -> bytes:
    manifest = {"format": RESOURCE_FORMAT, "version": RESOURCE_VERSION,
                "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "app_version": config_backup._app_version(), "roots": roots, "inputs": inputs or {}}
    return _archive(items, manifest)


def _target(name: str) -> tuple[str, Path]:
    data_dir = config_backup._data_dir()
    rel = config_backup._safe_rel(name)
    classified = config_backup._classify(rel, data_dir)
    if rel.as_posix() != name or classified is None or classified[0] not in BUNDLE_CATEGORIES:
        raise ResourceBundleError(f"不允许的资源路径: {name}")
    category, target = classified
    # data_dir 自身可由部署配置指向符号链接; 包内子路径必须是普通路径。
    current = target
    while current != data_dir:
        if current.is_symlink():
            raise ResourceBundleError(f"资源目标路径包含符号链接: {name}")
        current = current.parent
    if not target.resolve().is_relative_to(data_dir.resolve()):
        raise ResourceBundleError(f"资源目标越界: {name}")
    return category, target


def _metadata(name: str, payload: bytes) -> dict:
    category, target = _target(name)
    kind = _KIND_BY_CATEGORY[category]
    rid = PurePosixPath(name).stem
    label = rid
    draft = False
    if kind == "strategy":
        if rid.startswith("_"):
            kind, rid = "code", name
        else:
            meta = AIStrategyGenerator._extract_meta(payload.decode("utf-8-sig"))
            rid = str(meta.get("id", ""))
            if not config_backup._NAME_RE.fullmatch(rid):
                raise ResourceBundleError(f"策略 META.id 格式非法: {name}")
            label = str(meta.get("name") or rid)
            draft = bool(meta.get("research_only"))
    elif kind in ("signal", "factor"):
        doc = _json(payload)
        if doc.get("id") != rid or not definitions.get_kind(kind).id_pattern.fullmatch(rid):
            raise ResourceBundleError(f"定义 ID 与文件名不一致或格式非法: {name}")
        label = str(doc.get("name") or doc.get("label") or rid)
    elif kind == "settings":
        _json(payload)
        label = f"{rid} 的调参"
    return {"key": f"{kind}:{rid}", "kind": kind, "id": rid, "label": label, "path": name,
            "category": category, "target": target, "payload": payload, "draft": draft}


def _zip_entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = [info for info in archive.infolist() if not info.is_dir()]
    if len(entries) > config_backup.MAX_ENTRIES:
        raise StrategyBundleError("资源包条目数超过上限")
    seen = set()
    total = 0
    for info in entries:
        try:
            rel = config_backup._safe_rel(info.filename)
        except config_backup.ConfigBackupError as exc:
            raise StrategyBundleError(str(exc)) from exc
        if rel.as_posix() != info.filename:
            raise StrategyBundleError(f"非法条目路径: {info.filename}")
        if info.filename in seen:
            raise StrategyBundleError(f"资源包包含重复条目: {info.filename}")
        seen.add(info.filename)
        if stat.S_ISLNK(info.external_attr >> 16):
            raise StrategyBundleError(f"资源包包含符号链接: {info.filename}")
        total += info.file_size
        if total > config_backup.MAX_TOTAL_BYTES or info.file_size > config_backup.MAX_FILE_BYTES:
            raise StrategyBundleError("资源包解压总量或单文件大小超过上限")
    return entries


def _read_uploads(uploads: list[tuple[str, bytes]]) -> tuple[dict[str, dict], list[str]]:
    if not uploads or len(uploads) > config_backup.MAX_ENTRIES:
        raise ResourceBundleError("请选择资源文件 (数量不能超过 2000)")
    items: dict[str, bytes] = {}
    roots: list[str] = []
    inputs: dict[str, set[str]] = {}
    total = 0

    def add(name: str, payload: bytes) -> None:
        nonlocal total
        _target(name)
        if name in items:
            if not _same(items[name], payload, PurePosixPath(name).suffix):
                raise ResourceBundleError(f"所选文件携带了不同内容的同名资源: {name}")
            return
        if len(payload) > config_backup.MAX_FILE_BYTES:
            raise ResourceBundleError(f"资源文件超过大小上限: {name}")
        total += len(payload)
        if total > config_backup.MAX_TOTAL_BYTES or len(items) >= config_backup.MAX_ENTRIES:
            raise ResourceBundleError("所选资源解压总量或条目数超过上限")
        items[name] = payload

    def add_loose(filename: str, payload: bytes) -> None:
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix == ".json":
            doc = _json(payload)
            kind = "signal" if "conditions" in doc else "factor" if "formula" in doc or doc.get("kind") == "composite" else None
            if kind is None:
                raise ResourceBundleError(f"无法识别 {filename}，请选择信号或因子定义 JSON")
            rid = str(doc.get("id", ""))
            if not definitions.get_kind(kind).id_pattern.fullmatch(rid):
                raise ResourceBundleError(f"{filename} 的资源 ID 非法")
            category = "custom_signals" if kind == "signal" else "custom_factors"
            add(f"user_data/{category}/{rid}.json", payload)
            roots.append(f"{kind}:{rid}")
        elif suffix == ".py":
            code = payload.decode("utf-8-sig")
            meta = AIStrategyGenerator._extract_meta(code)
            rid = str(meta.get("id", ""))
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", rid):
                raise ResourceBundleError(f"{filename} 缺少有效的 META.id")
            source = "composite" if meta.get("children") else "ai" if rid.startswith("ai_") else "custom"
            if source == "ai" and "research_only" not in meta:
                # 与 AI 策略新建口径一致; 包内显式发布/草稿状态则原样保留。
                found = find_meta_assignment(code)
                if found is None:
                    raise ResourceBundleError("找不到 META 字典")
                node = found[1]
                encoded = code.encode("utf-8")
                lines = encoded.splitlines(keepends=True)
                position = sum(len(line) for line in lines[:node.lineno - 1]) + node.col_offset + 1
                code = (encoded[:position] + b'"research_only": True, ' + encoded[position:]).decode("utf-8")
            add(f"strategies/{source}/{rid}.py", code.encode("utf-8"))
            roots.append(f"strategy:{rid}")
        else:
            raise ResourceBundleError(f"不支持 {filename}，请选择 .zip、.py 或 .json")

    def add_plain_zip(archive: zipfile.ZipFile, entries: list[zipfile.ZipInfo]) -> None:
        root_count = len(roots)
        for info in entries:
            rel = PurePosixPath(info.filename)
            if "__MACOSX" in rel.parts or rel.name == ".DS_Store" or rel.name.startswith("._") or rel.suffix.lower() in (".md", ".txt", ".rst"):
                continue
            payload = config_backup._read_entry(archive, info.filename)
            # 容忍外层分享文件夹, 但保留已导出资源的来源目录、辅助模块与调参。
            for offset in range(len(rel.parts)):
                path = PurePosixPath(*rel.parts[offset:])
                classified = config_backup._classify(path, config_backup._data_dir())
                if classified is not None and classified[0] in BUNDLE_CATEGORIES:
                    add(path.as_posix(), payload)
                    item = _metadata(path.as_posix(), payload)
                    if item["kind"] in ("strategy", "factor", "signal"):
                        roots.append(item["key"])
                    break
            else:
                add_loose(info.filename, payload)
        if len(roots) == root_count:
            raise ResourceBundleError("压缩包中没有可导入的策略 .py、因子或信号 .json 文件")

    if sum(len(payload) for _, payload in uploads) > config_backup.MAX_ARCHIVE_BYTES:
        raise ResourceBundleError("上传文件总量超过 128MB 上限")
    for filename, payload in uploads:
        if not payload:
            raise ResourceBundleError(f"空文件: {filename}")
        suffix = Path(filename).suffix.lower()
        if suffix == ".zip":
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    entries = _zip_entries(archive)
                    if not any(info.filename == "manifest.json" for info in entries):
                        add_plain_zip(archive, entries)
                        continue
                    header = _json(config_backup._read_entry(archive, "manifest.json"))
                    if header.get("format") == config_backup.BACKUP_FORMAT:
                        raise ResourceBundleError("这是配置备份，请到「设置 → 配置备份」恢复")
                parsed = parse_bundle(payload, allow_resources=True)
                declared_inputs = parsed.manifest.get("inputs", {})
                if not isinstance(declared_inputs, dict) or any(not isinstance(k, str) or not isinstance(v, list) or not all(isinstance(ref, str) for ref in v) for k, v in declared_inputs.items()):
                    raise ResourceBundleError("资源包的输入依赖声明格式非法")
                for key, refs in declared_inputs.items():
                    inputs.setdefault(key, set()).update(refs)
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    for name, _, _ in parsed.entries:
                        add(name, config_backup._read_entry(archive, name))
                if parsed.manifest.get("format") == RESOURCE_FORMAT:
                    bundle_roots = parsed.manifest.get("roots")
                    if not isinstance(bundle_roots, list) or not bundle_roots or not all(isinstance(k, str) for k in bundle_roots):
                        raise ResourceBundleError("资源包缺少有效的导出选择清单")
                    roots.extend(bundle_roots)
                else:
                    sid = parsed.manifest.get("strategy_id")
                    roots.extend([f"strategy:{sid}"] if sid else [f"strategy:{PurePosixPath(n).stem}" for n, kind, _ in parsed.entries if kind == "strategies" and not PurePosixPath(n).stem.startswith("_")])
            except (zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
                raise ResourceBundleError(f"无法读取资源包 {filename}: {exc}") from exc
        else:
            add_loose(filename, payload)
    resources: dict[str, dict] = {}
    for name, payload in items.items():
        item = _metadata(name, payload)
        if item["key"] in resources:
            raise ResourceBundleError(f"同一资源 ID 出现在多个来源路径: {item['key']}")
        resources[item["key"]] = item
    for item in resources.values():
        item["inputs"] = sorted(inputs.get(item["key"], ()))
        if item["kind"] == "settings" and (owner := resources.get(f"strategy:{item['id']}")) is not None:
            item["label"] = f"{owner['label']}的调参"
    roots = list(dict.fromkeys(roots))
    if set(inputs) - set(resources):
        raise ResourceBundleError("输入依赖声明指向了包内不存在的资源")
    if not roots or any(k not in resources or resources[k]["kind"] not in ("strategy", "signal", "factor") for k in roots):
        raise ResourceBundleError("导出选择清单指向了包内不存在的资源")
    return resources, roots


def _literals(code: str) -> dict[str, Any]:
    result = {}
    for node in ast.parse(code).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                continue
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(target, ast.Name):
                    result[target.id] = value
    return result


def _factor_refs(doc: dict) -> set[str]:
    if doc.get("kind") == "composite":
        members = doc.get("members", {})
        return set(members) if isinstance(members, dict) else set()
    return set(store.to_spec(doc).dependencies)


def _condition_refs(conditions: list) -> set[str]:
    refs = set()
    for c in conditions:
        if not isinstance(c, dict):
            continue
        if isinstance(c.get("left"), str):
            refs.add(c["left"])
        right = c.get("right")
        if isinstance(right, str):
            if right.startswith("field:"):
                refs.add(right[6:])
            elif right in custom_signals.allowed_fields():
                refs.add(right)
    return refs


def _signal_input_refs(definition: dict) -> frozenset[str]:
    deps = definitions.definition_dependencies("signal", definition)
    if definition.get("timeframe") == custom_signals.TIMEFRAME_INTRADAY:
        from app.strategy.intraday_features import INTRADAY_FEATURES

        deps -= frozenset(INTRADAY_FEATURES)
    return deps


def _analyze(resources: dict[str, dict]) -> dict[str, registry.FactorSpec]:
    """按包内+本机合并快照验证; 预览不执行策略源码、不更改全局注册表。"""
    data_dir = config_backup._data_dir()
    local_factors = {f["id"]: f for f in store.load_all(data_dir)}
    local_signals = _signal_lookup(data_dir)
    local_strategies: dict[str, list[Path]] = {}
    for source in ("custom", "ai", "composite"):
        for path in (data_dir / "strategies" / source).glob("*.py"):
            if path.stem.startswith("_"):
                continue
            try:
                meta = AIStrategyGenerator._extract_meta(path.read_text(encoding="utf-8"))
                local_strategies.setdefault(str(meta.get("id")), []).append(path)
            except (ValueError, SyntaxError, UnicodeDecodeError):
                continue
    for item in resources.values():
        item.update(dependencies=[], issues=[], local_dependencies=[])
    registry.all_factors()  # 先同步本机已配置的扩展列, 再复制只读预检快照。
    with registry.isolated_registry() as snapshot:
        # 不把当前进程中已经删除/来自其他目录的动态因子误当成可用依赖。
        for fid, spec in list(snapshot.items()):
            if spec.kind in ("custom", "composite"):
                snapshot.pop(fid)
        factors = {**local_factors, **{i["id"]: _json(i["payload"]) for i in resources.values() if i["kind"] == "factor"}}
        visiting: set[str] = set()
        checked: set[str] = set()

        def factor(fid: str) -> None:
            if fid in checked:
                return
            if fid in visiting:
                raise ValueError(f"因子存在循环依赖: {fid}")
            if fid not in factors:
                return
            visiting.add(fid)
            doc = factors[fid]
            try:
                for dep in _factor_refs(doc):
                    factor(dep)
                snapshot[fid] = store.to_spec(doc)
                checked.add(fid)
            finally:
                visiting.remove(fid)

        for fid in factors:
            try:
                factor(fid)
            except (ValueError, TypeError, KeyError) as exc:
                if f"factor:{fid}" in resources:
                    resources[f"factor:{fid}"]["issues"].append(str(exc))
        known = definitions._known_input_columns() | {"symbol", "date"}
        signal_keys = dict(local_signals)
        for item in resources.values():
            if item["kind"] == "signal":
                doc = _json(item["payload"])
                signal_keys[doc["id"]] = doc
                signal_keys[custom_signals.resolve_column(doc)] = doc
                signal_keys[custom_signals.intraday_column_name(doc["id"])] = doc

        resolving_local: set[tuple[str, str]] = set()

        def field(item: dict, ref: str, *, is_signal: bool = False) -> None:
            signal = _match_signal(ref, signal_keys) if is_signal or ref.startswith(("csg_", "csgi_", "signal_")) else None
            if signal is not None:
                key = f"signal:{signal['id']}"
            elif ref in factors:
                key = f"factor:{ref}"
            elif ref in known:
                return
            else:
                item["issues"].append(f"缺少依赖: {ref}")
                return
            if key in resources:
                item["dependencies"].append(key)
            else:
                item["local_dependencies"].append(key)
                marker = (item["key"], key)
                if marker in resolving_local:
                    return
                resolving_local.add(marker)
                try:
                    if signal is not None:
                        doc = {**signal, "enabled": True}
                        custom_signals.validate(doc)
                        for dep in _signal_input_refs(doc):
                            field(item, dep)
                    elif ref in factors:
                        if ref not in checked:
                            raise ValueError(f"本机依赖因子不可用: {ref}")
                        for dep in _factor_refs(factors[ref]):
                            field(item, dep)
                except (ValueError, TypeError, KeyError) as exc:
                    item["issues"].append(str(exc))
                finally:
                    resolving_local.remove(marker)

        for item in resources.values():
            kind, payload = item["kind"], item["payload"]
            try:
                for ref in item["inputs"]:
                    field(item, ref)
                if kind == "factor":
                    for ref in _factor_refs(_json(payload)):
                        field(item, ref)
                elif kind == "signal":
                    doc = {**_json(payload), "enabled": True}
                    custom_signals.validate(doc)
                    for ref in _signal_input_refs(doc):
                        field(item, ref)
                    smoke = definitions.smoke_definition("signal", doc)
                    item["issues"].extend(i["message"] for i in smoke["issues"])
                elif kind in ("strategy", "code"):
                    code = payload.decode("utf-8-sig")
                    helpers = [i for i in resources.values() if i["kind"] == "code" and i["target"].parent == item["target"].parent]
                    dep_names = frozenset(i["target"].stem for i in helpers) | frozenset(p.stem for p in item["target"].parent.glob("_*.py"))
                    config_backup._validate_strategy_code(code, is_dependency=kind == "code", dep_names=dep_names)
                    values = _literals(code)
                    metadata = values.get("META")
                    native_signals = values.get("EXECUTION_BACKEND", metadata.get("execution_backend") if isinstance(metadata, dict) else None) == "matrix_native"
                    factor_refs, signal_refs = _strategy_refs_from_code(code, include_signal_outputs=not native_signals)
                    for ref in factor_refs:
                        field(item, ref)
                    for ref in signal_refs:
                        field(item, ref, is_signal=True)
                    if kind == "code":
                        continue
                    meta = AIStrategyGenerator._extract_meta(code)
                    item["dependencies"].extend(i["key"] for i in helpers)
                    settings_key = f"settings:{item['id']}"
                    override = _json(resources[settings_key]["payload"]) if settings_key in resources else {}
                    if settings_key in resources:
                        item["dependencies"].append(settings_key)
                    for ref in override.get("scoring") or {}:
                        field(item, str(ref))
                    for ref in _condition_refs(override.get("overlay_filter") or []):
                        field(item, ref)
                    for ref in ([] if native_signals else [*(override.get("entry_signals") or []), *(override.get("exit_signals") or [])]):
                        field(item, str(ref), is_signal=True)
                    children = override.get("children") or values.get("META", {}).get("children") or []
                    for child in children:
                        sid = child["strategy_id"]
                        key = f"strategy:{sid}"
                        if key in resources:
                            item["dependencies"].append(key)
                        elif not any((data_dir / "strategies" / source / f"{sid}.py").is_file() for source in ("custom", "ai", "composite")):
                            item["issues"].append(f"缺少子策略: {sid}")
                    # ID 全局唯一, 不允许以不同来源路径绕过冲突检测。
                    if any(path != item["target"] for path in local_strategies.get(item["id"], [])):
                        item["issues"].append("本机同 ID 策略位于另一来源，请先处理已有策略")
                elif kind == "settings":
                    config_backup._validate_json_object(payload, what="策略调参")
            except (ValueError, SyntaxError, TypeError, KeyError, UnicodeDecodeError) as exc:
                item["issues"].append(str(exc))
            item["dependencies"] = sorted(set(item["dependencies"]))
            item["issues"] = list(dict.fromkeys(item["issues"]))
        return {fid: snapshot[fid] for fid in checked}


def _closure(resources: dict[str, dict], selected: list[str]) -> list[str]:
    if not selected or any(key not in resources or resources[key]["kind"] not in ("strategy", "signal", "factor") for key in selected):
        raise ResourceBundleError("请至少选择一个包内存在的策略、信号或因子")
    visited: set[str] = set()
    visiting: set[str] = set()
    order: list[str] = []

    def visit(key: str) -> None:
        if key in visiting:
            raise ResourceBundleError(f"资源存在循环依赖: {key}")
        if key in visited:
            return
        visiting.add(key)
        for dep in resources[key]["dependencies"]:
            visit(dep)
        visiting.remove(key)
        visited.add(key)
        order.append(key)

    for key in selected:
        visit(key)
    return order


def _inspect(uploads: list[tuple[str, bytes]]) -> tuple[dict, dict[str, dict], dict[str, registry.FactorSpec]]:
    resources, roots = _read_uploads(uploads)
    specs = _analyze(resources)
    revision_parts = []
    revision_parts.extend((f"upload:{name}", _hash(payload)) for name, payload in uploads)
    for item in resources.values():
        target = item["target"]
        local = target.read_bytes() if target.exists() else None
        item["before"] = local
        item["action"] = "create" if local is None else "reuse" if _same(local, item["payload"], target.suffix) else "conflict"
        item["comparison"] = ({"local": local.decode("utf-8", errors="replace")[:6000], "incoming": item["payload"].decode("utf-8", errors="replace")[:6000],
                               "truncated": len(local) > 6000 or len(item["payload"]) > 6000} if item["action"] == "conflict" else None)
        revision_parts.append((item["key"], _hash(local), _hash(item["payload"])))
    # 本机依赖变化也使预览失效, 防止预览后修改因子而导入另一种语义。
    data_dir = config_backup._data_dir()
    for directory in ("custom_factors", "custom_signals", "strategy_overrides"):
        for path in sorted((data_dir / "user_data" / directory).glob("*.json")):
            revision_parts.append((path.relative_to(data_dir).as_posix(), _hash(path.read_bytes())))
    for source in ("custom", "ai", "composite"):
        for path in sorted((data_dir / "strategies" / source).glob("*.py")):
            revision_parts.append((path.relative_to(data_dir).as_posix(), _hash(path.read_bytes())))
    revision = _hash(json.dumps(sorted(revision_parts), ensure_ascii=False).encode())
    impact = _local_dependents(data_dir) if any(i["action"] == "conflict" for i in resources.values()) else {}
    for item in resources.values():
        item["referenced_by"] = impact.get(item["key"], [])
    entries = [{k: i[k] for k in ("key", "kind", "id", "label", "path", "dependencies", "local_dependencies", "issues", "action", "comparison", "referenced_by", "draft")} for i in resources.values()]
    counts = {kind: sum(i["kind"] == kind for i in entries) for kind in ("strategy", "signal", "factor", "settings", "code")}
    return {"roots": roots, "entries": sorted(entries, key=lambda i: (i["kind"], i["label"])), "counts": counts, "revision": revision}, resources, specs


def inspect_resources(uploads: list[tuple[str, bytes]]) -> dict:
    try:
        return _inspect(uploads)[0]
    except (ValueError, SyntaxError, TypeError, KeyError, UnicodeDecodeError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ResourceBundleError(str(exc)) from exc


def _local_dependents(data_dir: Path) -> dict[str, list[str]]:
    """替换前展示本机引用方, 包括经信号/组合因子间接引用的策略。"""
    edges: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    signals = _signal_lookup(data_dir)

    def key_for(ref: str) -> str:
        signal = _match_signal(ref, signals) if ref.startswith(("csg_", "csgi_", "signal_")) else None
        return f"signal:{signal['id']}" if signal is not None else f"factor:{ref}"

    for definition in store.load_all(data_dir):
        key = f"factor:{definition['id']}"
        labels[key] = str(definition.get("label") or definition["id"])
        try:
            refs = _factor_refs(definition)
        except (ValueError, TypeError, KeyError):
            refs = set()
        edges[key] = {key_for(ref) for ref in refs}
    for definition in {str(s['id']): s for s in signals.values()}.values():
        key = f"signal:{definition['id']}"
        labels[key] = str(definition.get("name") or definition["id"])
        edges[key] = {key_for(ref) for ref in _condition_refs(definition.get("conditions") or [])}
    for source in ("custom", "ai", "composite"):
        for path in (data_dir / "strategies" / source).glob("*.py"):
            if path.stem.startswith("_"):
                continue
            try:
                code = path.read_text(encoding="utf-8")
                meta = AIStrategyGenerator._extract_meta(code)
                key = f"strategy:{meta['id']}"
                labels[key] = str(meta.get("name") or meta["id"])
                values = _literals(code)
                native_signals = values.get("EXECUTION_BACKEND", meta.get("execution_backend")) == "matrix_native"
                factors, signal_refs = _strategy_refs_from_code(code, include_signal_outputs=not native_signals)
                override_path = data_dir / "user_data/strategy_overrides" / f"{meta['id']}.json"
                override = _json(override_path.read_bytes()) if override_path.exists() else {}
                factors.update(override.get("scoring") or {})
                factors.update(_condition_refs(override.get("overlay_filter") or []))
                if not native_signals:
                    signal_refs += [*(override.get("entry_signals") or []), *(override.get("exit_signals") or [])]
                edges[key] = {key_for(ref) for ref in factors}
                edges[key].update(f"signal:{match['id']}" for ref in signal_refs if (match := _match_signal(ref, signals)) is not None)
                edges[key].update(f"strategy:{c['strategy_id']}" for c in override.get("children") or meta.get("children") or [])
                edges[key].add(f"settings:{meta['id']}")
            except (ValueError, SyntaxError, KeyError, TypeError, UnicodeDecodeError):
                continue
    reverse: dict[str, set[str]] = {}
    for parent, deps in edges.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(parent)
    result = {}
    for key in reverse:
        seen = set()
        stack = list(reverse[key])
        while stack:
            parent = stack.pop()
            if parent == key or parent in seen:
                continue
            seen.add(parent)
            stack.extend(reverse.get(parent, ()))
        kind_labels = {"strategy": "策略", "signal": "信号", "factor": "因子"}
        result[key] = sorted(f"{kind_labels.get(parent.split(':')[0], '资源')}「{labels[parent]}」" for parent in seen if parent in labels)
    return result


def list_resources(data_dir: Path, engine: Any) -> list[dict]:
    items = [{"kind": d["kind"], "id": d["id"], "label": d["label"], "description": ""} for d in definitions.catalog(data_dir)]
    if engine is not None:
        for strategy in engine.list_strategies(include_research=True):
            sdef = engine.get(strategy["id"])
            if _strategy_rel_path(data_dir, sdef) is not None:
                items.append({"kind": "strategy", "id": strategy["id"], "label": str(sdef.meta.get("name") or strategy["id"]),
                              "description": str(sdef.meta.get("description") or ""), "source": sdef.source, "draft": bool(sdef.meta.get("research_only"))})
    return sorted(items, key=lambda i: (i["kind"], i["label"]))


def build_resources(data_dir: Path, engine: Any, roots: list[dict[str, str]]) -> tuple[bytes, str, dict]:
    keys = list(dict.fromkeys(f"{ref['kind']}:{ref['id']}" for ref in roots))
    allowed = {f"{i['kind']}:{i['id']}" for i in list_resources(data_dir, engine)}
    if not keys or any(key not in allowed for key in keys):
        raise ResourceBundleError("请选择可导出的策略、信号或自定义因子")
    try:
        items, _ = collect_resources(data_dir, engine, roots)
        inputs = {}
        if engine is not None:
            for name, _payload in items:
                if name.startswith("strategies/") and not PurePosixPath(name).stem.startswith("_"):
                    sdef = engine.get(_metadata(name, _payload)["id"])
                    inputs[f"strategy:{sdef.meta['id']}"] = sorted(ref for ref in sdef.required_features if ref.startswith(("uf_", "cf_", "ext_", "csg_", "csgi_", "signal_")))
        blob = _zip(dict(items), keys, inputs)
        preview = inspect_resources([("resources.zip", blob)])
        preview["revision"] = _hash(json.dumps([(name, _hash(payload)) for name, payload in sorted(items)], ensure_ascii=False).encode())
        return blob, "tsp-resources.zip", preview
    except (StrategyBundleError, config_backup.ConfigBackupError, ValueError, OSError) as exc:
        raise ResourceBundleError(str(exc)) from exc


def apply_resources(uploads: list[tuple[str, bytes]], *, selected: list[str], overwrite: list[str], revision: str, engine: Any = None, refresh: Callable[[], None] | None = None) -> dict:
    try:
        preview, resources, specs = _inspect(uploads)
        if revision != preview["revision"]:
            raise ResourceBundleError("本机资源已变化，请重新预览后再导入")
        order = _closure(resources, selected)
        if set(overwrite) - set(resources):
            raise ResourceBundleError("替换清单包含未知资源")
        issues = [f"{resources[k]['label']}: {issue}" for k in order for issue in resources[k]["issues"]]
        if issues:
            raise ResourceBundleError("所选资源校验失败: " + "; ".join(issues[:8]))
        conflicts = [k for k in order if resources[k]["action"] == "conflict" and k not in overwrite]
        if conflicts:
            raise ResourceBundleError("请处理同 ID 内容冲突: " + ", ".join(resources[k]["label"] for k in conflicts))
        writes = [resources[k] for k in order if resources[k]["action"] != "reuse"]
        # 复用磁盘定义时也保证其已注册 (例如从历史配置备份恢复过的因子)。
        needed_factors = {resources[k]["id"] for k in order if resources[k]["kind"] == "factor"}
        needed_factors.update(dep[7:] for k in order for dep in resources[k]["local_dependencies"] if dep.startswith("factor:"))
        pending = list(needed_factors)
        while pending:
            spec = specs.get(pending.pop())
            if spec is None:
                continue
            for fid in {*spec.dependencies, *(member for member, _ in spec.components)}:
                if fid in specs and fid not in needed_factors:
                    needed_factors.add(fid)
                    pending.append(fid)
        changed_specs = {fid: spec for fid, spec in specs.items() if fid in needed_factors and registry.get_factor(fid) != spec}
        previous_specs = {fid: registry.get_factor(fid) for fid in changed_specs}
        written: list[dict] = []
        with _IMPORT_LOCK:
            for key in order:
                item = resources[key]
                _target(item["path"])
                current = item["target"].read_bytes() if item["target"].exists() else None
                if current != item["before"]:
                    raise ResourceBundleError("本机资源已变化，请重新预览后再导入")
            try:
                for item in writes:
                    # 先记录再写: 原子替换失败也能恢复临时文件之外的原始目标。
                    written.append(item)
                    config_backup._write_payload(item["target"], item["payload"])
                for fid, spec in changed_specs.items():
                    registry.unregister_factor(fid)
                    registry.register_factor(spec)
                if engine is not None and (writes or changed_specs):
                    engine.reload()
                    for key in order:
                        item = resources[key]
                        if item["kind"] == "strategy" and engine.get(item["id"]).file_path.resolve() != item["target"].resolve():
                            raise ValueError(f"策略未加载到预期位置: {item['label']}")
                if refresh is not None and (writes or changed_specs):
                    refresh()
            except Exception as exc:
                rollback_errors = []
                for item in reversed(written):
                    try:
                        if item["before"] is None:
                            item["target"].unlink(missing_ok=True)
                        else:
                            config_backup._write_payload(item["target"], item["before"])
                    except OSError as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                for fid, old in previous_specs.items():
                    registry.unregister_factor(fid)
                    if old is not None:
                        registry.register_factor(old)
                if engine is not None:
                    engine.reload()
                if rollback_errors:
                    logger.error("resource import rollback incomplete: %s", rollback_errors)
                    raise ResourceBundleError(f"导入失败，部分文件回滚失败: {'; '.join(rollback_errors)}") from exc
                raise ResourceBundleError(f"导入失败，已回滚本次修改: {exc}") from exc
        return {"imported": len(writes), "reused": len(order) - len(writes), "counts": {kind: sum(resources[k]["kind"] == kind for k in order) for kind in ("strategy", "signal", "factor")},
                "resources": [k for k in order if resources[k]["kind"] in ("strategy", "signal", "factor")], "changed": sorted({*[i["key"] for i in writes], *[f"factor:{fid}" for fid in changed_specs]}),
                "drafts": [resources[k]["id"] for k in order if resources[k]["kind"] == "strategy" and resources[k]["draft"]]}
    except ResourceBundleError:
        raise
    except (ValueError, SyntaxError, TypeError, KeyError, OSError, UnicodeDecodeError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ResourceBundleError(str(exc)) from exc


# 旧策略包接口的兼容适配。

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
        "app_version": config_backup._app_version(),
        "strategy_id": summary.strategy_id,
        "label": summary.label,
        "counts": summary.counts,
        "warnings": summary.warnings,
        "entries": [
            {"path": rel, "kind": config_backup._classify(PurePosixPath(rel), data_dir)[0]} for rel, _ in items
        ],
    }
    return _archive(dict(items), manifest), f"tsp-strategy-{summary.strategy_id}.zip", summary


def inspect_bundle(data: bytes) -> dict[str, Any]:
    """旧接口仅映射响应, 依赖检查复用当前资源预检。"""
    parsed = parse_bundle(data)
    try:
        resources, _ = _read_uploads([("strategy.zip", data)])
        _analyze(resources)
    except (ValueError, SyntaxError, TypeError, KeyError, UnicodeDecodeError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise StrategyBundleError(str(exc)) from exc
    by_path = {item["path"]: item for item in resources.values()}
    return {
        "strategy_id": parsed.manifest.get("strategy_id"),
        "label": parsed.manifest.get("label"),
        "created_at": parsed.manifest.get("created_at"),
        "counts": parsed.manifest.get("counts", {}),
        "warnings": parsed.manifest.get("warnings", []),
        "entries": [{"path": name, "kind": category, "id": PurePosixPath(name).stem,
                     "action": "conflict" if target.exists() else "create",
                     "bytes": len(by_path[name]["payload"])} for name, category, target in parsed.entries],
        # 旧接口把未命中的 signal_* 视为安装自带信号, 只提示缺失的自定义输入。
        "missing_deps": sorted({issue for item in resources.values() for issue in item["issues"]
                                if issue.startswith("缺少依赖: ") and not issue.removeprefix("缺少依赖: ").startswith("signal_")}),
    }


def apply_bundle(data: bytes, *, allow_overwrite: bool = False) -> dict[str, Any]:
    """旧接口保留逐文件跳过/失败统计; 原子整批导入仍由 apply_resources 处理。"""
    parsed = parse_bundle(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        items = {name: config_backup._read_entry(archive, name) for name, _, _ in parsed.entries}
    counts = {category: sum(kind == category for _, kind, _ in parsed.entries) for category in BUNDLE_CATEGORIES}
    manifest = {"format": config_backup.BACKUP_FORMAT, "version": config_backup.BACKUP_VERSION,
                "created_at": parsed.manifest.get("created_at"), "app_version": parsed.manifest.get("app_version", ""),
                "include_secrets": False,
                "categories": [{"key": key, "label": config_backup._CATEGORY_LABELS[key], "count": count}
                               for key, count in counts.items() if count]}
    return config_backup.apply_archive(_archive(items, manifest), allow_overwrite=allow_overwrite, categories=BUNDLE_CATEGORIES)
