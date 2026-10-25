from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO


class MiningProcessLockError(RuntimeError):
    """Another application process owns mining for this data directory."""


_LOCK_SCHEMA_VERSION = 1


class MiningProcessLock:
    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / ".mining_process.lock"
        self._stream: BinaryIO | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self, *, role: str = "app") -> None:
        if self._stream is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        stream = self._path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            os.set_inheritable(stream.fileno(), False)
            _try_lock_file(stream)
        except BaseException:
            stream.close()
            raise
        self._stream = stream
        # flock 是唯一事实; 内容只是给诊断/强制释放用的持有者线索。
        # 持锁期间本进程是该文件唯一写入者, 无并发竞争。
        self._rewrite_locked(stream, holder_payload(role))

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        try:
            self._rewrite_locked(
                stream,
                {
                    "schema_version": _LOCK_SCHEMA_VERSION,
                    "pid": os.getpid(),
                    "released_at": datetime.now(UTC).isoformat(),
                },
            )
            _unlock_file(stream)
        finally:
            stream.close()

    @property
    def acquired(self) -> bool:
        return self._stream is not None

    @staticmethod
    def _rewrite_locked(stream: BinaryIO, payload: dict) -> None:
        try:
            stream.seek(0)
            stream.truncate()
            stream.write(
                (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
        except OSError:
            # 诊断信息写失败不影响锁语义 (flock 已生效/将生效)
            pass


def holder_payload(role: str) -> dict:
    return {
        "schema_version": _LOCK_SCHEMA_VERSION,
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
        "role": role,
        "host": socket.gethostname(),
    }


def read_lock_holder(data_dir: Path | str) -> dict | None:
    """读取锁文件中的持有者线索; 旧格式 ("0") / 损坏内容返回 None。

    返回值仅供参考: flock 才是持锁事实, 内容可能滞后 (例如持有者崩溃后
    内核自动释放锁但内容未更新)。
    """
    path = Path(data_dir) / ".mining_process.lock"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    pid = value.get("pid")
    if not isinstance(pid, int) or pid <= 0 or "released_at" in value:
        return None
    return value


def probe_lock_held(data_dir: Path | str) -> bool:
    """非阻塞探测锁是否被任何进程持有 (含本进程自己)。"""
    path = Path(data_dir) / ".mining_process.lock"
    try:
        stream = path.open("a+b")
    except OSError:
        return False
    try:
        os.set_inheritable(stream.fileno(), False)
        _try_lock_file(stream)
    except MiningProcessLockError:
        return True
    except OSError:
        # 无法探测时按未持锁处理, 让真正的 acquire 路径给出准确错误
        return False
    try:
        _unlock_file(stream)
    except OSError:
        pass
    finally:
        stream.close()
    return False


def _try_lock_file(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise MiningProcessLockError(
                "another application process already owns mining for this data directory"
            ) from exc
        return

    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise MiningProcessLockError(
            "another application process already owns mining for this data directory"
        ) from exc


def _unlock_file(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
