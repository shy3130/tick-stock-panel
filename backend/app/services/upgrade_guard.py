"""二次开发升级保护 — 用户修改的程序文件常备备份与升级对比报告。

机制 (docs/secondary-development.md §8):

- 构建时生成全量程序文件清单 (app/upgrade_manifest.json, sha256 基线);
- 每次启动后台扫描程序目录, 与清单对比 → 用户「修改/新增」的程序文件
  常备备份到 data/upgrade_guard/snapshots/ (只保留最近一次成功快照);
- 检测到版本变化 (上次运行版本 ≠ 当前版本) 时, 用 data 里幸存的旧清单
  快照 + 旧备份生成升级对比报告:
    restorable  用户改过、新版没动 → 升级覆盖丢失, 可一键恢复;
    conflict    用户与新版都改过 → 保留三方材料供手工合并 (不自动改代码);
    match       新版内容与用户修改后的内容一致, 无需处理;
    added_lost  用户新建的文件新版没有 (如换镜像), 可恢复;
- 不自动合并代码: 自动改写程序文件的合并风险不可控, 与项目
  「扩展点优先、直改源码属 L3」的分层原则一致。

源码运行 (无清单) 时功能整体停用, 二开者走 git + scripts/upgrade_check.py。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import __version__
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

MANIFEST_NAME = "upgrade_manifest.json"

# 扫描与清单共用的排除规则 (scripts/generate_manifest.py 保持同集):
# data/ 是用户数据 (含本守卫自身备份), .venv/node_modules 是依赖,
# __pycache__/*.pyc 每次运行都变, .env 含密钥不入备份, 日志无合并价值。
EXCLUDED_DIRS = {"data", ".venv", "venv", "node_modules", "__pycache__", ".git", "logs"}
EXCLUDED_NAMES = {".env", ".DS_Store", MANIFEST_NAME}
EXCLUDED_SUFFIXES = {".pyc", ".log", ".tmp"}

_REPORT_CATEGORIES = ("restorable", "conflict", "match", "added_lost")
_SNAPSHOT_ID = re.compile(r"[0-9a-f]{32}")
_REPORT_ID = re.compile(r"[^/\\:]+_to_[^/\\:]+_\d{8}_\d{6}(?:_[0-9a-f]{8})?")


class UpgradeGuardBusyError(RuntimeError):
    """扫描或恢复正在写入备份, 不允许并发修改。"""


def _safe_path(base: Path, relative: str) -> Path:
    if not relative or "\\" in relative or ":" in relative or any(
        part in ("", ".", "..") for part in relative.split("/")
    ):
        raise ValueError("非法升级备份路径")
    path = base
    for part in ("", *relative.split("/")):
        path = path / part
        if path.is_symlink():
            raise ValueError("升级备份路径不能包含符号链接")
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("升级备份路径超出允许目录")
    return path


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _unreadable_directory(error: OSError) -> None:
    # rglob 会略过无权限目录, 不能把漏扫当成用户删除而清掉旧备份。
    raise error


def _is_excluded(rel: str, name: str) -> bool:
    if any(part in EXCLUDED_DIRS for part in rel.split("/")):
        return True
    if name in EXCLUDED_NAMES:
        return True
    return Path(name).suffix in EXCLUDED_SUFFIXES


def manifest_path() -> Path:
    """随程序落盘的清单位置 (是否存在由调用方判断)。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "app" / MANIFEST_NAME
    # 源码布局: app/services/upgrade_guard.py → app/upgrade_manifest.json
    return Path(__file__).resolve().parent.parent / MANIFEST_NAME


def program_root() -> Path | None:
    """程序文件根目录 (清单存在的父布局); 无清单 (源码运行) 返回 None。

    frozen (PyInstaller onedir): 清单落在 _internal/app/, 根 = _MEIPASS;
    Docker: /app/app/upgrade_manifest.json, 根 = /app;
    源码: backend/app/upgrade_manifest.json (仅 CI 生成过才有), 根 = backend/。
    """
    manifest = manifest_path()
    return manifest.parent.parent if manifest.is_file() else None


class UpgradeGuard:
    """扫描/备份/报告/恢复的单一入口; 全部状态落在 data/upgrade_guard/。"""

    def __init__(self, data_dir: Path, *, root: Path | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.state_dir = self.data_dir / "upgrade_guard"
        self.reports_dir = self.state_dir / "reports"
        self._lock = threading.Lock()
        self._busy = False
        self._last_status: dict[str, Any] | None = None
        self._scanning = False
        self._thread: threading.Thread | None = None

        # root 注入口仅供测试; 运行时由 program_root() 按清单位置推导。
        self.root = Path(root).resolve() if root is not None else program_root()
        self.manifest_version: str | None = None
        self.manifest: dict[str, str] = {}
        if self.root is not None:
            try:
                payload = json.loads((self.root / "app" / MANIFEST_NAME).read_text(encoding="utf-8"))
                self.manifest = {str(k): str(v) for k, v in payload.get("files", {}).items()}
                self.manifest_version = str(payload.get("version") or "") or None
            except Exception as e:  # noqa: BLE001 — 清单损坏按未启用处理
                logger.warning("upgrade guard manifest load failed, disabled: %s", e)
                self.root = None
                self.manifest = {}

    # ── 状态文件 ────────────────────────────────────────────────

    @property
    def _state_path(self) -> Path:
        return self.state_dir / "state.json"

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (ValueError, UnicodeError) as exc:
            raise OSError("升级备份状态损坏，已保留原备份") from exc
        if not isinstance(state, dict):
            raise OSError("升级备份状态无效，已保留原备份")
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._state_path, json.dumps(state, ensure_ascii=False, indent=1))

    @property
    def live_dir(self) -> Path:
        snapshot = self._load_state().get("live_snapshot")
        if snapshot is None:
            return _safe_path(self.state_dir, "live")  # 兼容旧版本固定目录。
        if not isinstance(snapshot, str) or not _SNAPSHOT_ID.fullmatch(snapshot):
            raise ValueError("升级备份快照标识无效")
        return _safe_path(self.state_dir, f"snapshots/{snapshot}")

    # ── 扫描 ────────────────────────────────────────────────────

    @contextmanager
    def _operation(self, *, scanning: bool = False) -> Iterator[None]:
        # 只用锁切换准入状态, 扫描/复制期间不持锁阻塞状态查询。
        with self._lock:
            if self._busy:
                raise UpgradeGuardBusyError("扫描或恢复正在进行中")
            self._busy = True
            self._scanning = scanning
        try:
            yield
        finally:
            with self._lock:
                self._busy = False
                self._scanning = False

    def _classify(self, cache: dict[str, Any]) -> tuple[list[str], list[str]]:
        """磁盘 vs 当前清单 → (用户修改的, 用户新增的)。命中缓存免重哈希。"""
        modified: list[str] = []
        added: list[str] = []
        seen: set[str] = set()
        data_root = self.data_dir.resolve()
        if self.root == data_root:
            raise ValueError("程序目录与数据目录相同，无法安全扫描升级备份")

        for parent, directories, filenames in os.walk(self.root, onerror=_unreadable_directory):
            directories[:] = [
                name for name in directories
                if name not in EXCLUDED_DIRS and (Path(parent) / name).resolve() != data_root
            ]
            for name in filenames:
                path = Path(parent) / name
                if path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(self.root).as_posix()  # type: ignore[arg-type]
                if _is_excluded(rel, name):
                    continue
                seen.add(rel)
                stat = path.stat()
                entry = cache.get(rel)
                if entry and entry.get("s") == stat.st_size and entry.get("m") == stat.st_mtime_ns:
                    digest = str(entry.get("h", ""))
                else:
                    digest = _sha256(path)
                    cache[rel] = {"s": stat.st_size, "m": stat.st_mtime_ns, "h": digest}
                expected = self.manifest.get(rel)
                if expected is None:
                    added.append(rel)
                elif digest != expected:
                    modified.append(rel)
        for rel in cache.keys() - seen:
            del cache[rel]
        return sorted(modified), sorted(added)

    def _sync_live(self, files: list[str]) -> Path:
        """先完整复制新快照; state.json 原子切换成功后才允许清理上一份。"""
        snapshot = _safe_path(self.state_dir, f"snapshots/{uuid.uuid4().hex}")
        snapshot.mkdir(parents=True)
        try:
            for rel in files:
                src = _safe_path(self.root, rel)  # type: ignore[arg-type]
                dst = snapshot / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            return snapshot
        except BaseException:
            shutil.rmtree(snapshot, ignore_errors=True)
            raise

    def _build_report(self, from_version: str, prev_manifest: dict[str, str]) -> dict[str, Any] | None:
        """版本变化时, 用旧 live 备份 + 旧清单快照 + 新磁盘生成对比报告。"""
        rows: list[dict[str, Any]] = []
        live = self.live_dir
        if not live.exists():
            if self._load_state().get("live_snapshot"):
                raise OSError("上一份升级备份快照缺失，已停止更新")
            return None
        backups = (
            Path(parent) / name
            for parent, _, names in os.walk(live, onerror=_unreadable_directory)
            for name in names
        )
        for backup in sorted(backups):
            if backup.is_symlink():
                raise ValueError("升级备份副本不能包含符号链接")
            if not backup.is_file():
                continue
            rel = backup.relative_to(live).as_posix()
            if _is_excluded(rel, backup.name):
                continue
            user_sha = _sha256(backup)
            disk = _safe_path(self.root, rel)  # type: ignore[arg-type]
            disk_sha = _sha256(disk) if disk.is_file() else None
            prev_sha = prev_manifest.get(rel)
            if disk_sha == user_sha:
                category = "match"
            elif disk_sha is None:
                category = "added_lost"
            elif prev_sha is not None and disk_sha == prev_sha:
                # 新版内容 == 旧版基线 → 新版没动此文件, 用户的修改被升级覆盖
                category = "restorable"
            else:
                category = "conflict"
            rows.append({
                "relpath": rel,
                "category": category,
                "user_modified": True,
                "in_old_manifest": prev_sha is not None,
                "current_exists": disk_sha is not None,
            })
        if not rows:
            return None
        token = uuid.uuid4().hex
        report_id = f"{from_version}_to_{__version__}_{time.strftime('%Y%m%d_%H%M%S')}_{token[:8]}"
        report_dir = self._report_dir(report_id)
        temporary = _safe_path(self.state_dir, f"reports/.report-{token}")
        counts = {c: 0 for c in _REPORT_CATEGORIES}
        for row in rows:
            counts[row["category"]] += 1
        report = {
            "id": report_id,
            "from_version": from_version,
            "to_version": __version__,
            "created_at": _utcnow_iso(),
            "counts": counts,
            "files": rows,
            "dir": str(report_dir),
        }
        try:
            for row in rows:
                src = live / row["relpath"]
                dst = temporary / "files" / row["relpath"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            atomic_write_text(temporary / "report.json", json.dumps(report, ensure_ascii=False, indent=1))
            temporary.rename(report_dir)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        return report

    def _latest_report_id(self, state: dict[str, Any]) -> str | None:
        for key in ("latest_report", "pending_report"):
            candidate = state.get(key)
            if candidate and self._load_report(str(candidate)):
                return str(candidate)
        # 老版本确认报告后不保存指针, 从旧归档中选最近一次进行兼容迁移。
        reports = []
        reports_dir = _safe_path(self.state_dir, "reports")
        if reports_dir.exists():
            for path in reports_dir.iterdir():
                if path.is_symlink() or not _REPORT_ID.fullmatch(path.name):
                    continue
                report = self._load_report(path.name)
                if report:
                    reports.append((str(report.get("created_at", "")), path.name))
        return max(reports)[1] if reports else None

    def _prune_backups(self, snapshot: Path, report_id: str | None) -> None:
        """仅清理本功能生成的自动副本, 新快照已提交后才调用。"""
        for path in snapshot.parent.iterdir():
            if path != snapshot and not path.is_symlink() and path.is_dir() and _SNAPSHOT_ID.fullmatch(path.name):
                shutil.rmtree(path)
        legacy = self.state_dir / "live"
        if legacy.exists() and not legacy.is_symlink():
            shutil.rmtree(legacy)
        reports_dir = _safe_path(self.state_dir, "reports")
        if reports_dir.exists():
            for path in reports_dir.iterdir():
                owned = _REPORT_ID.fullmatch(path.name) or re.fullmatch(r"\.report-[0-9a-f]{32}", path.name)
                if owned and path.name != report_id and not path.is_symlink() and path.is_dir():
                    shutil.rmtree(path)

    def scan(self) -> dict[str, Any]:
        """分类、生成完整副本、原子提交状态, 最后清理过期自动备份。"""
        with self._operation(scanning=True):
            return self._scan()

    def _scan(self) -> dict[str, Any]:
        if self.root is None:
            with self._lock:
                self._last_status = {"enabled": False, "reason": "manifest_missing"}
                return self._last_status
        snapshot = None
        report = None
        committed = False
        try:
            state = self._load_state()
            cache = state.get("file_cache") if isinstance(state.get("file_cache"), dict) else {}
            cache = {str(k): v for k, v in cache.items()}

            modified, added = self._classify(cache)

            latest_report_id = self._latest_report_id(state)
            prev_version = state.get("last_version")
            if prev_version and prev_version != __version__:
                prev_manifest = state.get("manifest_snapshot")
                prev_manifest = {str(k): str(v) for k, v in prev_manifest.items()} if isinstance(prev_manifest, dict) else {}
                report = self._build_report(str(prev_version), prev_manifest)

            snapshot = self._sync_live(modified + added)

            if report is not None:
                state["pending_report"] = report["id"]
                latest_report_id = report["id"]
            state["latest_report"] = latest_report_id
            state["live_snapshot"] = snapshot.name
            state["last_version"] = __version__
            state["manifest_snapshot"] = self.manifest
            state["file_cache"] = cache
            state["last_scan"] = _utcnow_iso()
            self._save_state(state)
            committed = True

            status = {
                "enabled": True,
                "program_root": str(self.root),
                "manifest_version": self.manifest_version,
                "current_version": __version__,
                "last_scan": state["last_scan"],
                "modified": modified,
                "added": added,
                "backup_dir": str(snapshot),
                "reports_dir": str(self.reports_dir),
                "pending_report": self._load_report(report["id"]) if report else self._pending_report(state),
            }
            with self._lock:
                self._last_status = status
            self._prune_backups(snapshot, latest_report_id)
            return status
        finally:
            if not committed:
                if snapshot is not None:
                    shutil.rmtree(snapshot, ignore_errors=True)
                if report is not None:
                    shutil.rmtree(self._report_dir(report["id"]), ignore_errors=True)

    # ── 报告读取 / 恢复 / 确认 ──────────────────────────────────

    def _pending_report(self, state: dict[str, Any]) -> dict[str, Any] | None:
        report_id = state.get("pending_report")
        return self._load_report(str(report_id)) if report_id else None

    def _load_report(self, report_id: str) -> dict[str, Any] | None:
        path = _safe_path(self._report_dir(report_id), "report.json")
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(report, dict):
            raise ValueError("升级报告内容无效")
        return report

    def _report_dir(self, report_id: str) -> Path:
        if "/" in report_id or "\\" in report_id:
            raise ValueError("非法升级报告标识")
        return _safe_path(self.state_dir, f"reports/{report_id}")

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._last_status is not None:
                return dict(self._last_status)
        return self.scan()

    def is_scanning(self) -> bool:
        with self._lock:
            return self._scanning

    def restore(self, report_id: str, relpath: str) -> Path:
        """把报告归档的用户版本复制回程序目录 (relpath 必须在报告清单内)。"""
        with self._operation():
            return self._restore(report_id, relpath)

    def _restore(self, report_id: str, relpath: str) -> Path:
        report = self._load_report(report_id)
        if report is None:
            raise FileNotFoundError(f"报告不存在: {report_id}")
        if not any(row["relpath"] == relpath for row in report.get("files", [])):
            raise ValueError(f"文件不在报告清单内: {relpath}")
        src = _safe_path(self._report_dir(report_id), f"files/{relpath}")
        if not src.is_file():
            raise FileNotFoundError(f"备份副本缺失: {relpath}")
        if self.root is None:
            raise ValueError("升级保护未启用")
        dst = _safe_path(self.root, relpath)
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".upgrade-restore-", suffix=".tmp", dir=dst.parent)
        temporary = Path(name)
        # 先关闭 mkstemp 的句柄, Windows 下 copy2/replace 才能独占临时文件。
        os.close(fd)
        try:
            shutil.copy2(src, temporary)
            temporary.replace(dst)
        finally:
            temporary.unlink(missing_ok=True)
        with self._lock:
            self._last_status = None
        logger.info("upgrade guard restored %s from report %s", relpath, report_id)
        return dst

    def dismiss_report(self) -> None:
        with self._operation():
            state = self._load_state()
            if state.get("pending_report"):
                # 兼容旧状态, 确认后仍保留最近一份归档供回查。
                state["latest_report"] = self._latest_report_id(state)
                state.pop("pending_report", None)
                self._save_state(state)
                with self._lock:
                    if self._last_status is not None:
                        self._last_status["pending_report"] = None

    # ── 后台线程 ────────────────────────────────────────────────

    def start_background_scan(self) -> None:
        """启动后台守护线程执行首扫 (增量缓存后秒级); 重复调用只跑一个。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._scan_worker, name="upgrade-guard", daemon=True)
            self._thread.start()

    def _scan_worker(self) -> None:
        try:
            status = self.scan()
            if status.get("enabled"):
                logger.info(
                    "upgrade guard: %d modified, %d added files backed up (manifest %s)",
                    len(status.get("modified", [])), len(status.get("added", [])),
                    status.get("manifest_version"),
                )
                report = status.get("pending_report")
                if report:
                    logger.warning(
                        "upgrade guard: upgrade %s → %s detected: %s",
                        report.get("from_version"), report.get("to_version"),
                        report.get("counts"),
                    )
            else:
                logger.info("upgrade guard disabled (no shipped manifest)")
        except Exception:
            logger.exception("upgrade guard scan failed")
