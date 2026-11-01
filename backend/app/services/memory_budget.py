"""Process-visible memory budget, including Linux cgroup ancestors.

System counters keep their psutil semantics; admission and batch sizing use the
smaller container budget. No cgroup files are written by this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import psutil

_PROC_SELF = Path("/proc/self")


@dataclass(frozen=True)
class MemoryBudget:
    total_bytes: int
    available_bytes: int


def _read_int(path: Path) -> int | None:
    try:
        value = int(path.read_text().strip())
        return value if value >= 0 else None
    except (OSError, ValueError):
        return None


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def _memory_groups() -> list[tuple[Path, Path, bool]]:
    try:
        membership = _PROC_SELF.joinpath("cgroup").read_text().splitlines()
        mounts = _PROC_SELF.joinpath("mountinfo").read_text().splitlines()
    except OSError:
        return []
    groups = []
    for line in membership:
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        unified = parts[0] == "0" and not parts[1]
        if not unified and "memory" not in parts[1].split(","):
            continue
        member = Path(_unescape(parts[2]))
        if not member.is_absolute() or ".." in member.parts:
            continue
        for mount in mounts:
            before, sep, after = mount.partition(" - ")
            fields, filesystem = before.split(), after.split()
            if not sep or len(fields) < 5 or len(filesystem) < 3:
                continue
            if filesystem[0] != ("cgroup2" if unified else "cgroup"):
                continue
            if not unified and "memory" not in filesystem[2].split(","):
                continue
            root, point = Path(_unescape(fields[3])), Path(_unescape(fields[4]))
            try:
                relative = member.relative_to(root)
            except ValueError:
                continue
            groups.append((point / relative, point, unified))
    return groups


def memory_budget(vm=None) -> MemoryBudget:
    vm = psutil.virtual_memory() if vm is None else vm
    total = int(getattr(vm, "total", vm.available))
    available = int(vm.available)
    for leaf, mount, unified in _memory_groups():
        current = leaf
        while True:
            usage = _read_int(current / ("memory.current" if unified else "memory.usage_in_bytes"))
            names = ("memory.max", "memory.high") if unified else ("memory.limit_in_bytes",)
            for name in names:
                limit = _read_int(current / name)
                # 'max', unreadable controls and v1's huge unlimited sentinel
                # cannot tighten the host budget. Include ancestor usage because
                # sibling processes consume the same parent allocation.
                if limit is not None and limit < int(getattr(vm, "total", vm.available)):
                    total = min(total, limit)
                    if usage is not None:
                        available = min(available, max(0, limit - usage))
            if current == mount:
                break
            current = current.parent
    return MemoryBudget(total, min(total, max(0, available)))
