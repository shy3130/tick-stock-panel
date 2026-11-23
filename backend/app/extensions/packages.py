"""Versioned backend/page packages, activated only during application startup."""
from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import types
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import yaml
from fastapi import FastAPI
from fastapi.routing import APIRoute
from packaging.version import Version

from app.extensions.contracts import BACKEND_EXTENSION_API_VERSION, ExtensionContext
from app.extensions.registry import BackendExtensionRegistrar
from app.services.ext_pages import PagePackMeta
from app.services.fs_utils import atomic_write_text
from app.services.package_archive import compatibility, read_code_archive

_ID = re.compile(r"[a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)*\Z")
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)\Z")
_MODULE = re.compile(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*\Z")


class _StateChangedError(RuntimeError):
    pass


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _contents_hash(files: dict[str, bytes]) -> str:
    return _hash(json.dumps(sorted((name, _hash(data)) for name, data in files.items())).encode())


def _validate_manifest(doc: dict, files: dict[str, bytes]) -> None:
    if not isinstance(doc, dict) or doc.get("format") != "tick-extension-package" or type(doc.get("format_version")) is not int or doc["format_version"] != 1:
        raise ValueError("extension.yaml 需要 tick-extension-package 格式 v1")
    extension_id = doc.get("id", "")
    if not isinstance(extension_id, str) or len(extension_id) > 80 or not _ID.fullmatch(extension_id) or extension_id.startswith(("official.", "core.")):
        raise ValueError("扩展 id 非法或使用了主程序保留命名空间")
    version = doc.get("version", "")
    if not isinstance(version, str) or len(version) > 64 or not _VERSION.fullmatch(version):
        raise ValueError("version 必须是有效版本，例如 1.0.0")
    Version(version)
    if type(doc.get("extension_api_version")) is not int or doc["extension_api_version"] != BACKEND_EXTENSION_API_VERSION:
        raise ValueError("不支持的后端扩展契约版本")
    for field in ("display_name", "author", "description"):
        if field in doc and (not isinstance(doc[field], str) or len(doc[field]) > 4000):
            raise ValueError(f"{field} 必须是文本且不超过 4000 字符")
    module = doc.get("backend")
    if not isinstance(module, str) or not _MODULE.fullmatch(module):
        raise ValueError("backend 必须指定包内模块，例如 backend.extension")
    path = module.replace(".", "/")
    if f"{path}.py" not in files and f"{path}/__init__.py" not in files:
        raise ValueError("backend 指定的模块不存在")
    frontend = doc.get("frontend")
    if frontend is not None and (not isinstance(frontend, str) or frontend not in files or PurePosixPath(frontend).suffix not in (".js", ".mjs")):
        raise ValueError("frontend 必须指向包内自包含的 .js/.mjs 页面产物")
    for name, payload in files.items():
        if name.endswith(".py"):
            try:
                compile(payload, name, "exec")
            except (SyntaxError, ValueError) as exc:
                raise ValueError(f"Python 文件语法错误：{name}") from exc


class ExtensionPackageStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "extensions"

    def _directory(self, extension_id: str) -> Path:
        if not isinstance(extension_id, str) or len(extension_id) > 80 or not _ID.fullmatch(extension_id):
            raise ValueError("扩展标识非法")
        path = self.root / extension_id
        if self.root.is_symlink() or path.is_symlink():
            raise ValueError("扩展目录不能是符号链接")
        return path

    def _version_dir(self, extension_id: str, version: str) -> Path:
        if not isinstance(version, str) or len(version) > 64 or not _VERSION.fullmatch(version):
            raise ValueError("扩展版本非法")
        path = self._directory(extension_id) / "versions" / version
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("扩展版本目录不能是符号链接")
        return path

    @contextmanager
    def mutation(self):
        if self.root.is_symlink():
            raise ValueError("扩展目录不能是符号链接")
        self.root.mkdir(parents=True, exist_ok=True)
        db = self.root / ".management.sqlite"
        if db.is_symlink():
            raise ValueError("扩展管理文件不能是符号链接")
        conn = sqlite3.connect(db, timeout=5)
        try:
            conn.execute("BEGIN EXCLUSIVE")
            yield
        finally:
            conn.rollback()
            conn.close()

    def _state(self, extension_id: str) -> dict | None:
        path = self._directory(extension_id) / "state.json"
        if path.is_symlink():
            raise ValueError("扩展记录不能是符号链接")
        if not path.exists():
            return None
        state = json.loads(path.read_text("utf-8"))
        if not isinstance(state, dict) or state.get("schema") != 1 or not isinstance(state.get("hashes"), dict) or not isinstance(state.get("contents"), dict):
            raise ValueError("不支持的扩展安装记录")
        return state

    def _write(self, extension_id: str, state: dict) -> None:
        path = self._directory(extension_id)
        path.mkdir(parents=True, exist_ok=True)
        if (path / "state.json").is_symlink():
            raise ValueError("扩展记录不能是符号链接")
        atomic_write_text(path / "state.json", json.dumps(state, ensure_ascii=False))

    @staticmethod
    def _revision(state: dict | None) -> str:
        return _hash(json.dumps(state, sort_keys=True).encode())

    def inspect(self, data: bytes) -> dict:
        files = read_code_archive(data, frontend=True)
        if "extension.yaml" not in files or len(files["extension.yaml"]) > 64 * 1024:
            raise ValueError("包根目录需要 extension.yaml（最大 64 KiB）")
        doc = yaml.safe_load(files["extension.yaml"])
        _validate_manifest(doc, files)
        state = self._state(doc["id"])
        digest = _hash(data)
        if state and doc["version"] in state["hashes"] and state["hashes"][doc["version"]] != digest:
            raise ValueError("相同版本已有不同内容，请提升版本号")
        return {
            "manifest": doc, "digest": digest, "revision": self._revision(state),
            "errors": compatibility(doc), "files": sorted(files),
            "installed_version": state.get("active") if state else None,
            "pending_version": state.get("pending") if state else None,
        }

    def install(self, data: bytes, digest: str, revision: str) -> dict:
        with self.mutation():
            preview = self.inspect(data)
            if digest != preview["digest"]:
                raise ValueError("扩展包与预览不一致，请重新预览")
            if revision != preview["revision"]:
                raise ValueError("扩展状态已变化，请重新预览")
            if preview["errors"]:
                raise ValueError("；".join(preview["errors"]))
            doc = preview["manifest"]
            extension_id, version = doc["id"], doc["version"]
            state = self._state(extension_id)
            if state and state.get("remove"):
                raise ValueError("扩展等待卸载，请先重启主程序")
            if state and version == state.get("active"):
                return {"id": extension_id, "message": "此版本已安装"}
            if state and state.get("active") and Version(version) <= Version(state["active"]):
                raise ValueError("更新需要更高版本，退回旧版本请使用回退操作")
            files = read_code_archive(data, frontend=True)
            target = self._version_dir(extension_id, version)
            content_hash = _contents_hash(files)
            if target.exists():
                if self._read_version_files(extension_id, version) != files:
                    raise ValueError("该版本目录已有其他内容，请提升版本号")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=target.parent))
                try:
                    for name, payload in files.items():
                        dest = staging / name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(payload)
                    staging.rename(target)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            state = state or {"schema": 1, "active": None, "enabled": True, "hashes": {}, "contents": {}}
            state["hashes"][version] = digest
            state["contents"][version] = content_hash
            state["pending"] = version
            state["updated_at"] = time.time()
            state.pop("error", None)
            state.pop("failed_version", None)
            self._write(extension_id, state)
            return {"id": extension_id, "message": "扩展包已保存，重启主程序后生效"}

    def _read_version_files(self, extension_id: str, version: str) -> dict[str, bytes]:
        path = self._version_dir(extension_id, version)
        files = {}
        total = 0
        for item in path.rglob("*"):
            if item.is_symlink():
                raise ValueError("扩展文件不能是符号链接")
            if not item.is_file() or "__pycache__" in item.parts:
                continue
            total += item.stat().st_size
            if total > 80 * 1024 * 1024 or len(files) >= 1000:
                raise ValueError("扩展文件超限")
            files[item.relative_to(path).as_posix()] = item.read_bytes()
        return files

    def _manifest(self, extension_id: str, version: str, state: dict) -> tuple[dict, dict[str, bytes]]:
        files = self._read_version_files(extension_id, version)
        if _contents_hash(files) != state.get("contents", {}).get(version):
            raise ValueError("扩展文件与安装记录不一致")
        doc = yaml.safe_load(files["extension.yaml"])
        _validate_manifest(doc, files)
        if doc["id"] != extension_id or doc["version"] != version:
            raise ValueError("扩展清单与安装记录不一致")
        return doc, files

    def manage(self, extension_id: str, action: str, revision: str | None = None) -> dict:
        with self.mutation():
            state = self._state(extension_id)
            if not state:
                raise ValueError("扩展不存在")
            if revision is not None and revision != self._revision(state):
                raise ValueError("扩展状态已变化，请刷新清单后重试")
            if state.get("remove"):
                raise ValueError("扩展等待卸载，请先重启主程序")
            if action in ("enable", "disable"):
                state["pending_enabled"] = action == "enable"
                if action == "enable" and not state.get("active") and state.get("failed_version"):
                    state["pending"] = state["failed_version"]
            elif action == "rollback":
                if not state.get("previous"):
                    raise ValueError("没有可回退版本")
                state["pending"] = state["previous"]
            elif action == "cancel-update":
                if not state.get("active"):
                    raise ValueError("首次安装尚未生效，请使用卸载操作取消")
                state.pop("pending", None)
                state.pop("error", None)
            elif action == "remove":
                state["remove"] = True
            else:
                raise ValueError("不支持的扩展操作")
            self._write(extension_id, state)
        return {"id": extension_id, "message": "变更已保存，重启主程序后生效"}

    def _ids(self) -> list[str]:
        if self.root.is_symlink():
            raise ValueError("扩展目录不能是符号链接")
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.is_symlink() and _ID.fullmatch(p.name))

    def list_installed(self, running: dict[str, str]) -> list[dict]:
        result = []
        for extension_id in self._ids():
            try:
                state = self._state(extension_id)
                if not state:
                    continue
                version = state.get("active") or state.get("pending") or state.get("failed_version")
                doc, _ = self._manifest(extension_id, version, state)
                status = "已启用" if extension_id in running else "已停用"
                if state.get("pending"):
                    status = "待重启生效"
                if state.get("error"):
                    status = "启动失败 · " + ("保留原版" if extension_id in running else "未启用")
                if "pending_enabled" in state:
                    status = "待重启" + ("启用" if state["pending_enabled"] else "停用")
                if state.get("remove"):
                    status = "待重启卸载"
                result.append({
                    "id": extension_id, "name": doc.get("display_name") or extension_id,
                    "version": state.get("active"), "pending_version": state.get("pending"),
                    "previous_version": state.get("previous"), "enabled": state.get("pending_enabled", state.get("enabled", True)),
                    "runtime_version": running.get(extension_id), "frontend": bool(doc.get("frontend")),
                    "failed_version": state.get("failed_version"), "revision": self._revision(state),
                    "status": status, "error": state.get("error", ""), "pending_remove": bool(state.get("remove")),
                })
            except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError):
                result.append({"id": extension_id, "name": extension_id, "status": "安装记录损坏", "error": "扩展记录或文件损坏，未加载", "enabled": False})
        return result

    def runtime_pages(self, running: dict[str, str]) -> list[PagePackMeta]:
        pages = []
        for extension_id, version in running.items():
            try:
                doc, files = self._manifest(extension_id, version, self._state(extension_id))
                if doc.get("frontend"):
                    payload = files[doc["frontend"]]
                    pages.append(PagePackMeta(
                        id=f"package:{extension_id}", enabled=True, label=doc.get("display_name") or extension_id,
                        size=len(payload), sha256=_hash(payload), managed_by=extension_id, version=version,
                    ))
            except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError):
                continue
        return pages

    def runtime_bundle(self, pack_id: str, running: dict[str, str]) -> Path | None:
        extension_id = pack_id.removeprefix("package:")
        version = running.get(extension_id)
        if not pack_id.startswith("package:") or not version:
            return None
        doc, _ = self._manifest(extension_id, version, self._state(extension_id))
        return self._version_dir(extension_id, version) / doc["frontend"] if doc.get("frontend") else None


def _prepare(app: FastAPI, context: ExtensionContext, store: ExtensionPackageStore, extension_id: str, version: str, state: dict):
    doc, files = store._manifest(extension_id, version, state)
    issues = compatibility(doc)
    if issues:
        raise ValueError("；".join(issues))
    registry = app.state.extension_registry.mutable_copy()
    if extension_id in registry.extension_ids():
        raise ValueError("扩展 ID 与主程序已有扩展冲突")
    namespace = "_tsp_extension_" + _hash(str(store._version_dir(extension_id, version).resolve()).encode() + _contents_hash(files).encode())[:24]
    if namespace not in sys.modules:
        package = types.ModuleType(namespace)
        package.__path__ = [str(store._version_dir(extension_id, version))]
        package.__package__ = namespace
        sys.modules[namespace] = package
    module = importlib.import_module(f"{namespace}.{doc['backend']}")
    if getattr(module, "EXTENSION_ID", None) != extension_id or getattr(module, "EXTENSION_API_VERSION", None) != BACKEND_EXTENSION_API_VERSION:
        raise ValueError("后端模块 ID/契约与清单不一致")
    setup = getattr(module, "setup", None)
    if not callable(setup) or inspect.iscoroutinefunction(setup):
        raise ValueError("扩展需要同步 setup(registrar)")
    registrar = BackendExtensionRegistrar(extension_id, api_version=BACKEND_EXTENSION_API_VERSION)
    setup(registrar)
    prefix = f"/api/extensions/{extension_id}/"
    for router in registrar.routers:
        for route in router.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith(prefix):
                raise ValueError(f"外部扩展接口必须位于 {prefix}")
    from app.extensions.loader import _validate_router_conflicts
    _validate_router_conflicts(app, registrar)
    registry.register(registrar)
    startup = getattr(module, "startup", None)
    if startup is not None:
        if not callable(startup) or inspect.iscoroutinefunction(startup):
            raise ValueError("扩展 startup(context) 必须是同步函数")
        startup(context)
    registry.freeze()
    return registrar, registry


def start_managed_extensions(app: FastAPI, context: ExtensionContext) -> None:
    """Stage registration and startup before publishing routes, policies and pages."""
    if getattr(app.state, "managed_extensions_started", False):
        return
    store = ExtensionPackageStore(context.data_dir)
    running: dict[str, str] = {}
    app.state.managed_extension_versions = running
    try:
        ids = store._ids()
    except ValueError:
        ids = []
    for extension_id in ids:
        try:
            state = store._state(extension_id)
            if not state:
                continue
            if state.get("remove"):
                with store.mutation():
                    if store._revision(store._state(extension_id)) != store._revision(state):
                        continue
                    shutil.rmtree(store._directory(extension_id))
                continue
            enabled = state.get("pending_enabled", state.get("enabled", True))
            if not enabled:
                with store.mutation():
                    if store._revision(store._state(extension_id)) != store._revision(state):
                        continue
                    state["enabled"] = False
                    state.pop("pending_enabled", None)
                    store._write(extension_id, state)
                continue
            candidates = list(dict.fromkeys(v for v in (state.get("pending"), state.get("active")) if v))
            failed = False
            for version in candidates:
                routes_before = list(app.router.routes)
                lifespan_before = app.router.lifespan_context
                revision = store._revision(state)
                try:
                    registrar, registry = _prepare(app, context, store, extension_id, version, state)
                    for router in registrar.routers:
                        app.include_router(router)
                    # Lifespan runs after the production SPA catch-all is mounted.
                    # Publish API routes before that fallback so GETs reach Python.
                    fallback = next((i for i, route in enumerate(routes_before)
                                     if getattr(route, "name", None) == "spa_fallback"
                                     and getattr(route, "path", None) == "/{full_path:path}"), len(routes_before))
                    added = app.router.routes[len(routes_before):]
                    app.router.routes[:] = routes_before[:fallback] + added + routes_before[fallback:]
                    with store.mutation():
                        if store._revision(store._state(extension_id)) != store._revision(state):
                            raise _StateChangedError("扩展状态在启动时发生变化")
                        next_state = dict(state)
                        if version != state.get("active"):
                            next_state["previous"] = state.get("active")
                        next_state.update(active=version, enabled=True)
                        next_state.pop("pending", None)
                        next_state.pop("pending_enabled", None)
                        if not failed and state.get("pending") == version:
                            next_state.pop("error", None)
                            next_state.pop("failed_version", None)
                        store._write(extension_id, next_state)
                    app.state.extension_registry = registry
                    running[extension_id] = version
                    app.openapi_schema = None
                    break
                except Exception as exc:  # noqa: BLE001 — optional code cannot leave partial routes or policies
                    app.router.routes[:] = routes_before
                    app.router.lifespan_context = lifespan_before
                    if isinstance(exc, _StateChangedError):
                        break
                    failed = True
                    state["error"] = "新版本启动失败，已保留原版本" if state.get("active") else "扩展启动失败，未启用"
                    state["failed_version"] = version
                    state.pop("pending", None)
                    with store.mutation():
                        if store._revision(store._state(extension_id)) != revision:
                            break
                        store._write(extension_id, state)
        except Exception:  # noqa: BLE001 — corrupt records must not stop unrelated extensions
            continue
    app.state.managed_extensions_started = True
