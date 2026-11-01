"""External provider packages. Static inspection never imports uploaded code.

The existing loader owns provider instances. This module owns immutable package
versions and activation state only; it never installs into the host environment.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path

import yaml
from packaging.version import Version

from app.config import settings
from app.services.fs_utils import atomic_write_text
from app.services.package_archive import MAX_EXPANDED as MAX_EXPANDED
from app.services.package_archive import MAX_UPLOAD as MAX_UPLOAD
from app.services.package_archive import compatibility, read_code_archive

NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)\Z")
ENTRY = re.compile(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*:[a-zA-Z_]\w*\Z")
DATASETS = {"daily", "adj_factor", "minute", "full_minute", "realtime", "depth5", "financial"}
# Once per process: never activate a staged update during a settings reload.
_started: set[str] = set()


def root() -> Path:
    return settings.data_dir / "plugins"


def directory(name: str) -> Path:
    if not NAME.fullmatch(name):
        raise ValueError("插件标识非法")
    path = root() / name
    if path.is_symlink() or root().is_symlink():
        raise ValueError("插件目录不能是符号链接")
    return path


@contextmanager
def mutation():
    root().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root() / ".management.sqlite", timeout=5)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        yield
    finally:
        conn.rollback()
        conn.close()


def read_state(name: str) -> dict | None:
    if not NAME.fullmatch(name):
        return None
    path = directory(name) / "state.json"
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != 1:
        raise ValueError("不支持的插件安装状态版本")
    return state


def write_state(name: str, state: dict):
    directory(name).mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory(name) / "state.json", json.dumps(state, ensure_ascii=False))


def version_dir(name: str, version: str) -> Path:
    if not VERSION.fullmatch(version):
        raise ValueError("插件版本非法")
    path = directory(name) / "versions" / version
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("插件版本目录不能是符号链接")
    return path


def manifest(name: str, version: str | None = None) -> dict | None:
    state = read_state(name)
    if state is None:
        return None
    version = version or state["active"]
    doc = yaml.safe_load((version_dir(name, version) / "plugin.yaml").read_text("utf-8"))
    validate_manifest(doc)
    if doc["name"] != name or doc["version"] != version:
        raise ValueError("插件清单与安装记录不一致")
    return doc


def validate_manifest(doc: dict):
    if not isinstance(doc, dict):
        raise ValueError("plugin.yaml 必须是对象")
    if not NAME.fullmatch(str(doc.get("name", ""))):
        raise ValueError("name 必须是小写字母开头的字母、数字、下划线")
    if not VERSION.fullmatch(str(doc.get("version", ""))):
        raise ValueError("version 必须是有效版本，例如 1.0.0")
    Version(doc["version"])
    if type(doc.get("plugin_api_version")) is not int or doc["plugin_api_version"] != 1:
        raise ValueError("仅支持 plugin_api_version: 1")
    for field in ("entry", "check"):
        if (field == "entry" or doc.get(field)) and not ENTRY.fullmatch(str(doc.get(field, ""))):
            raise ValueError(f"{field} 应使用包内相对模块引用，例如 provider:MyProvider")
    datasets = doc.get("datasets")
    if not isinstance(datasets, list) or not datasets or any(not isinstance(x, str) or x not in DATASETS for x in datasets):
        raise ValueError("datasets 必须声明已支持的数据集")
    for key in ("display_name", "description", "api_key_env", "homepage", "key_manage_url", "author", "billing", "billing_note", "install_hint"):
        if key in doc and (not isinstance(doc[key], str) or len(doc[key]) > 4000):
            raise ValueError(f"{key} 必须是文本且不超过 4000 字符")
    if doc.get("api_key_env") and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", doc["api_key_env"]):
        raise ValueError("api_key_env 必须是大写环境变量名")
    for key in ("homepage", "key_manage_url"):
        if doc.get(key) and not doc[key].startswith("https://"):
            raise ValueError(f"{key} 必须使用 HTTPS")
    if "usage_notes" in doc and (not isinstance(doc["usage_notes"], list) or any(not isinstance(x, str) for x in doc["usage_notes"])):
        raise ValueError("usage_notes 必须是文本列表")
    if doc.get("runtime", "none") not in ("none", "python"):
        raise ValueError("外部安装暂只支持 none/python 运行时，不安装额外运行时")
    if doc.get("hidden"):
        raise ValueError("外部安装的插件不能隐藏卡片")


def inspect_archive(data: bytes) -> tuple[dict, dict[str, bytes]]:
    files = read_code_archive(data)
    if "plugin.yaml" not in files or len(files["plugin.yaml"]) > 64 * 1024:
        raise ValueError("包根目录需要 plugin.yaml（最大 64 KiB）")
    doc = yaml.safe_load(files["plugin.yaml"])
    validate_manifest(doc)
    for field in ("entry", "check"):
        if doc.get(field):
            module = doc[field].split(":")[0].replace(".", "/")
            if f"{module}.py" not in files and f"{module}/__init__.py" not in files:
                raise ValueError(f"{field} 指定的模块不存在")
    for name, payload in files.items():
        if name.endswith(".py"):
            try:
                compile(payload, name, "exec")
            except (SyntaxError, ValueError) as exc:
                raise ValueError(f"Python 文件语法错误: {name}") from exc
    errors = compatibility(doc)
    from . import loader
    if (loader.plugins_dir() / doc["name"]).exists() or doc["name"] == "tickflow" or any(
        (loader.data_sources_dir() / f"{doc['name']}{suffix}").exists() for suffix in (".yaml", ".yml")
    ):
        raise ValueError("插件名称与内置插件或 HTTP 数据源冲突")
    state = read_state(doc["name"])
    digest = hashlib.sha256(data).hexdigest()
    if state and doc["version"] in state["hashes"] and state["hashes"][doc["version"]] != digest:
        raise ValueError("相同版本已有不同内容，请提升版本号")
    return {"manifest": doc, "digest": digest, "errors": errors,
            "pending_version": state.get("pending") if state else None,
            "revision": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(),
            "installed_version": state["active"] if state else None}, files


def import_module(doc: dict, module: str):
    path = version_dir(doc["name"], doc["version"])
    # Root-specific namespace keeps different installations and tests independent.
    contents = b"".join(file.read_bytes() for file in sorted(path.rglob("*.py")))
    digest = hashlib.sha256(str(path.resolve()).encode() + contents).hexdigest()[:20]
    namespace = f"_tsp_provider_{digest}"
    if namespace not in sys.modules:
        package = types.ModuleType(namespace)
        package.__path__ = [str(path)]
        package.__package__ = namespace
        sys.modules[namespace] = package
    return importlib.import_module(f"{namespace}.{module}")


def load_entry(doc: dict, entry: str):
    module, attr = entry.split(":")
    return getattr(import_module(doc, module), attr)


def build_provider(doc: dict):
    issues = compatibility(doc)
    if issues:
        raise ValueError("；".join(issues))
    factory = load_entry(doc, doc["entry"])
    if not callable(factory):
        raise ValueError("entry 必须指向 Provider 类或工厂")
    if doc.get("api_key_env"):
        module = import_module(doc, doc["entry"].split(":")[0])
        if not callable(getattr(module, "probe_api_key", None)):
            raise ValueError("声明 API Key 的插件必须提供 probe_api_key")
    from app import secrets_store
    if doc.get("api_key_env") and not secrets_store.get_env_backed_secret(f"{doc['name']}_api_key", doc["api_key_env"]):
        return None, "待配置 API Key"
    if doc.get("check"):
        check = load_entry(doc, doc["check"])()
        ok, reason = check if isinstance(check, tuple) else (check, "不可用")
        if not ok:
            key = secrets_store.get_env_backed_secret(f"{doc['name']}_api_key", doc.get("api_key_env", ""))
            return None, str(reason).replace(key, "[已隐藏]") if key else str(reason)
    provider = factory()
    try:
        if provider.name != doc["name"] or set(provider.config.datasets) != set(doc["datasets"]):
            raise ValueError("Provider 名称/能力声明与清单不一致")
        required = {"daily": "get_daily", "adj_factor": "get_adj_factors", "minute": "get_minute", "realtime": "get_realtime", "depth5": "get_depth_batch", "financial": "get_financials"}
        for dataset in doc["datasets"]:
            methods = ("get_intraday_batch", "get_minute") if dataset == "full_minute" else (required[dataset],)
            if not any(callable(getattr(provider, method, None)) for method in methods):
                raise ValueError(f"缺少 {dataset} 数据方法")
        if not callable(getattr(provider, "close", None)):
            raise ValueError("Provider 缺少 close 方法")
    except Exception:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        raise
    return provider, "已就绪"


def install(data: bytes, digest: str, revision: str | None = None) -> dict:
    from . import loader
    with mutation():
        preview, files = inspect_archive(data)
        if preview["digest"] != digest:
            raise ValueError("插件包与预览不一致，请重新预览")
        if revision is not None and preview["revision"] != revision:
            raise ValueError("插件状态已变化，请重新预览")
        if preview["errors"]:
            raise ValueError("；".join(preview["errors"]))
        doc = preview["manifest"]
        name, version = doc["name"], doc["version"]
        state = read_state(name)
        if state and state.get("remove"):
            raise ValueError("插件正在等待卸载，请先重启")
        if state and version == state["active"]:
            return {"name": name, "message": "此版本已安装"}
        if state and Version(version) <= Version(state["active"]):
            raise ValueError("升级需要更高版本；退回旧版本请使用回退操作")
        target = version_dir(name, version)
        if target.exists() and any(not (target / filename).is_file() or (target / filename).read_bytes() != payload for filename, payload in files.items()):
            raise ValueError("该版本目录已有其他内容，请提升版本号")
        created = not target.exists()
        if created:
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=target.parent))
            try:
                for filename, payload in files.items():
                    dest = staging / filename
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(payload)
                staging.rename(target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        if state:
            state["hashes"][version] = digest
            state["pending"] = version
            write_state(name, state)
            loader.refresh_external_metadata(name)
            return {"name": name, "message": "新版本已暂存，重启后生效"}
        # No live version exists: validate before publishing the installation record.
        try:
            provider, reason = build_provider(doc)
        except Exception as exc:
            raise ValueError("插件加载失败，请检查开发文档中的 Provider 契约") from exc
        try:
            write_state(name, {"schema": 1, "active": version, "hashes": {version: digest}, "enabled": True})
        except OSError:
            if provider is not None:
                provider.close()
            if created:
                shutil.rmtree(target)
            raise
        _started.add(str(directory(name).resolve()))
        loader.publish_external(doc, provider, reason)
        return {"name": name, "message": "安装完成，请配置并选择启用能力"}


def referenced_by(name: str) -> list[str]:
    from app.data_providers.capabilities import CAPABILITY_REGISTRY
    from app.services import preferences
    prefs = preferences.load()
    return [cap["label"] for cap in CAPABILITY_REGISTRY if cap["field"] and prefs.get(cap["field"]) == name]


def manage(name: str, action: str) -> dict:
    from . import loader
    with mutation():
        state = read_state(name)
        if not state:
            raise ValueError("外部插件不存在")
        if action in ("disable", "remove"):
            refs = referenced_by(name)
            if refs:
                raise ValueError(f"请先切换这些能力的数据源：{'、'.join(refs)}")
        if action == "rollback":
            if not state.get("previous"):
                raise ValueError("没有可回退版本")
            state["pending"] = state["previous"]
        elif action == "cancel-update":
            state.pop("pending", None)
        elif action == "remove":
            state["remove"] = True
            state["enabled"] = False
        elif action in ("enable", "disable"):
            if state.get("remove"):
                raise ValueError("插件正在等待卸载，请先重启")
            state["enabled"] = action == "enable"
        else:
            raise ValueError("不支持的插件操作")
        write_state(name, state)
        if action in ("enable", "disable", "remove"):
            loader.refresh_plugin(name)
        else:
            loader.refresh_external_metadata(name)
        return {"name": name, "message": "操作完成" + ("，重启后生效" if action in ("rollback", "remove") else "")}


def discover():
    """Apply pending versions once at cold discovery, before capability detection."""
    if not root().exists():
        return
    from . import loader
    for path in sorted(root().iterdir()):
        if not path.is_dir() or not NAME.fullmatch(path.name) or path.is_symlink():
            continue
        name = path.name
        try:
            with mutation():
                state = read_state(name)
                if state is None:
                    continue
                identity = str(path.resolve())
                cold = identity not in _started
                if cold and state.get("remove"):
                    shutil.rmtree(directory(name))
                    continue
                if cold:
                    _started.add(identity)
                    if state.get("pending") and state.get("enabled", True):
                        pending = state["pending"]
                        try:
                            candidate = manifest(name, pending)
                            provider, _ = build_provider(candidate)
                            if provider:
                                provider.close()
                            state["previous"], state["active"] = state["active"], pending
                            state.pop("error", None)
                        except Exception:  # noqa: BLE001 — retain the last working version
                            state["error"] = "新版本加载失败，已保留原版本"
                        state.pop("pending", None)
                        write_state(name, state)
                doc = manifest(name)
                # Never let external packages shadow built-in or user YAML providers.
                if (loader.plugins_dir() / name).exists() or name == "tickflow" or any(
                    (loader.data_sources_dir() / f"{name}{suffix}").exists() for suffix in (".yaml", ".yml")
                ):
                    raise ValueError("插件名称冲突")
                loader.refresh_plugin(name, manifest_doc=doc)
        except Exception:  # noqa: BLE001 — one malformed package must not block startup
            loader.record_external_error(name)
