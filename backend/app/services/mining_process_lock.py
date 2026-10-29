from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO


class MiningProcessLockError(RuntimeError):
    """Another application process owns mining for this data directory."""


_LOCK_SCHEMA_VERSION = 1

# 旧格式 (b"0") / 内容损坏时定位持有者的白名单线索: 只认本产品的进程形态,
# 避免把"恰好打开过锁文件"的无关进程 (杀毒扫描/编辑器) 当成持有者。
_APP_NAME_HINTS = ("python", "tsp", "tickflow")
_APP_CMDLINE_HINTS = (
    "uvicorn",
    "app.main",
    "app.desktop",
    "desktop.py",
    "tick-stock-panel",
    "tickflow",
    "panel",
)


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


def locate_lock_holder_pid(data_dir: Path | str) -> int | None:
    """锁内容缺失/旧格式时, 从操作系统层面定位实际打开锁文件的进程。

    flock 才是持锁事实, 但旧版写入的 b"0" 内容不含 pid, 只靠内容无法定位
    持有者 (旧版本残留后端进程的典型场景: 升级前启动的旧进程仍在跑)。
    这里用 lsof (POSIX, 精确) / psutil (跨平台兜底) 反查, 让「旧格式」也能
    走既有的人工确认强制释放流程, 而不是让用户去任务管理器里逐个猜。

    只返回本产品形态的进程 (python/uvicorn/desktop/tickflow), 并做二次确认:
    连续两次扫描都打开着同一文件才算数, 排除瞬时探测/扫描器的误判 (误杀
    健康进程是破坏性操作, 宁可定位失败也不误伤)。
    """
    target = Path(data_dir) / ".mining_process.lock"
    if not target.exists():
        return None
    if os.name != "nt":
        pids = _lsof_holder_pids(target)
        if pids:
            time.sleep(0.15)
            pids_again = _lsof_holder_pids(target)
            common = [pid for pid in pids if pid in pids_again]
            return common[0] if common else None
    return _psutil_holder_pid(target)


def _lsof_holder_pids(target: Path) -> list[int]:
    """lsof 反查持有该文件的 pid (macOS/Linux); lsof 不可用时返回空。"""
    try:
        completed = subprocess.run(
            ["lsof", "-t", "--", str(target)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    self_pid = os.getpid()
    pids: list[int] = []
    for token in completed.stdout.split():
        if not token.isdigit():
            continue
        pid = int(token)
        if pid != self_pid and pid not in pids:
            pids.append(pid)
    return pids


def _psutil_holder_pid(target: Path) -> int | None:
    """跨平台兜底: 遍历本产品形态的进程, 找打开锁文件的那个。"""
    try:
        import psutil
    except ImportError:  # pragma: no cover — psutil 是硬依赖, 仅防御性
        return None
    self_pid = os.getpid()
    try:
        preferred = os.path.normcase(os.path.realpath(str(target)))
    except OSError:
        preferred = os.path.normcase(str(target))
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        info = proc.info
        pid = info.get("pid")
        if not pid or pid == self_pid:
            continue
        if not _looks_like_app_process(info.get("name") or "", info.get("cmdline") or []):
            continue
        if not _process_has_open_file(proc, psutil, preferred):
            continue
        time.sleep(0.15)
        if _process_has_open_file(proc, psutil, preferred):
            return pid
    return None


def _looks_like_app_process(name: str, cmdline: list[str]) -> bool:
    lowered_name = name.lower()
    lowered_cmd = " ".join(cmdline).lower()
    return any(hint in lowered_name for hint in _APP_NAME_HINTS) or any(
        hint in lowered_cmd for hint in _APP_CMDLINE_HINTS
    )


def _process_has_open_file(proc, psutil, wanted: str) -> bool:
    try:
        for opened in proc.open_files():
            try:
                current = os.path.normcase(os.path.realpath(opened.path))
            except OSError:
                current = os.path.normcase(opened.path)
            if current == wanted:
                return True
    except (psutil.Error, OSError):
        return False
    return False


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
