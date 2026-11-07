"""Desktop data recovery. Existing values win; source configurations remain recoverable."""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_MERGE_FILES = {Path("user_data/secrets.json"), Path("user_data/preferences.json")}
_LOCK_FILES = {".desktop.lock", ".mining_process_lock", ".mining_process.lock"}


def _copy_missing(source: Path, dest: Path, *, private: bool = False) -> bool:
    """Publish a complete file without ever replacing a concurrently created file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".upgrade-", dir=dest.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600 if private else stat.S_IMODE(source.stat().st_mode))
        try:
            if os.name == "nt":
                # Windows rename fails when the destination exists.
                temporary.rename(dest)
            else:
                os.link(temporary, dest)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _fill_missing(current: dict, previous: dict) -> bool:
    changed = False
    for key, value in previous.items():
        if key not in current:
            current[key] = copy.deepcopy(value)
            changed = True
        elif isinstance(current[key], dict) and isinstance(value, dict):
            changed = _fill_missing(current[key], value) or changed
    return changed


def _backup(path: Path, folder: Path, label: str) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{label}-{path.name}-{digest}.backup"
    if _copy_missing(path, dest, private=True):
        return
    if dest.is_symlink() or not dest.is_file() or hashlib.sha256(dest.read_bytes()).hexdigest() != digest:
        raise RuntimeError(f"现有升级备份与原文件不一致，停止恢复：{dest}")


def _recover_json(source: Path, dest: Path, backup: Path, *, merge_fields: bool) -> None:
    try:
        previous = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            raise ValueError("expected object")
    except (ValueError, UnicodeError):
        logger.warning("旧配置格式无效，保留原文件，未写入：%s", source)
        return
    if not dest.exists():
        _copy_missing(source, dest, private=True)
        return
    if not dest.is_file():
        raise RuntimeError(f"配置目标不是普通文件，停止恢复：{dest}")
    raw = dest.read_bytes()
    try:
        current = json.loads(raw.decode("utf-8"))
        if not isinstance(current, dict):
            raise ValueError("expected object")
    except (ValueError, UnicodeError):
        logger.warning("现有配置格式无效，未自动覆盖：%s", dest)
        return
    if not merge_fields:
        # A Key, endpoint and account form one credential configuration. Never
        # combine an old endpoint/account with a newly configured Key.
        if current != previous:
            _backup(dest, backup, "current")
            _backup(source, backup, "previous")
        return
    if not _fill_missing(current, previous):
        return
    _backup(dest, backup, "current")
    _backup(source, backup, "previous")
    if dest.read_bytes() != raw:
        raise RuntimeError(f"配置在恢复期间发生变化，停止写入：{dest}")
    atomic_write_text(dest, json.dumps(current, ensure_ascii=False, indent=2), mode=0o600)


def _merge_tree(source: Path, dest: Path, data_dir: Path, backup: Path, depth: int = 0) -> None:
    if depth > 32:
        raise RuntimeError(f"旧数据目录层级过深，停止恢复：{source}")
    if source.is_symlink() or dest.is_symlink() or not dest.resolve().is_relative_to(data_dir):
        logger.warning("升级恢复跳过符号链接，未写入：%s", dest)
        return
    if dest.exists() and not dest.is_dir():
        logger.warning("升级恢复保留现有同名文件：%s", dest)
        return
    dest.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        if item.name.casefold() in _LOCK_FILES or item.is_symlink():
            continue
        # Older releases could repeatedly nest user_data in itself.
        target = dest if source.name.casefold() == item.name.casefold() == "user_data" and item.is_dir() else dest / item.name
        if target.is_symlink() or not target.resolve().is_relative_to(data_dir):
            logger.warning("升级恢复跳过符号链接，未写入：%s", target)
            continue
        if item.is_dir():
            _merge_tree(item, target, data_dir, backup, depth + 1)
            continue
        if not item.is_file():
            continue
        relative = target.relative_to(data_dir)
        if relative in _MERGE_FILES:
            _recover_json(item, target, backup, merge_fields=relative.name.casefold() == "preferences.json")
            continue
        if target.exists():
            continue
        # Configurations remain in the old directory for rollback. Immutable
        # market partitions may move without duplicating gigabytes on one disk.
        move_partition = relative.parts[0].casefold() != "user_data" and item.suffix.casefold() == ".parquet"
        published = False
        if move_partition:
            try:
                os.link(item, target)
                published = True
            except FileExistsError:
                continue
            except OSError as exc:
                if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP}:
                    raise
        if not published:
            published = _copy_missing(item, target, private=relative.parts[0].casefold() == "user_data")
        if published and move_partition:
            try:
                item.unlink()
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
                    raise
                logger.info("旧分区只读，已复制并保留原文件：%s", item)


def _restore_source(source: Path, dest: Path, data_dir: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        return
    source_root = source.resolve()
    # Never traverse a source that contains the destination.
    if source_root == dest.resolve() or dest.resolve().is_relative_to(source_root):
        return
    source_identity = os.path.normcase(str(source_root))
    token = hashlib.sha256(source_identity.encode()).hexdigest()[:20]
    backup = data_dir / "upgrade_backups" / "data-dir-v1" / token
    for directory in (data_dir / "upgrade_backups", backup.parent, backup):
        if directory.is_symlink() or not directory.resolve().is_relative_to(data_dir):
            raise RuntimeError(f"升级备份目录是符号链接，停止恢复：{directory}")
    marker = backup / "completed.json"
    if marker.is_symlink():
        raise RuntimeError(f"升级恢复记录是符号链接，停止恢复：{marker}")
    if marker.exists():
        if not marker.is_file():
            raise RuntimeError(f"升级恢复记录不是普通文件：{marker}")
        try:
            record = json.loads(marker.read_bytes())
            recorded_source = record.get("source")
            if record.get("version") == 1 and isinstance(recorded_source, str) and os.path.normcase(recorded_source) == source_identity:
                return
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f"升级恢复记录无效，未自动重复迁移：{marker}")
    logger.info("恢复旧数据目录（保留现有配置）：%s -> %s", source, dest)
    _merge_tree(source, dest, data_dir, backup)
    backup.mkdir(parents=True, exist_ok=True)
    # A completed recovery must not resurrect keys users subsequently remove.
    atomic_write_text(marker, json.dumps({"version": 1, "source": str(source_root)}), mode=0o600)


def recover_desktop_data(data_dir: Path) -> None:
    """Run before backend imports on desktops; source and Docker never migrate."""
    if not getattr(sys, "frozen", False):
        return
    with _LOCK:
        data_dir = Path(data_dir).resolve()
        executable_dir = Path(sys.executable).resolve().parent
        candidates = [data_dir.parent / "TickFlowStockPanel_Data",
                      executable_dir.parent / "TickFlowStockPanel_Data"]
        if sys.platform == "darwin":
            candidates.append(executable_dir / "data")
        seen: set[Path] = set()
        # Prefer configurations already in the current data root over older roots.
        nested = data_dir / "user_data" / "user_data"
        _restore_source(nested, data_dir / "user_data", data_dir)
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen or resolved == data_dir or resolved.is_relative_to(data_dir):
                continue
            seen.add(resolved)
            _restore_source(candidate, data_dir, data_dir)
