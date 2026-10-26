"""定义契约 (P1b) — signal/factor/未来 kind 的统一注册与存储基座。

目标契约: {id, kind, version, params, inputs(依赖列), expression, meta}。
本模块承载其中与计算无关的公共半边:

  - JsonDefinitionStore: 四处同型 JSON 持久化 (自定义信号/因子/监控规则/
    批次) 的单一实现 — 原子写 + 损坏跳过告警 + 目录 mtime 签名。
    失效广播/文件锁按架构路线 (docs/ext-architecture-plan.md) 在 P2
    物化阶段叠加, 此处不预建。
  - DefinitionKind + register_kind: 每种定义声明 id 规则/校验/依赖提取/
    存储; validate_definition / definition_dependencies 是统一分发入口。
    新增一种 kind = 注册一个描述器, 即复用同一校验/注册/依赖路径,
    不再平行实现持久化与分发。

契约视图 (params/inputs/expression 归一化渲染) 的消费方 — 统一选择器、
AI 提示词、扩展包分发 — 在 P4 才存在, 届时再加。
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)


class JsonDefinitionStore:
    """``data_dir/<subdir>`` 下的 JSON 定义存储 — load/save/delete/签名单一实现。

    差异点参数化:
      - glob_pattern: 文件匹配 (批次只认 ``lot_*.json``, 其余 ``*.json``);
      - label: 日志名称 (custom signal / custom factor / ...);
      - normalize: 读取后的逐条归一 (监控规则/批次的历史默认值回填)。
    损坏或归一失败的文件跳过并告警, 不影响其余条目 (插件隔离要求)。
    """

    def __init__(
        self,
        subdir: str,
        *,
        glob_pattern: str = "*.json",
        label: str = "definition",
        normalize: Callable[[dict], dict] | None = None,
    ) -> None:
        self.subdir = subdir
        self.glob_pattern = glob_pattern
        self.label = label
        self._normalize = normalize

    def dir(self, data_dir: Path) -> Path:
        d = data_dir / self.subdir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path(self, data_dir: Path, def_id: str) -> Path:
        return self.dir(data_dir) / f"{def_id}.json"

    def fingerprint(self, data_dir: Path) -> tuple:
        """目录内匹配文件的 (文件名, mtime) 签名 — 创建/删除/编辑都会变化。"""
        try:
            return tuple(
                sorted((f.name, f.stat().st_mtime_ns) for f in self.dir(data_dir).glob(self.glob_pattern))
            )
        except OSError:
            return ()

    def _read(self, file: Path) -> dict:
        item = json.loads(file.read_text(encoding="utf-8"))
        return self._normalize(item) if self._normalize is not None else item

    def load_all(self, data_dir: Path) -> list[dict]:
        out: list[dict] = []
        for f in sorted(self.dir(data_dir).glob(self.glob_pattern)):
            try:
                out.append(self._read(f))
            except Exception as e:  # noqa: BLE001 — 损坏隔离: 单文件失败只跳过告警
                logger.warning("%s load failed %s: %s", self.label, f.name, e)
        return out

    def load_one(self, data_dir: Path, def_id: str) -> dict | None:
        p = self.path(data_dir, def_id)
        if not p.exists():
            return None
        try:
            return self._read(p)
        except Exception as e:  # noqa: BLE001 — 同 load_all: 损坏文件返回 None 不抛
            logger.warning("%s load failed %s: %s", self.label, def_id, e)
            return None

    def save_one(self, data_dir: Path, definition: dict) -> None:
        atomic_write_text(
            self.path(data_dir, str(definition["id"])),
            json.dumps(definition, ensure_ascii=False, indent=2),
        )

    def delete_one(self, data_dir: Path, def_id: str) -> bool:
        p = self.path(data_dir, def_id)
        if p.exists():
            p.unlink()
            return True
        return False


@dataclass(frozen=True)
class DefinitionKind:
    """一种定义 kind 的契约描述: 存储 + id 规则 + 校验 + 依赖提取。

    validate 非法抛 ValueError (中文信息由各领域校验器给出);
    dependencies 返回该定义的输入列集合 — 提取器必须复用既有单一实现
    (信号: 编译表达式 root_names; 因子: DSL 编译依赖), 不引入第二套逻辑。
    id_pattern 是命名约束声明, 供表单/导入侧预检, 分发入口不做二次校验。
    """

    kind: str
    store: JsonDefinitionStore
    id_pattern: re.Pattern
    validate: Callable[[dict], None]
    dependencies: Callable[[dict], frozenset[str]] | None = None


_KINDS: dict[str, DefinitionKind] = {}

# 内置 kind → 注册所在模块。get_kind 未命中时惰性导入后重查 — 消费方
# (备份/导入流/测试) 不必预导入领域模块也能走统一分发入口。
_KIND_MODULES: dict[str, str] = {
    "signal": "app.strategy.custom_signals",
    "factor": "app.factors.store",
}


def register_kind(descriptor: DefinitionKind) -> None:
    """注册 kind; 同一 kind 重复注册同一描述器幂等, 换描述器拒绝 (fail-closed)。"""
    existing = _KINDS.get(descriptor.kind)
    if existing is not None and existing is not descriptor:
        raise ValueError(f"definition kind 已注册且描述器不同: {descriptor.kind}")
    _KINDS[descriptor.kind] = descriptor


def get_kind(kind: str) -> DefinitionKind:
    descriptor = _KINDS.get(kind)
    if descriptor is None and kind in _KIND_MODULES:
        import importlib

        importlib.import_module(_KIND_MODULES[kind])
        descriptor = _KINDS.get(kind)
    if descriptor is None:
        raise ValueError(f"未知 definition kind: {kind} (已注册: {sorted(_KINDS)})")
    return descriptor


def registered_kinds() -> tuple[str, ...]:
    """已注册 kind 名单; 内置 kind 先经惰性导入落实 (消费方无需预导入领域模块)。"""
    for kind in list(_KIND_MODULES):
        if kind not in _KINDS:
            get_kind(kind)
    return tuple(sorted(_KINDS))


def validate_definition(kind: str, definition: dict) -> None:
    """统一校验分发入口; 非法抛 ValueError。"""
    get_kind(kind).validate(definition)


def definition_dependencies(kind: str, definition: dict) -> frozenset[str]:
    """统一依赖提取分发入口 — 定义 → 输入列集合。

    无提取器的 kind (纯记录类定义) 返回空集。
    """
    extractor = get_kind(kind).dependencies
    if extractor is None:
        return frozenset()
    return extractor(definition)


# ── 统一目录与导入冒烟 (P4) ──────────────────────────────
# 「定义即扩展」的消费面: 一份跨 kind 的目录 (统一选择器的数据半边) 和
# 导入即冒烟 (校验 + 编译 + 依赖可解析, 无 IO) — 备份导入 / AI 生成 /
# 手工创建统一调用, 导入完成即知道定义能不能算。

# 回测恒载的分组/日期列 — 依赖提取的 root_names 会带上 .over("symbol")
# 的 symbol, 它们不参与"未知依赖"判定。
_ALWAYS_LOADED_COLUMNS = frozenset({"symbol", "date"})


def _known_input_columns() -> frozenset[str]:
    """定义可引用的全部输入列 (依赖可解析性判据): enriched 存储/指标列 ∪
    注册表因子 ∪ 字符串扩展字段。"""
    from app.indicators.pipeline import ENRICHED_STORAGE_COLS, INDICATOR_COLUMNS
    from app.strategy import custom_signals

    return frozenset(
        set(ENRICHED_STORAGE_COLS) | set(INDICATOR_COLUMNS) | set(custom_signals.allowed_fields())
    )


class _IssueCapture(logging.Handler):
    """冒烟期捕获 custom_signals 编译告警 (build_expressions 失败只告警不抛)。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def smoke_definition(kind: str, definition: dict) -> dict:
    """导入冒烟 (静态层): validate → 编译 → 依赖可解析。

    返回 {ok, issues: [{code, message}], dependencies, warmup_bars};
    ok=False 时 issues 至少一条, 不会静默通过。
    """
    issues: list[dict[str, str]] = []
    try:
        validate_definition(kind, definition)
    except ValueError as e:
        return {"ok": False, "issues": [{"code": "V001", "message": str(e)}],
                "dependencies": [], "warmup_bars": None}

    dependencies: frozenset[str] = frozenset()
    warmup_bars: int | None = None
    if kind == "signal":
        from app.strategy import custom_signals

        capture = _IssueCapture()
        signal_logger = logging.getLogger("app.strategy.custom_signals")
        signal_logger.addHandler(capture)
        try:
            if definition.get("timeframe") == custom_signals.TIMEFRAME_INTRADAY:
                exprs = custom_signals.build_intraday_expressions([definition])
                column = custom_signals.intraday_column_name(str(definition.get("id", "")))
            else:
                exprs = custom_signals.build_expressions([definition])
                column = custom_signals.resolve_column(definition)
        finally:
            signal_logger.removeHandler(capture)
        if column not in exprs:
            issues.append({
                "code": "C001",
                "message": capture.messages[0] if capture.messages else "信号编译失败",
            })
        dependencies = definition_dependencies("signal", definition)
    else:
        from app.factors import store as factor_store

        try:
            spec = factor_store.to_spec(definition)
        except ValueError as e:  # validate 已过, 这里只剩环境性失败
            return {"ok": False, "issues": [{"code": "V002", "message": str(e)}],
                    "dependencies": [], "warmup_bars": None}
        dependencies = spec.dependencies
        warmup_bars = spec.warmup_bars

    unknown = sorted(
        dependencies - _known_input_columns() - _ALWAYS_LOADED_COLUMNS
    )
    if unknown:
        issues.append({"code": "W001", "message": f"依赖列不在已知输入列: {unknown}"})
    return {
        "ok": not issues,
        "issues": issues,
        "dependencies": sorted(dependencies),
        "warmup_bars": warmup_bars,
    }


def smoke_stored(kind: str, definition_id: str, data_dir: Path) -> dict:
    """按 id 复检已存定义 (统一选择器逐条冒烟); 不存在时 fail-closed 报错。"""
    from fastapi import HTTPException

    from app.factors import store as factor_store
    from app.strategy import custom_signals

    if kind == "signal":
        definitions = custom_signals.load_all(data_dir)
    elif kind == "factor":
        definitions = factor_store.load_all(data_dir)
    else:
        raise HTTPException(status_code=422, detail=f"未知 kind: {kind}")
    for definition in definitions:
        if definition.get("id") == definition_id:
            return smoke_definition(kind, definition)
    raise HTTPException(status_code=404, detail=f"定义不存在: {kind}/{definition_id}")


def catalog(data_dir: Path) -> list[dict]:
    """全部已存定义的统一目录视图 (signal + factor), 供统一选择器消费。"""
    from app.factors import store as factor_store
    from app.strategy import custom_signals

    items: list[dict] = []
    for sig in custom_signals.load_all(data_dir):
        items.append({
            "kind": "signal",
            "id": sig.get("id"),
            "label": sig.get("name") or sig.get("id"),
            "enabled": sig.get("enabled") is not False,
            "timeframe": sig.get("timeframe") or "daily",
            "lang": "conditions",
            "dependencies": sorted(definition_dependencies("signal", sig)),
        })
    for definition in factor_store.load_all(data_dir):
        item = {
            "kind": "factor",
            "id": definition.get("id"),
            "label": definition.get("label") or definition.get("id"),
            "lang": str(definition.get("lang", "dsl")),
            "status": str(definition.get("status", "draft")),
        }
        try:
            spec = factor_store.to_spec(definition)
            item.update({
                "dependencies": sorted(spec.dependencies),
                "warmup_bars": spec.warmup_bars,
                "group": spec.group,
            })
        except ValueError as e:
            item["error"] = str(e)  # 目录不因单个坏定义失败 (隔离要求)
        items.append(item)
    items.sort(key=lambda item: (str(item["kind"]), str(item["id"])))
    return items
