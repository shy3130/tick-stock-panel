"""本机能力探测：给「只在特定平台可验证」的用例提供精确 skip 依据。

为什么不用 `sys.platform == "win32"` 一刀切：
1. Windows 开启开发者模式或管理员运行时**可以**建符号链接，一刀切会漏测；
2. 反过来，Linux 容器里也可能因 seccomp/权限建不了符号链接；
3. 但 Windows 的另外两条语义（不允许替换被句柄持有的文件、无 POSIX 执行位）
   是内核行为，只能靠平台判定。

因此本模块区分两类探测：**实测能力**（真跑一次）与**语义判定**（平台不可变行为）。
所有 skip 的理由必须写在这里，避免散落在各测试文件里各自表述。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


def _probe_symlink() -> bool:
    """真建一次符号链接。Windows 需开发者模式或管理员，否则 WinError 1314。"""
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.txt"
            dst = Path(td) / "dst.txt"
            src.write_text("x", encoding="utf-8")
            try:
                dst.symlink_to(src)
            except (OSError, NotImplementedError):
                return False
            return dst.is_symlink()
    except OSError:
        return False


def _probe_replace_open_file() -> bool:
    """能否用 os.replace 覆盖一个仍被别的句柄打开的文件。

    POSIX: rename 原子替换，允许；Windows: WinError 5 拒绝访问。
    原子写(part.parquet.tmp -> part.parquet)依赖此语义。
    """
    try:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target.bin"
            tmp = Path(td) / "tmp.bin"
            target.write_bytes(b"old")
            tmp.write_bytes(b"new")
            with target.open("rb") as holder:  # 保持句柄打开
                try:
                    os.replace(tmp, target)
                except OSError:
                    return False
                finally:
                    holder.close()
            return True
    except OSError:
        return False


def _probe_cgroup_memory() -> bool:
    """是否存在 cgroup 内存限制文件（容器内存预算语义的前提）。"""
    candidates = (
        "/sys/fs/cgroup/memory.max",                   # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
        "/sys/fs/cgroup/memory.high",
    )
    return any(Path(p).is_file() for p in candidates)


def _probe_posix_exec_bit() -> bool:
    """POSIX 执行位语义：0644 文件的 os.access(X_OK) 必须为 False。

    Windows 上 os.access(X_OK) 退化为存在性检查，恒为 True，
    无法表达「文件存在但不可执行」这一被测状态。
    """
    if os.name == "nt":
        return False
    try:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "noexec"
            p.write_text("x", encoding="utf-8")
            p.chmod(0o644)
            return not os.access(p, os.X_OK)
    except OSError:
        return False


IS_WINDOWS = os.name == "nt"
CAN_SYMLINK = _probe_symlink()
CAN_REPLACE_OPEN_FILE = _probe_replace_open_file()
HAS_CGROUP_MEMORY = _probe_cgroup_memory()
HAS_POSIX_EXEC_BIT = _probe_posix_exec_bit()

# ---- 可直接挂到测试/参数上的标记 ----

skip_no_symlink = pytest.mark.skipif(
    not CAN_SYMLINK,
    reason="本机无法创建符号链接 (Windows 需开发者模式或管理员)，该安全用例无法验证",
)

skip_no_atomic_replace = pytest.mark.skipif(
    not CAN_REPLACE_OPEN_FILE,
    reason="本平台不允许替换仍被句柄持有的文件 (Windows WinError 5)，"
           "原子替换语义与 POSIX rename 不同",
)

skip_no_cgroup = pytest.mark.skipif(
    not HAS_CGROUP_MEMORY,
    reason="本机无 cgroup 内存限制文件，容器内存预算/降级语义不适用",
)

skip_no_exec_bit = pytest.mark.skipif(
    not HAS_POSIX_EXEC_BIT,
    reason="本平台无 POSIX 执行位语义 (os.access X_OK 恒真)，不可执行文件无法区分",
)

skip_windows_path_semantics = pytest.mark.skipif(
    IS_WINDOWS,
    reason="Windows 把反斜杠视为路径分隔符，`a\\\\b.py` 是合法子路径而非穿越串；"
           "该拒绝规则针对 POSIX 解压器语义，需在 Linux 上验证",
)

skip_windows_text_eol = pytest.mark.skipif(
    IS_WINDOWS,
    reason="Windows 文本模式会把 \\n 写成 \\r\\n，字节级行尾比较必然失配；"
           "内容本身无误，需在 LF 环境验证",
)
