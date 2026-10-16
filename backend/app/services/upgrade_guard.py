"""二次开发升级保护 — 用户修改的程序文件常备备份与升级对比报告。

机制 (docs/secondary-development.md §8):

- 构建时生成全量程序文件清单 (app/upgrade_manifest.json, sha256 基线);
- 每次启动后台扫描程序目录, 与清单对比 → 用户「修改/新增」的程序文件
  常备备份到 data/upgrade_guard/live/ (data 不随升级覆盖, 天然安全);
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
import shutil
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import __version__

logger = logging.getLogger(__name__)

MANIFEST_NAME = "upgrade_manifest.json"

# 扫描与清单共用的排除规则 (scripts/generate_manifest.py 保持同集):
# data/ 是用户数据 (含本守卫自身备份), .venv/node_modules 是依赖,
# __pycache__/*.pyc 每次运行都变, .env 含密钥不入备份, 日志无合并价值。
EXCLUDED_DIRS = {"data", ".venv", "venv", "node_modules", "__pycache__", ".git", "logs"}
EXCLUDED_NAMES = {".env", ".DS_Store", MANIFEST_NAME}
EXCLUDED_SUFFIXES = {".pyc", ".log", ".tmp"}

_REPORT_CATEGORIES = ("restorable", "conflict", "match", "added_lost")


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_excluded(rel: str, name: str) -> bool:
    if rel in EXCLUDED_DIRS or rel.split("/", 1)[0] in EXCLUDED_DIRS:
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
        self.live_dir = self.state_dir / "live"
        self.reports_dir = self.state_dir / "reports"
        self._lock = threading.Lock()
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
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 首次/损坏都按空状态起步
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._state_path)

    # ── 扫描 ────────────────────────────────────────────────────

    def _classify(self, cache: dict[str, Any]) -> tuple[list[str], list[str]]:
        """磁盘 vs 当前清单 → (用户修改的, 用户新增的)。命中缓存免重哈希。"""
        modified: list[str] = []
        added: list[str] = []
        for path in self.root.rglob("*"):  # type: ignore[union-attr]
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()  # type: ignore[arg-type]
            if _is_excluded(rel, path.name):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entry = cache.get(rel)
            if entry and entry.get("s") == stat.st_size and entry.get("m") == stat.st_mtime_ns:
                digest = str(entry.get("h", ""))
            else:
                try:
                    digest = _sha256(path)
                except OSError:
                    continue
                cache[rel] = {"s": stat.st_size, "m": stat.st_mtime_ns, "h": digest}
            expected = self.manifest.get(rel)
            if expected is None:
                added.append(rel)
            elif digest != expected:
                modified.append(rel)
        return sorted(modified), sorted(added)

    def _sync_live(self, files: list[str]) -> None:
        """live/ 常备备份对齐当前修改集: 复制现存, 清掉已还原/已删除的陈旧项。"""
        wanted: set[str] = set()
        for rel in files:
            src = self.root / rel  # type: ignore[operator]
            dst = self.live_dir / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                wanted.add(rel)
            except OSError as e:
                logger.warning("upgrade guard backup copy failed %s: %s", rel, e)
        if self.live_dir.exists():
            for old in self.live_dir.rglob("*"):
                if old.is_file():
                    rel = old.relative_to(self.live_dir).as_posix()
                    if rel not in wanted:
                        old.unlink(missing_ok=True)

    def _build_report(self, from_version: str, prev_manifest: dict[str, str]) -> dict[str, Any] | None:
        """版本变化时, 用旧 live 备份 + 旧清单快照 + 新磁盘生成对比报告。"""
        rows: list[dict[str, Any]] = []
        if not self.live_dir.exists():
            return None
        for backup in sorted(self.live_dir.rglob("*")):
            if not backup.is_file():
                continue
            rel = backup.relative_to(self.live_dir).as_posix()
            if _is_excluded(rel, backup.name):
                continue
            try:
                user_sha = _sha256(backup)
            except OSError:
                continue
            disk = self.root / rel  # type: ignore[operator]
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
        report_id = f"{from_version}_to_{__version__}_{time.strftime('%Y%m%d_%H%M%S')}"
        report_dir = self.reports_dir / report_id
        # 三方材料: files/<rel> = 用户版本 (旧备份原样归档)
        for row in rows:
            src = self.live_dir / row["relpath"]
            dst = report_dir / "files" / row["relpath"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
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
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8",
        )
        return report

    def scan(self) -> dict[str, Any]:
        """完整扫描: 分类 → (版本变化时) 报告 → 重对齐常备备份 → 状态落盘。"""
        with self._lock:
            if self.root is None:
                self._last_status = {"enabled": False, "reason": "manifest_missing"}
                return self._last_status
            self._scanning = True
        try:
            state = self._load_state()
            cache = state.get("file_cache") if isinstance(state.get("file_cache"), dict) else {}
            cache = {str(k): v for k, v in cache.items()}

            modified, added = self._classify(cache)

            report: dict[str, Any] | None = None
            prev_version = state.get("last_version")
            if prev_version and prev_version != __version__:
                prev_manifest = state.get("manifest_snapshot")
                prev_manifest = {str(k): str(v) for k, v in prev_manifest.items()} if isinstance(prev_manifest, dict) else {}
                try:
                    report = self._build_report(str(prev_version), prev_manifest)
                except Exception as e:  # noqa: BLE001 — 报告失败不阻断扫描
                    logger.warning("upgrade guard report build failed: %s", e)

            self._sync_live(modified + added)

            if report is not None:
                state["pending_report"] = report["id"]
            state["last_version"] = __version__
            state["manifest_snapshot"] = self.manifest
            state["file_cache"] = cache
            state["last_scan"] = _utcnow_iso()
            self._save_state(state)

            status = {
                "enabled": True,
                "program_root": str(self.root),
                "manifest_version": self.manifest_version,
                "current_version": __version__,
                "last_scan": state["last_scan"],
                "modified": modified,
                "added": added,
                "backup_dir": str(self.live_dir),
                "reports_dir": str(self.reports_dir),
                "pending_report": self._load_report(report["id"]) if report else self._pending_report(state),
            }
            with self._lock:
                self._last_status = status
            return status
        finally:
            with self._lock:
                self._scanning = False

    # ── 报告读取 / 恢复 / 确认 ──────────────────────────────────

    def _pending_report(self, state: dict[str, Any]) -> dict[str, Any] | None:
        report_id = state.get("pending_report")
        return self._load_report(str(report_id)) if report_id else None

    def _load_report(self, report_id: str) -> dict[str, Any] | None:
        path = self.reports_dir / report_id / "report.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

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
        report = self._load_report(report_id)
        if report is None:
            raise FileNotFoundError(f"报告不存在: {report_id}")
        if not any(row["relpath"] == relpath for row in report.get("files", [])):
            raise ValueError(f"文件不在报告清单内: {relpath}")
        src = self.reports_dir / report_id / "files" / relpath
        if not src.is_file():
            raise FileNotFoundError(f"备份副本缺失: {relpath}")
        dst = self.root / relpath  # type: ignore[operator]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logger.info("upgrade guard restored %s from report %s", relpath, report_id)
        return dst

    def dismiss_report(self) -> None:
        state = self._load_state()
        if state.get("pending_report"):
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
