from types import SimpleNamespace

import psutil
import pytest

from app.indicators import pipeline
from app.services import heavy_job_limiter, memory_budget

GIB = 1024**3


@pytest.fixture
def cgroup(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    mount = tmp_path / "cgroup"
    mount.mkdir()
    (proc / "cgroup").write_text("0::/parent/job\n")
    (proc / "mountinfo").write_text(f"1 0 0:1 / {mount} rw - cgroup2 cgroup rw\n")
    (mount / "parent/job").mkdir(parents=True)
    monkeypatch.setattr(memory_budget, "_PROC_SELF", proc)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(total=32*GIB, available=16*GIB))
    return proc, mount


def write(group, **controls):
    for name, value in controls.items():
        (group / name.replace("_", ".")).write_text(str(value))


def test_parent_limit_accounts_for_sibling_usage_and_recovers(cgroup):
    _, mount = cgroup
    write(mount / "parent", memory_max=4*GIB, memory_current=3*GIB)
    write(mount / "parent/job", memory_max="max", memory_current=GIB)
    assert memory_budget.memory_budget() == memory_budget.MemoryBudget(4*GIB, GIB)
    write(mount / "parent", memory_current=GIB)
    assert memory_budget.memory_budget().available_bytes == 3*GIB


def test_memory_high_is_an_admission_budget(cgroup):
    _, mount = cgroup
    write(mount / "parent/job", memory_max=4*GIB, memory_high=2*GIB, memory_current=GIB)
    assert memory_budget.memory_budget() == memory_budget.MemoryBudget(2*GIB, GIB)


def test_heavy_gate_and_batch_size_use_container_budget(cgroup, monkeypatch):
    _, mount = cgroup
    write(mount / "parent/job", memory_max=2*GIB, memory_current=3*GIB//2)
    monkeypatch.setattr(heavy_job_limiter, "_HEAVY_MIN_AVAILABLE_BYTES", GIB)
    assert not heavy_job_limiter._memory_admission_ok()
    assert pipeline._adaptive_sym_batch(1000, 1500) == 100
    write(mount / "parent/job", memory_current=GIB//2, memory_max=16*GIB)
    assert heavy_job_limiter._memory_admission_ok()
    assert pipeline._adaptive_sym_batch(1000, 1500) == 1000


def test_v1_bind_mount_and_unlimited_sentinel(cgroup):
    proc, mount = cgroup
    (proc / "cgroup").write_text("2:cpu:/other\n4:memory:/docker/app\n")
    (proc / "mountinfo").write_text(f"1 0 0:1 /docker/app {mount} rw - cgroup cgroup rw,memory\n")
    (mount / "memory.limit_in_bytes").write_text(str(2*GIB))
    (mount / "memory.usage_in_bytes").write_text(str(GIB))
    assert memory_budget.memory_budget() == memory_budget.MemoryBudget(2*GIB, GIB)
    (mount / "memory.limit_in_bytes").write_text(str(2**63-4096))
    assert memory_budget.memory_budget() == memory_budget.MemoryBudget(32*GIB, 16*GIB)


def test_missing_or_malformed_controls_preserve_host_fallback(cgroup):
    proc, mount = cgroup
    write(mount / "parent/job", memory_max="bad", memory_current=GIB)
    assert memory_budget.memory_budget() == memory_budget.MemoryBudget(32*GIB, 16*GIB)
    (proc / "mountinfo").unlink()
    assert memory_budget.memory_budget().available_bytes == 16*GIB


def test_exhausted_limit_never_returns_negative_available(cgroup):
    _, mount = cgroup
    write(mount / "parent/job", memory_max=GIB, memory_current=2*GIB)
    assert memory_budget.memory_budget().available_bytes == 0


def test_health_keeps_host_counters_and_exposes_effective_budget(cgroup, monkeypatch):
    from app.api.routes import _memory_snapshot

    _, mount = cgroup
    write(mount / "parent/job", memory_max=2*GIB, memory_current=GIB)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(total=32*GIB, available=16*GIB, percent=50))
    snapshot = _memory_snapshot()
    assert snapshot["system_total_bytes"] == 32*GIB
    assert snapshot["system_available_bytes"] == 16*GIB
    assert snapshot["effective_total_bytes"] == 2*GIB
    assert snapshot["effective_available_bytes"] == GIB


def test_escaped_mount_path_and_host_pressure(cgroup, monkeypatch):
    proc, mount = cgroup
    moved = mount.with_name("group with space")
    mount.rename(moved)
    escaped = str(moved).replace(" ", r"\040")
    (proc / "mountinfo").write_text(f"1 0 0:1 / {escaped} rw - cgroup2 cgroup rw\n")
    write(moved / "parent/job", memory_max=4*GIB, memory_current=GIB)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(total=32*GIB, available=GIB//2))
    assert memory_budget.memory_budget() == memory_budget.MemoryBudget(4*GIB, GIB//2)
