"""Make a private, resumable workspace copy while the desktop is offline."""

import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import ExitStack, closing, contextmanager, nullcontext
from pathlib import Path

_EXCLUDED = {
    "tivelo-desktop",
    "research/engine/installation",
    ".desktop.lock",
    ".mining_process.lock",
    "research/engine/owner/.mining_process.lock",
    "research/engine/data/reader-locks",
    "webview/SingletonLock",
    "webview/SingletonSocket",
    "webview/SingletonCookie",
}


def sync_dir(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _raise(error):
    raise error


def _excluded(name):
    return name in _EXCLUDED or (
        name.startswith("research/engine/jobs/") and name.endswith("/.mining_process.lock")
    )


def inventory(root, *, source):
    entries = {}
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=_raise):
        for name in [*dirs, *files]:
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if _excluded(relative):
                if name in dirs:
                    dirs.remove(name)
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                if name in dirs:
                    dirs.remove(name)
                link = os.readlink(path)
                resolved = path.resolve()
                if source and (
                    os.path.isabs(link)
                    or not resolved.is_relative_to(root)
                    or any(
                        _excluded(p.as_posix())
                        for p in (resolved.relative_to(root), *resolved.relative_to(root).parents)
                    )
                ):
                    raise ValueError(f"工作区链接指向迁移范围外：{relative}")
                entries[relative] = ("link", link)
            elif getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"工作区包含不支持的重解析路径：{relative}")
            elif stat.S_ISDIR(info.st_mode):
                entries[relative] = ("dir",)
            elif stat.S_ISREG(info.st_mode):
                entries[relative] = (
                    "file",
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    info.st_mode,
                )
            else:
                raise ValueError(f"工作区含非普通文件：{relative}")
    return entries


def _sqlite_files(root, entries):
    databases = set()
    for name, info in entries.items():
        if info[0] == "file" and info[3] >= 16:
            with (root / name).open("rb") as stream:
                if stream.read(16) == b"SQLite format 3\x00":
                    databases.add(name)
    return databases


def _sidecars(databases):
    return {db + suffix for db in databases for suffix in ("-wal", "-shm", "-journal")}


def _checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def _copy_file(source, target, info):
    expected = _checksum(source)
    mode = stat.S_IMODE(info[6]) & 0o777
    if (
        target.is_file()
        and target.stat().st_size == info[3]
        and target.stat().st_nlink == 1
        and _checksum(target) == expected
    ):
        stamp = target.stat()
        if stat.S_IMODE(stamp.st_mode) != mode or stamp.st_mtime_ns != info[4]:
            target.chmod(0o600)
            with target.open("r+b") as stream:
                os.utime(target, ns=(stamp.st_atime_ns, info[4]))
                target.chmod(mode)
                os.fsync(stream.fileno())
        return True
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as out:
        staged = Path(out.name)
    try:
        with source.open("rb") as src, staged.open("wb") as out:
            shutil.copyfileobj(src, out, length=1024 * 1024)
            out.flush()
            os.utime(staged, ns=(info[4], info[4]))
            staged.chmod(mode)
            os.fsync(out.fileno())
        if staged.stat().st_size != info[3] or _checksum(staged) != expected:
            raise ValueError("来源文件在复制期间改变")
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
    return False


def _copy_database(source, target):
    # The separate BEGIN IMMEDIATE reservation permits this reader and excludes writers,
    # including a standalone DataEngine. SQLite incorporates every committed WAL page.
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".db-copy-") as tmp:
        copied = Path(tmp) / "snapshot"
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(copied)) as duplicate,
        ):
            original.backup(duplicate)
            duplicate.execute("PRAGMA journal_mode=DELETE")
            if duplicate.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("来源 SQLite 索引未通过完整性核对")
        with copied.open("r+b") as stream:
            copied.chmod(stat.S_IMODE(source.stat().st_mode) & 0o777)
            os.fsync(stream.fileno())
        os.replace(copied, target)


@contextmanager
def quiescent_copy(source, draft, *, hold_until=None):
    """The caller owns draft and holds both desktop/worker leases until activation."""
    entries = inventory(source, source=True)
    databases = _sqlite_files(source, entries)
    sidecars = _sidecars(databases)
    expected = {k: v for k, v in entries.items() if k not in sidecars}
    with ExitStack() if hold_until is None else nullcontext(hold_until) as locks:
        for name in sorted(databases):
            path = source / name
            db = locks.enter_context(
                closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0))
            )
            db.execute("BEGIN IMMEDIATE")
        # A writer may have committed since discovery. Take the baseline under the locks.
        locked = {k: v for k, v in inventory(source, source=True).items() if k not in sidecars}
        if locked != expected:
            raise ValueError("来源在取得数据库锁时已改变，请重试")
        existing = inventory(draft, source=False)
        for name in sorted(existing, key=lambda value: value.count("/"), reverse=True):
            before, after = existing[name], expected.get(name)
            if after is None or before[0] != after[0] or (before[0] == "link" and before != after):
                path = draft / name
                path.rmdir() if before[0] == "dir" else path.unlink()
        reused = 0
        directories = [draft]
        for name, info in expected.items():
            target = draft / name
            if info[0] == "dir":
                target.mkdir(mode=0o700, exist_ok=True)
                directories.append(target)
            elif info[0] == "link":
                if not target.is_symlink():
                    target.symlink_to(info[1], target_is_directory=(source / name).is_dir())
            elif name in databases:
                _copy_database(source / name, target)
            else:
                reused += _copy_file(source / name, target, info)
        for path in reversed(directories):
            sync_dir(path)
        yield {
            "files": sum(v[0] == "file" for v in expected.values()),
            "databases": len(databases),
            "bytes": sum(v[3] for v in expected.values() if v[0] == "file"),
            "reused_files": reused,
        }
        final = {k: v for k, v in inventory(source, source=True).items() if k not in sidecars}
        if final != expected:
            raise ValueError("来源在复制期间改变，保留原工作区，请重试")
