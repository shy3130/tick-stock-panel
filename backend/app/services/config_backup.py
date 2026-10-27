"""配置备份 — 导出/导入用户配置为单个 ZIP, 解决切换设备配置丢失。

备份文件 = manifest.json + 按 data_dir 相对路径归档的用户配置文件。
保持各 store 的原始文件格式 (策略 .py、规则/偏好 JSON、自选 parquet),
不做任何格式转换, 避免"每类资产一套序列化"的平行实现。

导入是高风险文件操作 (CONTRIBUTING §8), 全程 fail-closed:
- ZIP 条目名只接受白名单路径形态 (_classify), 拒绝绝对路径/`..`/反斜杠;
- 防炸弹: 条目数、单文件、解压总量均有上限, 实际读取按上限截断校验;
- 策略源码写入前过与引擎一致的 AST 安全校验, 不安全的条目跳过并报错;
- 文件类冲突默认跳过, 显式 allow_overwrite 才覆盖; 偏好/密钥走 merge 不整替。
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

BACKUP_FORMAT = "tick-config-backup"
BACKUP_VERSION = 1

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024   # 上传压缩包上限
MAX_ENTRIES = 2_000                     # 条目数上限
MAX_FILE_BYTES = 10 * 1024 * 1024       # 单文件解压上限
MAX_TOTAL_BYTES = 64 * 1024 * 1024      # 全包解压总量上限

# 导出默认脱敏的偏好键 — webhook URL 本身含 token, 与 secret 同级处理。
SENSITIVE_PREFERENCE_KEYS = (
    "feishu_webhook_url",
    "feishu_webhook_secret",
    "wecom_webhook_url",
    "custom_webhook_url",
    "wecom_bot_id",
    "wecom_bot_secret",
)
# 监控规则内 legacy 的 per-rule webhook 字段 (URL 含 token)。
SENSITIVE_RULE_FIELDS = ("webhook_url",)

_STRATEGY_SOURCES = ("custom", "ai", "composite")

# 类别注册表 — 导出枚举、导入校验、结果展示共用一份。
_CATEGORY_LABELS: dict[str, str] = {
    "strategies": "策略源码",
    "strategy_overrides": "策略调参",
    "custom_signals": "自定义信号",
    "custom_factors": "自定义因子",
    "position_policies": "仓位策略",
    "monitor_rules": "监控规则",
    "watchlist": "自选股",
    "lots": "批量持仓",
    "paper": "模拟盘账户",
    "research_candidates": "挖掘候选",
    "data_sources": "自定义数据源",
    "analysis_menus": "分析菜单",
    "ext_pages": "扩展页面",
    "ext_data": "扩展数据集配置",
    "sound_packs": "语音包",
    "history": "历史记录",
    "preferences": "偏好设置",
    "secrets": "密钥",
    "auth": "访问凭证",
}
# 全部类别键 (导出/导入按类别勾选的合法值域)。
CATEGORY_KEYS = frozenset(_CATEGORY_LABELS)
# 敏感类别: 默认不导出 (UI 单独警示)。
SENSITIVE_CATEGORIES = frozenset({"secrets", "auth"})
# 与 api/settings.py 的 _SOUND_PACK_EXTS 保持一致 (service 不反向依赖 api 层)。
_SOUND_PACK_EXTS = frozenset({".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus", ".webm"})
# 结果输出顺序 = 重要性顺序 (策略 → 监控 → …, 敏感类别放末尾)。
_CATEGORY_ORDER = (
    "strategies",
    "strategy_overrides",
    "custom_signals",
    "custom_factors",
    "position_policies",
    "monitor_rules",
    "watchlist",
    "lots",
    "paper",
    "research_candidates",
    "data_sources",
    "analysis_menus",
    "ext_pages",
    "ext_data",
    "sound_packs",
    "history",
    "preferences",
    "secrets",
    "auth",
)

# 文件名白名单: 字母/数字/下划线/连字符 (含中文), 不允许 `.` 开头、路径分隔符。
_NAME_RE = re.compile(r"^[\w\-]{1,128}$", re.UNICODE)

# 导入后需要上层 (API 层) 触发重载的状态类别。
_RELOAD_HINT_KEYS = ("strategies", "monitor_rules", "data_sources", "watchlist")


class ConfigBackupError(ValueError):
    """备份文件不可接受 (格式/安全/超限)。API 层映射为 400。"""


def _data_dir() -> Path:
    from app.config import settings

    return settings.data_dir


def _app_version() -> str:
    """manifest 记录来源版本, 打包环境缺 VERSION 文件时留空即可。"""
    try:
        return (Path(__file__).resolve().parents[3] / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class _Entry:
    """一个通过白名单校验的备份条目。"""

    name: str        # zip 内条目名 (data_dir 相对 POSIX 路径)
    category: str
    target: Path     # 本机落盘绝对路径


# ── 路径归类: zip 条目名 → (类别, 目标路径); 不在备份范围返回 None ──────────

def _classify(rel: PurePosixPath, data_dir: Path) -> tuple[str, Path] | None:
    def _name_ok(name: str, suffix: str) -> bool:
        return name.endswith(suffix) and bool(_NAME_RE.match(name[: -len(suffix)]))

    def _name_ext_ok(name: str, suffixes: frozenset[str]) -> bool:
        return any(_name_ok(name, s) for s in suffixes)

    parts = rel.parts
    if (
        len(parts) == 3
        and parts[0] == "strategies"
        and parts[1] in _STRATEGY_SOURCES
        and _name_ok(parts[2], ".py")
    ):
        return "strategies", data_dir / Path(*parts)
    if (
        len(parts) == 3
        and parts[0] == "user_data"
        and parts[1] == "position_policies"
        and _name_ok(parts[2], ".py")
    ):
        return "position_policies", data_dir / Path(*parts)
    if (
        len(parts) == 3
        and parts[0] == "user_data"
        and parts[1] in ("strategy_overrides", "custom_signals", "custom_factors", "monitor_rules")
        and _name_ok(parts[2], ".json")
    ):
        return parts[1], data_dir / Path(*parts)
    if (
        len(parts) == 3
        and parts[0] == "user_data"
        and parts[1] == "lots"
        and parts[2].startswith("lot_")
        and _name_ok(parts[2], ".json")
    ):
        return "lots", data_dir / Path(*parts)
    if len(parts) == 2 and parts[0] == "user_data":
        name = parts[1]
        if name == "preferences.json":
            return "preferences", data_dir / "user_data" / "preferences.json"
        if name == "secrets.json":
            return "secrets", data_dir / "user_data" / "secrets.json"
        if name == "auth.json":
            return "auth", data_dir / "user_data" / "auth.json"
        if name in ("watchlist.parquet", "watchlist_groups.json"):
            return "watchlist", data_dir / "user_data" / name
        if name == "research_candidates.json":
            return "research_candidates", data_dir / "user_data" / name
        if name in ("alerts.jsonl", "assistant_sessions.json"):
            return "history", data_dir / "user_data" / name
    if len(parts) == 2 and parts[0] == "data_sources" and (_name_ok(parts[1], ".yaml") or _name_ok(parts[1], ".yml")):
        return "data_sources", data_dir / Path(*parts)
    if len(parts) == 2 and parts[0] == "analysis_menus" and _name_ok(parts[1], ".json"):
        return "analysis_menus", data_dir / Path(*parts)
    if len(parts) == 2 and parts[0] == "sound_packs" and _name_ext_ok(parts[1], _SOUND_PACK_EXTS):
        return "sound_packs", data_dir / Path(*parts)
    if len(parts) == 3 and parts[0] == "ext_data" and parts[2] == "config.json" and bool(_NAME_RE.match(parts[1])):
        return "ext_data", data_dir / Path(*parts)
    if (
        len(parts) == 3
        and parts[0] == "ext_pages"
        and bool(_NAME_RE.match(parts[1]))
        and parts[2] in ("page.json", "bundle.js")
    ):
        return "ext_pages", data_dir / Path(*parts)
    # 模拟盘账户: paper/accounts/{acc}/[nav|orders|auto_rules/...]/{file}.json|.jsonl
    # 中间目录段是纯名称 (nav/orders), 只有末段带扩展名。
    if (
        4 <= len(parts) <= 5
        and parts[0] == "paper"
        and parts[1] == "accounts"
        and all(bool(_NAME_RE.match(p)) for p in parts[2:-1])
        and _name_ext_ok(parts[-1], frozenset({".json", ".jsonl"}))
    ):
        return "paper", data_dir / Path(*parts)
    return None


def _safe_rel(entry_name: str) -> PurePosixPath:
    """校验条目名可安全转换为相对路径, 否则整体拒绝。"""
    if not entry_name or entry_name.endswith("/"):
        raise ConfigBackupError(f"非法条目: {entry_name!r}")
    if "\\" in entry_name or entry_name.startswith("/") or ":" in entry_name:
        raise ConfigBackupError(f"非法条目路径: {entry_name!r}")
    rel = PurePosixPath(entry_name)
    if rel.is_absolute() or any(p in ("", ".", "..") for p in rel.parts):
        raise ConfigBackupError(f"非法条目路径: {entry_name!r}")
    return rel


# ── 导出 ────────────────────────────────────────────────

def _collect_backup_files(data_dir: Path) -> list[Path]:
    out: list[Path] = []

    def _glob(directory: Path, pattern: str) -> None:
        if directory.is_dir():
            out.extend(sorted(directory.glob(pattern)))

    for sub in _STRATEGY_SOURCES:
        _glob(data_dir / "strategies" / sub, "*.py")
    for sub in ("strategy_overrides", "custom_signals", "custom_factors", "monitor_rules"):
        _glob(data_dir / "user_data" / sub, "*.json")
    _glob(data_dir / "user_data" / "position_policies", "*.py")
    _glob(data_dir / "user_data" / "lots", "lot_*.json")
    for name in (
        "watchlist.parquet", "watchlist_groups.json", "preferences.json",
        "research_candidates.json", "alerts.jsonl", "assistant_sessions.json", "auth.json",
    ):
        p = data_dir / "user_data" / name
        if p.is_file():
            out.append(p)
    _glob(data_dir / "data_sources", "*.yaml")
    _glob(data_dir / "data_sources", "*.yml")
    _glob(data_dir / "analysis_menus", "*.json")
    if (data_dir / "sound_packs").is_dir():
        for f in sorted((data_dir / "sound_packs").iterdir()):
            if f.is_file() and f.suffix.lower() in _SOUND_PACK_EXTS:
                out.append(f)
    ext_base = data_dir / "ext_data"
    if ext_base.is_dir():
        for sub in sorted(ext_base.iterdir()):
            cfg = sub / "config.json"
            if sub.is_dir() and cfg.is_file():
                out.append(cfg)
    pages_base = data_dir / "ext_pages"
    if pages_base.is_dir():
        for sub in sorted(pages_base.iterdir()):
            if not sub.is_dir():
                continue
            for fname in ("page.json", "bundle.js"):
                p = sub / fname
                if p.is_file():
                    out.append(p)
    accounts_base = data_dir / "paper" / "accounts"
    if accounts_base.is_dir():
        for pattern in ("*.json", "*.jsonl"):
            out.extend(sorted(accounts_base.rglob(pattern)))
    # 只带走 classify 认可的文件, 保证导出/导入白名单严格对称。
    return [
        p for p in out
        if _classify(PurePosixPath(p.relative_to(data_dir).as_posix()), data_dir) is not None
    ]


def _export_payload(path: Path, category: str, *, include_secrets: bool) -> bytes | None:
    """读取文件内容; 偏好/规则在不包含密钥时做脱敏。不可读/超限则跳过并告警。"""
    try:
        raw = path.read_bytes()
    except OSError as e:
        logger.warning("config backup skip unreadable %s: %s", path.name, e)
        return None
    if len(raw) > MAX_FILE_BYTES:
        logger.warning("config backup skip oversized %s (%d bytes)", path.name, len(raw))
        return None
    if not include_secrets:
        if category == "preferences":
            data = json.loads(raw)
            for key in SENSITIVE_PREFERENCE_KEYS:
                data.pop(key, None)
            return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        if category == "monitor_rules":
            data = json.loads(raw)
            for key in SENSITIVE_RULE_FIELDS:
                data.pop(key, None)
            return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    return raw


def _normalize_selection(categories: Iterable[str] | None) -> frozenset[str] | None:
    """类别勾选归一: None → 全量 (不含密钥, 安全默认); 否则取与注册表的交集。

    交集可以为空 (API 直调传了非法键/全不选) → fail-closed, 导出只剩 manifest、
    导入不写任何文件, 绝不静默回退成"全选"。
    """
    if categories is None:
        return None
    return frozenset(k for k in categories if k in _CATEGORY_LABELS)


def build_backup(*, categories: Iterable[str] | None = None) -> tuple[bytes, str]:
    """打包用户配置, 返回 (zip 字节, 建议文件名)。

    categories=None 导出全部 (不含密钥); 显式包含 "secrets" 才打包密钥。
    """
    selected = _normalize_selection(categories)
    include_secrets = selected is not None and "secrets" in selected
    data_dir = _data_dir()
    counts: dict[str, int] = {}
    buf = io.BytesIO()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in _collect_backup_files(data_dir):
            rel = path.relative_to(data_dir).as_posix()
            category, _ = _classify(PurePosixPath(rel), data_dir)  # collect 已过滤, 必非 None
            if selected is not None and category not in selected:
                continue
            # 敏感类别 (密钥/访问凭证) 只在显式勾选时打包, 默认导出不带。
            if category in SENSITIVE_CATEGORIES and not (
                selected is not None and category in selected
            ):
                continue
            payload = _export_payload(path, category, include_secrets=include_secrets)
            if payload is None:
                continue
            zf.writestr(rel, payload)
            counts[category] = counts.get(category, 0) + 1
        if include_secrets:
            secrets_path = data_dir / "user_data" / "secrets.json"
            if secrets_path.is_file():
                zf.writestr("user_data/secrets.json", secrets_path.read_bytes())
                counts["secrets"] = counts.get("secrets", 0) + 1
        manifest = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "created_at": now.isoformat(),
            "app_version": _app_version(),
            "include_secrets": include_secrets,
            "categories": [
                {"key": key, "label": _CATEGORY_LABELS[key], "count": counts[key]}
                for key in _CATEGORY_ORDER
                if key in counts
            ],
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    logger.info(
        "config backup built: include_secrets=%s categories=%s",
        include_secrets,
        {k: counts[k] for k in _CATEGORY_ORDER if k in counts},
    )
    return buf.getvalue(), f"tsp-config-backup-{now:%Y%m%d-%H%M}.zip"


# ── 解析与校验 (inspect / apply 共用) ─────────────────────

@dataclass(frozen=True)
class _ParsedArchive:
    manifest: dict[str, Any]
    entries: list[_Entry]
    out_of_scope: list[str]  # 结构安全但不在备份范围内的条目 (忽略, 计数上报)


def _parse_archive(data: bytes) -> _ParsedArchive:
    if not data:
        raise ConfigBackupError("空文件")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ConfigBackupError(f"备份文件超过 {MAX_ARCHIVE_BYTES // 1024 // 1024}MB 上限")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ConfigBackupError("不是有效的 ZIP 备份文件") from e
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            raise ConfigBackupError(f"备份条目数超过上限 ({len(infos)} > {MAX_ENTRIES})")
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError as e:
            raise ConfigBackupError("备份缺少 manifest.json") from e
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ConfigBackupError(f"manifest.json 解析失败: {e}") from e
        if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
            raise ConfigBackupError("不是本应用的配置备份文件")
        version = manifest.get("version")
        if version != BACKUP_VERSION:
            raise ConfigBackupError(f"不支持的备份版本 {version!r} (当前支持 {BACKUP_VERSION})")

        data_dir = _data_dir()
        entries: list[_Entry] = []
        out_of_scope: list[str] = []
        total = 0
        for info in infos:
            if info.filename == "manifest.json":
                continue
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ConfigBackupError(f"备份解压总量超过 {MAX_TOTAL_BYTES // 1024 // 1024}MB 上限")
            if info.file_size > MAX_FILE_BYTES:
                raise ConfigBackupError(f"条目 {info.filename} 超过单文件 {MAX_FILE_BYTES // 1024 // 1024}MB 上限")
            rel = _safe_rel(info.filename)
            cls = _classify(rel, data_dir)
            if cls is None:
                out_of_scope.append(info.filename)
                continue
            entries.append(_Entry(name=info.filename, category=cls[0], target=cls[1]))
    return _ParsedArchive(manifest=manifest, entries=entries, out_of_scope=out_of_scope)


def _read_entry(zf: zipfile.ZipFile, name: str) -> bytes:
    """按上限截断读取, 防止伪造 central directory 声明小尺寸的炸弹。"""
    with zf.open(name) as f:
        payload = f.read(MAX_FILE_BYTES + 1)
    if len(payload) > MAX_FILE_BYTES:
        raise ConfigBackupError(f"条目 {name} 实际大小超过 {MAX_FILE_BYTES // 1024 // 1024}MB 上限")
    return payload


def inspect_archive(data: bytes) -> dict[str, Any]:
    """解析备份并给出预览: 各类别条目数、与本地冲突数。不写任何文件。"""
    parsed = _parse_archive(data)
    cats: dict[str, dict[str, Any]] = {}
    for entry in parsed.entries:
        c = cats.setdefault(
            entry.category,
            {"key": entry.category, "label": _CATEGORY_LABELS[entry.category], "count": 0, "conflicts": 0},
        )
        c["count"] += 1
        # 偏好/密钥是 merge 语义, 不存在冲突概念。
        if entry.category not in ("preferences", "secrets") and entry.target.exists():
            c["conflicts"] += 1
    return {
        "created_at": parsed.manifest.get("created_at", ""),
        "app_version": str(parsed.manifest.get("app_version", "")),
        # 以包内是否真的有 secrets 条目为准 (manifest 声明只作参考)。
        "has_secrets": any(e.category == "secrets" for e in parsed.entries),
        "categories": [cats[k] for k in _CATEGORY_ORDER if k in cats],
        "total_files": len(parsed.entries),
        "conflict_files": sum(c["conflicts"] for c in cats.values()),
        "out_of_scope_files": len(parsed.out_of_scope),
    }


# ── 导入 ────────────────────────────────────────────────

def _validate_strategy_code(code: str, *, is_dependency: bool, dep_names: frozenset[str]) -> None:
    """与 engine._load_file 相同口径的 AST 安全校验 — 备份可能被篡改, 不因
    '是自己导出的' 免检; 写盘前拦截, 避免给引擎留下每次 reload 都报错的毒文件。"""
    from app.strategy.ai_generator import AIStrategyGenerator

    if is_dependency:
        AIStrategyGenerator._validate_safety(
            code,
            extra_allowed_import_modules=frozenset({"collections.abc", "types", "typing"}),
        )
    else:
        AIStrategyGenerator._validate_safety(code, extra_allowed_import_modules=dep_names)


def _validate_json_object(payload: bytes, *, what: str) -> dict:
    try:
        doc = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ConfigBackupError(f"{what} JSON 解析失败: {e}") from e
    if not isinstance(doc, dict):
        raise ConfigBackupError(f"{what}内容必须是 JSON 对象")
    return doc


def _validate_payload(entry: _Entry, payload: bytes) -> None:
    """写盘前的内容级校验 (策略的安全校验独立处理)。"""
    name = entry.target.name
    if entry.category in ("sound_packs",):
        return  # 音频二进制, 无从校验内容, 路径白名单已限死
    if entry.category == "ext_pages" and name == "bundle.js":
        return  # 编译产物无法 AST 校验; 仅导入可信备份 (说明书已注明)
    if name.endswith(".jsonl"):
        # 逐行 JSON (成交台账/净值/告警); 空行容忍, 坏行 fail-closed。
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ConfigBackupError(f"{name} 不是有效 UTF-8: {e}") from e
        for i, line in enumerate(text.splitlines()):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                raise ConfigBackupError(f"{name} 第 {i + 1} 行不是有效 JSON: {e}") from e
        return
    if entry.category == "watchlist" and name.endswith(".parquet"):
        import polars as pl

        try:
            pl.read_parquet_schema(io.BytesIO(payload))
        except Exception as e:  # 转换为备份错误后 re-raise, 不算静默吞异常
            raise ConfigBackupError(f"自选股 parquet 无法解析: {e}") from e
        return
    if entry.category == "data_sources":
        try:
            doc = yaml.safe_load(payload)
        except yaml.YAMLError as e:
            raise ConfigBackupError(f"自定义数据源 YAML 解析失败: {e}") from e
        if not isinstance(doc, dict):
            raise ConfigBackupError("自定义数据源配置必须是 YAML 对象")
        return
    if entry.category in ("watchlist", "research_candidates") and (
        name == "watchlist_groups.json" or name == "research_candidates.json"
    ):
        # 分组/候选清单是 JSON 数组, 只要求可解析。
        try:
            json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ConfigBackupError(f"JSON 解析失败: {e}") from e
        return
    # 其余 (规则/信号/因子/菜单/调参/扩展配置) 均为 JSON 对象。
    _validate_json_object(payload, what=f"{_CATEGORY_LABELS[entry.category]} ")


def _write_bytes_atomic(target: Path, payload: bytes) -> None:
    """二进制原子写: tmp + replace, 与 fs_utils.atomic_write_text 同语义。"""
    from app.tickflow.repository import replace_with_retry

    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(payload)
    replace_with_retry(tmp, target)


# 文本类后缀走 atomic_write_text; 其余 (parquet/音频等二进制) 走字节原子写。
_TEXT_SUFFIXES = frozenset({".py", ".json", ".jsonl", ".yaml", ".yml", ".js"})


def _write_payload(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.lower() in _TEXT_SUFFIXES:
        atomic_write_text(target, payload.decode("utf-8"))
    else:
        _write_bytes_atomic(target, payload)


def apply_archive(data: bytes, *, allow_overwrite: bool = False, categories: Iterable[str] | None = None) -> dict[str, Any]:
    """把备份写入本机。categories 只导入勾选的类别 (None=全部); 文件类默认冲突
    跳过; 偏好/密钥 merge。逐条容错, 单条失败不影响其余条目, 结果按类别汇总。
    返回 reload_hints 供 API 层刷新引擎。"""
    parsed = _parse_archive(data)
    selected = _normalize_selection(categories)
    if selected is None:
        entries = parsed.entries
        skipped_unselected = 0
    else:
        entries = [e for e in parsed.entries if e.category in selected]
        skipped_unselected = len(parsed.entries) - len(entries)
    results: dict[str, dict[str, Any]] = {}
    reload_hints = {key: False for key in _RELOAD_HINT_KEYS}
    prefs_payload: dict | None = None
    secrets_payload: dict | None = None

    def _result(category: str) -> dict[str, Any]:
        return results.setdefault(
            category,
            {"key": category, "label": _CATEGORY_LABELS[category],
             "imported": 0, "skipped_conflict": 0, "failed": []},
        )

    # 第一遍: 截断读全部条目 + 冲突过滤 + 偏好/密钥提取。
    # 策略条目先攒批 — 同目录 _*.py 依赖要进主文件的 import 白名单
    # (与 engine._load_file 的 dependency glob 口径一致), 校验必须看全批。
    strategy_codes: dict[_Entry, str] = {}
    policy_codes: dict[_Entry, str] = {}
    plain_entries: list[tuple[_Entry, bytes]] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:  # _parse_archive 已校验过, 理论不可达
        raise ConfigBackupError("不是有效的 ZIP 备份文件") from e
    with zf:
        for entry in entries:
            try:
                payload = _read_entry(zf, entry.name)
            except ConfigBackupError as e:
                _result(entry.category)["failed"].append({"file": entry.name, "error": str(e)})
                continue
            if entry.category == "preferences":
                prefs_payload = _validate_json_object(payload, what="偏好设置 ")
                continue
            if entry.category == "secrets":
                secrets_payload = _validate_json_object(payload, what="密钥文件 ")
                continue
            if entry.target.exists() and not allow_overwrite:
                _result(entry.category)["skipped_conflict"] += 1
                continue
            if entry.category == "strategies":
                try:
                    strategy_codes[entry] = payload.decode("utf-8")
                except UnicodeDecodeError as e:
                    _result("strategies")["failed"].append(
                        {"file": entry.name, "error": f"策略文件不是有效 UTF-8: {e}"}
                    )
            elif entry.category == "position_policies":
                try:
                    policy_codes[entry] = payload.decode("utf-8")
                except UnicodeDecodeError as e:
                    _result("position_policies")["failed"].append(
                        {"file": entry.name, "error": f"仓位策略文件不是有效 UTF-8: {e}"}
                    )
            else:
                plain_entries.append((entry, payload))

    # 第二遍: 内容校验 + 写盘 (策略 → 仓位策略 → 其余文件)。
    for directory, batch in _groupby_dir(strategy_codes):
        # 依赖名单 = 本批同目录 _*.py + 本机已存在的 _*.py (引擎 reload 时
        # glob 的是落盘后的目录, 两边取并集才不误杀)。
        dep_names = {p.name[:-3] for p in directory.glob("_*.py")}
        dep_names |= {e.target.name[:-3] for e, _ in batch if e.target.name.startswith("_")}
        for entry, code in batch:
            is_dep = entry.target.name.startswith("_")
            try:
                _validate_strategy_code(code, is_dependency=is_dep, dep_names=frozenset(dep_names))
                _write_payload(entry.target, code.encode("utf-8"))
            except (ConfigBackupError, ValueError, SyntaxError, OSError) as e:
                _result("strategies")["failed"].append({"file": entry.name, "error": str(e)})
                continue
            _result("strategies")["imported"] += 1
            reload_hints["strategies"] = True

    # 仓位策略: 与策略同一信任模型 — 安全校验 + 结构解析 + 合成冒烟三关全过才写盘
    # (validate_policy_code 内置); 注册表按目录签名惰性刷新, 无需 reload 钩子。
    if policy_codes:
        from app.backtest import position_policies as _pp

        for entry, code in policy_codes.items():
            check = _pp.validate_policy_code(code)
            if not check["ok"]:
                _result("position_policies")["failed"].append(
                    {"file": entry.name, "error": "; ".join(check["issues"])}
                )
                continue
            try:
                _write_payload(entry.target, code.encode("utf-8"))
            except OSError as e:
                _result("position_policies")["failed"].append({"file": entry.name, "error": str(e)})
                continue
            _result("position_policies")["imported"] += 1
        _pp.invalidate_cache()

    for entry, payload in plain_entries:
        try:
            _validate_payload(entry, payload)
            _write_payload(entry.target, payload)
        except (ConfigBackupError, ValueError, OSError, UnicodeDecodeError) as e:
            _result(entry.category)["failed"].append({"file": entry.name, "error": str(e)})
            continue
        _result(entry.category)["imported"] += 1
        if entry.category in reload_hints:
            reload_hints[entry.category] = True
        # 导入即冒烟 (P4): 信号/因子落盘后立刻静态冒烟, 结果随导入返回 —
        # 导入完成即知道定义能不能算, 不用等回测报错才发现。
        if entry.category in ("custom_signals", "custom_factors"):
            _result(entry.category).setdefault("smoke", []).append(_smoke_entry(entry, payload))

    # auth.json 是外部写入的凭证文件, 进程内密码/会话缓存需要重读。
    if results.get("auth", {}).get("imported"):
        from app.services import auth as auth_store

        auth_store.invalidate_runtime_caches()

    # 偏好/密钥最后落: merge 语义 (备份键覆盖本地同名键, 本地新增键保留),
    # 走各 store 的 save (自带锁、0600 与缓存失效)。
    if prefs_payload is not None:
        from app.services import preferences

        preferences.save(prefs_payload)
        _result("preferences")["imported"] += 1
    if secrets_payload is not None:
        from app import secrets_store

        secrets_store.save(secrets_payload)
        _result("secrets")["imported"] += 1

    failed_total = sum(len(r["failed"]) for r in results.values())
    logger.info(
        "config backup applied: allow_overwrite=%s imported=%d skipped=%d failed=%d",
        allow_overwrite,
        sum(r["imported"] for r in results.values()),
        sum(r["skipped_conflict"] for r in results.values()),
        failed_total,
    )
    return {
        "categories": [results[k] for k in _CATEGORY_ORDER if k in results],
        "preferences_merged": prefs_payload is not None,
        "secrets_merged": secrets_payload is not None,
        "skipped_unselected": skipped_unselected,
        "out_of_scope_files": len(parsed.out_of_scope),
        "reload_hints": reload_hints,
    }


def _groupby_dir(strategy_codes: dict[_Entry, str]) -> list[tuple[Path, list[tuple[_Entry, str]]]]:
    grouped: dict[Path, list[tuple[_Entry, str]]] = {}
    for entry, code in strategy_codes.items():
        grouped.setdefault(entry.target.parent, []).append((entry, code))
    # 排序只为结果稳定与写盘顺序可预期 (_ 依赖在前)。
    for batch in grouped.values():
        batch.sort(key=lambda item: (not item[0].target.name.startswith("_"), item[0].name))
    return sorted(grouped.items(), key=lambda kv: kv[0].name)


def _smoke_entry(entry: "_Entry", payload: bytes) -> dict:
    """对导入的信号/因子定义跑静态冒烟; 冒烟自身异常不阻断导入 (fail-open)。"""
    from app.definitions import smoke_definition

    kind = "signal" if entry.category == "custom_signals" else "factor"
    try:
        definition = json.loads(payload.decode("utf-8"))
        if not isinstance(definition, dict):
            raise ValueError("定义不是 JSON 对象")
        result = smoke_definition(kind, definition)
    except Exception as e:  # noqa: BLE001 — 冒烟是增值信息, 不构成导入失败
        return {"file": entry.name, "ok": False, "issues": [{"code": "S000", "message": f"冒烟未执行: {e}"}]}
    result["file"] = entry.name
    return result
