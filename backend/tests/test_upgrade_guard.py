"""二次开发升级保护 — 扫描分类 / 常备备份 / 升级报告 / 恢复的契约测试。

模拟完整生命周期: v1 基线 → 用户改动 (v1 扫描备份) → 模拟升级到 v2
(改写磁盘与清单) → v2 扫描生成对比报告 → 按类恢复。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.services import upgrade_guard

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "generate_manifest", _REPO_ROOT / "scripts" / "generate_manifest.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_generate_manifest = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("generate_manifest", _generate_manifest)
_SPEC.loader.exec_module(_generate_manifest)


def _write_manifest(prog: Path, version: str) -> None:
    manifest = _generate_manifest.generate(prog, version=version)
    out = prog / "app" / "upgrade_manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def prog(tmp_path: Path) -> Path:
    """v1 程序目录: 三个清单内文件 + 静态页。"""
    p = tmp_path / "prog"
    (p / "app" / "custom").mkdir(parents=True)
    (p / "app" / "a.py").write_text("A_ORIG\n", encoding="utf-8")
    (p / "app" / "b.py").write_text("B_ORIG\n", encoding="utf-8")
    (p / "app" / "c.py").write_text("C_ORIG\n", encoding="utf-8")
    (p / "static").mkdir()
    (p / "static" / "index.html").write_text("<html></html>", encoding="utf-8")
    _write_manifest(p, "v1.0.0")
    return p


def test_disabled_without_manifest(tmp_path: Path) -> None:
    prog = tmp_path / "prog"
    (prog / "app").mkdir(parents=True)
    guard = upgrade_guard.UpgradeGuard(tmp_path / "data", root=prog)
    assert guard.root is None
    status = guard.scan()
    assert status["enabled"] is False
    assert status["reason"] == "manifest_missing"


def test_scan_classifies_and_backs_up(prog: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    data = tmp_path / "data"
    (prog / "app" / "a.py").write_text("A_USER\n", encoding="utf-8")          # 修改
    (prog / "app" / "custom" / "ext.py").write_text("EXT\n", encoding="utf-8")  # 新增
    (prog / "app" / "__pycache__").mkdir()
    (prog / "app" / "__pycache__" / "a.pyc").write_bytes(b"\x00")              # 排除项
    (prog / "data").mkdir(exist_ok=True)
    (prog / "data" / "inside.txt").write_text("x", encoding="utf-8")          # data 排除

    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    status = guard.scan()

    assert status["enabled"] is True
    assert status["modified"] == ["app/a.py"]
    # 静态文件在清单内且未改 → 不出现; __pycache__/data 不算新增
    assert status["added"] == ["app/custom/ext.py"]
    backup = guard.live_dir / "app" / "a.py"
    assert backup.read_text(encoding="utf-8") == "A_USER\n"


def test_live_backup_follows_reverts(prog: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    data = tmp_path / "data"
    f = prog / "app" / "a.py"
    f.write_text("A_USER\n", encoding="utf-8")
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    guard.scan()
    assert (guard.live_dir / "app" / "a.py").exists()

    f.write_text("A_ORIG\n", encoding="utf-8")  # 用户还原 → 备份应清掉该条目
    guard.scan()
    assert not (guard.live_dir / "app" / "a.py").exists()


def test_upgrade_report_and_restore(prog: Path, tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data"

    # ── v1 阶段: 用户修改 a/b/c, 新增 e ──
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    (prog / "app" / "a.py").write_text("A_USER\n", encoding="utf-8")
    (prog / "app" / "b.py").write_text("B_USER\n", encoding="utf-8")
    (prog / "app" / "c.py").write_text("C_USER\n", encoding="utf-8")
    (prog / "app" / "e.py").write_text("E_USER\n", encoding="utf-8")
    guard1 = upgrade_guard.UpgradeGuard(data, root=prog)
    s1 = guard1.scan()
    assert s1["pending_report"] is None
    assert s1["modified"] == ["app/a.py", "app/b.py", "app/c.py"]

    # ── 模拟升级到 v2: 安装器覆盖程序目录 + 新清单 ──
    # a: 新版也改 (conflict); b: 新版没动, 被覆盖回基线 (restorable);
    # c: 新版内容 == 用户修改后内容 (match); e: 用户新增, 新版没有 (added_lost)
    monkeypatch.setattr(upgrade_guard, "__version__", "v2.0.0")
    (prog / "app" / "a.py").write_text("A_V2\n", encoding="utf-8")
    (prog / "app" / "b.py").write_text("B_ORIG\n", encoding="utf-8")
    (prog / "app" / "c.py").write_text("C_USER\n", encoding="utf-8")
    (prog / "app" / "e.py").unlink()
    _write_manifest(prog, "v2.0.0")

    guard2 = upgrade_guard.UpgradeGuard(data, root=prog)
    s2 = guard2.scan()
    report = s2["pending_report"]
    assert report is not None
    assert report["from_version"] == "v1.0.0"
    assert report["to_version"] == "v2.0.0"
    assert report["counts"] == {"restorable": 1, "conflict": 1, "match": 1, "added_lost": 1}
    cats = {row["relpath"]: row["category"] for row in report["files"]}
    assert cats == {
        "app/a.py": "conflict",
        "app/b.py": "restorable",
        "app/c.py": "match",
        "app/e.py": "added_lost",
    }

    # ── 恢复: b (restorable) 与 e (added_lost) 可回填用户版本 ──
    guard2.restore(report["id"], "app/b.py")
    assert (prog / "app" / "b.py").read_text(encoding="utf-8") == "B_USER\n"
    guard2.restore(report["id"], "app/e.py")
    assert (prog / "app" / "e.py").read_text(encoding="utf-8") == "E_USER\n"

    # 报告目录里保留了用户版本归档 (冲突项 a 的三方材料)
    archived = data / "upgrade_guard" / "reports" / report["id"] / "files" / "app" / "a.py"
    assert archived.read_text(encoding="utf-8") == "A_USER\n"

    # 确认后 pending_report 清空, 报告文件仍在
    guard2.dismiss_report()
    assert guard2.status()["pending_report"] is None
    assert archived.exists()


def test_restore_rejects_path_outside_report(prog: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    data = tmp_path / "data"
    (prog / "app" / "a.py").write_text("A_USER\n", encoding="utf-8")
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    guard.scan()

    monkeypatch.setattr(upgrade_guard, "__version__", "v2.0.0")
    (prog / "app" / "a.py").write_text("A_V2\n", encoding="utf-8")
    _write_manifest(prog, "v2.0.0")
    guard2 = upgrade_guard.UpgradeGuard(data, root=prog)
    report = guard2.scan()["pending_report"]
    assert report is not None

    with pytest.raises(ValueError):
        guard2.restore(report["id"], "../../etc/passwd")
    with pytest.raises(FileNotFoundError):
        guard2.restore("v0.0.0_to_v9.9.9_20990101_000000", "app/a.py")


def test_manifest_script_exclusions(prog: Path) -> None:
    """生成脚本与运行守卫的排除规则同集 — 运行期产物不入基线。"""
    (prog / "app" / "__pycache__").mkdir()
    (prog / "app" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (prog / "app" / "__pycache__" / "runtime-marker").write_text("temporary", encoding="utf-8")
    (prog / ".env").write_text("SECRET=1", encoding="utf-8")
    manifest = _generate_manifest.generate(prog, version="v0")
    rels = set(manifest["files"])
    assert "app/__pycache__/x.pyc" not in rels
    assert "app/__pycache__/runtime-marker" not in rels
    assert ".env" not in rels
    assert {"app/a.py", "static/index.html"} <= rels


def test_repeated_backups_keep_only_latest_snapshot(prog, tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    data = tmp_path / "data"
    extra = prog / "app" / "custom" / "extra.py"
    extra.write_text("extra", encoding="utf-8")
    for number in range(4):
        (prog / "app" / "a.py").write_text(f"USER_{number}", encoding="utf-8")
        if number == 2:
            extra.unlink()
        guard = upgrade_guard.UpgradeGuard(data, root=prog)
        status = guard.scan()
        assert (guard.live_dir / "app" / "a.py").read_text() == f"USER_{number}"
        assert (guard.live_dir / "app" / "custom" / "extra.py").exists() is (number < 2)
        assert Path(status["backup_dir"]) == guard.live_dir
        assert list((guard.state_dir / "snapshots").iterdir()) == [guard.live_dir]
        assert ("app/custom/extra.py" in guard._load_state()["file_cache"]) is (number < 2)


def test_report_failure_preserves_previous_backup_and_retries(prog, tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    (prog / "app" / "a.py").write_text("USER", encoding="utf-8")
    previous = upgrade_guard.UpgradeGuard(data, root=prog)
    previous.scan()
    saved_state = previous._state_path.read_bytes()
    saved_backup = previous.live_dir

    monkeypatch.setattr(upgrade_guard, "__version__", "v2.0.0")
    (prog / "app" / "a.py").write_text("A_ORIG\n", encoding="utf-8")
    _write_manifest(prog, "v2.0.0")
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    with monkeypatch.context() as failure:
        def fail(*args):
            raise OSError("report write failed")
        failure.setattr(guard, "_build_report", fail)
        with pytest.raises(OSError, match="report write failed"):
            guard.scan()
    assert guard._state_path.read_bytes() == saved_state
    assert (saved_backup / "app" / "a.py").read_text() == "USER"
    assert not guard.is_scanning()

    report = guard.scan()["pending_report"]
    assert report["from_version"] == "v1.0.0"
    assert report["counts"]["restorable"] == 1
    guard.restore(report["id"], "app/a.py")
    assert (prog / "app" / "a.py").read_text() == "USER"


@pytest.mark.parametrize("failure_stage", ["copy", "state", "hash"])
def test_failed_snapshot_keeps_previous_files_and_state(prog, tmp_path, monkeypatch, failure_stage):
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    guard = upgrade_guard.UpgradeGuard(tmp_path / "data", root=prog)
    for name in ("a.py", "b.py"):
        (prog / "app" / name).write_text("OLD_USER", encoding="utf-8")
    guard.scan()
    previous_dir = guard.live_dir
    previous_state = guard._state_path.read_bytes()
    (prog / "app" / "a.py").write_text("NEW_USER", encoding="utf-8")
    copy = upgrade_guard.shutil.copy2
    sha = upgrade_guard._sha256

    def failing_copy(src, dst, **kwargs):
        if Path(src).name == "b.py":
            Path(dst).write_text("partial", encoding="utf-8")
            raise PermissionError("copy failed")
        return copy(src, dst, **kwargs)

    def failing_hash(path):
        if path == prog / "app" / "a.py":
            raise PermissionError("hash failed")
        return sha(path)

    def failing_save(state):
        raise PermissionError("state failed")

    with monkeypatch.context() as failure:
        if failure_stage == "copy":
            failure.setattr(upgrade_guard.shutil, "copy2", failing_copy)
        elif failure_stage == "hash":
            failure.setattr(upgrade_guard, "_sha256", failing_hash)
        else:
            failure.setattr(guard, "_save_state", failing_save)
        with pytest.raises(PermissionError):
            guard.scan()
    assert guard._state_path.read_bytes() == previous_state
    for name in ("a.py", "b.py"):
        assert (previous_dir / "app" / name).read_text() == "OLD_USER"
    assert list((guard.state_dir / "snapshots").iterdir()) == [previous_dir]
    assert not guard.is_scanning()


def test_upgrade_archives_keep_latest_after_repeated_scans_and_dismiss(prog, tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    (prog / "app" / "a.py").write_text("USER_1", encoding="utf-8")
    upgrade_guard.UpgradeGuard(data, root=prog).scan()
    previous_report_dir = None
    for number in (2, 3, 4):
        version = f"v{number}.0.0"
        monkeypatch.setattr(upgrade_guard, "__version__", version)
        (prog / "app" / "a.py").write_text("A_ORIG\n", encoding="utf-8")
        _write_manifest(prog, version)
        guard = upgrade_guard.UpgradeGuard(data, root=prog)
        report = guard.scan()["pending_report"]
        archive = Path(report["dir"])
        assert list(guard.reports_dir.iterdir()) == [archive]
        assert (archive / "files" / "app" / "a.py").read_text() == f"USER_{number - 1}"
        if previous_report_dir is not None:
            assert not previous_report_dir.exists()
        previous_report_dir = archive
        guard.restore(report["id"], "app/a.py")
        (prog / "app" / "a.py").write_text(f"USER_{number}", encoding="utf-8")
        guard.scan()
        assert guard.status()["pending_report"]["id"] == report["id"]
        guard.dismiss_report()
        restarted = upgrade_guard.UpgradeGuard(data, root=prog)
        assert restarted.scan()["pending_report"] is None
        assert list(guard.reports_dir.iterdir()) == [archive]


def test_failed_upgrade_state_commit_preserves_previous_archive(prog, tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    (prog / "app" / "a.py").write_text("USER_1", encoding="utf-8")
    upgrade_guard.UpgradeGuard(data, root=prog).scan()
    monkeypatch.setattr(upgrade_guard, "__version__", "v2.0.0")
    _write_manifest(prog, "v2.0.0")
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    old_report = guard.scan()["pending_report"]
    (prog / "app" / "a.py").write_text("USER_2", encoding="utf-8")
    guard.scan()
    old_snapshot = guard.live_dir
    state = guard._state_path.read_bytes()

    monkeypatch.setattr(upgrade_guard, "__version__", "v3.0.0")
    _write_manifest(prog, "v3.0.0")
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    def fail(state):
        raise OSError("state commit failed")
    monkeypatch.setattr(guard, "_save_state", fail)
    with pytest.raises(OSError, match="state commit failed"):
        guard.scan()
    assert guard._state_path.read_bytes() == state
    assert (old_snapshot / "app" / "a.py").read_text() == "USER_2"
    assert list(guard.reports_dir.iterdir()) == [Path(old_report["dir"])]
    assert (Path(old_report["dir"]) / "files" / "app" / "a.py").read_text() == "USER_1"


def test_legacy_backup_and_historical_reports_migrate_without_touching_manual_exports(prog, tmp_path, monkeypatch):
    data = tmp_path / "data"
    base = data / "upgrade_guard"
    legacy = base / "live" / "app" / "a.py"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("LEGACY_USER", encoding="utf-8")
    manifest = json.loads((prog / "app" / upgrade_guard.MANIFEST_NAME).read_text())
    (base / "state.json").write_text(json.dumps({"last_version": "v1.0.0", "manifest_snapshot": manifest["files"]}), encoding="utf-8")
    old_report = base / "reports" / "v0.0.0_to_v1.0.0_20260101_000000"
    old_report.mkdir(parents=True)
    (old_report / "report.json").write_text('{"id": "old"}', encoding="utf-8")
    manual = data / "backups" / "manual.zip"
    manual.parent.mkdir()
    manual.write_bytes(b"manual export")
    orphan = base / "snapshots" / ("a" * 32)
    orphan.mkdir(parents=True)
    (orphan / "partial.py").write_text("interrupted write", encoding="utf-8")

    monkeypatch.setattr(upgrade_guard, "__version__", "v2.0.0")
    _write_manifest(prog, "v2.0.0")
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    report = guard.scan()["pending_report"]
    assert (Path(report["dir"]) / "files" / "app" / "a.py").read_text() == "LEGACY_USER"
    assert not (base / "live").exists()
    assert not old_report.exists()
    assert not orphan.exists()
    assert manual.read_bytes() == b"manual export"
    assert list(guard.reports_dir.iterdir()) == [Path(report["dir"])]


@pytest.mark.parametrize("stage", ["copy", "report_write"])
def test_partial_report_is_discarded_without_changing_backup(prog, tmp_path, monkeypatch, stage):
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    (prog / "app" / "a.py").write_text("USER", encoding="utf-8")
    data = tmp_path / "data"
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    guard.scan()
    previous_dir = guard.live_dir
    state = guard._state_path.read_bytes()
    monkeypatch.setattr(upgrade_guard, "__version__", "v2.0.0")
    _write_manifest(prog, "v2.0.0")
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    copy = upgrade_guard.shutil.copy2
    write = upgrade_guard.atomic_write_text

    def fail_copy(src, dst, **kwargs):
        if any(part.startswith(".report-") for part in Path(dst).parts):
            Path(dst).write_text("partial", encoding="utf-8")
            raise PermissionError("report copy failed")
        return copy(src, dst, **kwargs)

    def fail_write(path, text, **kwargs):
        if path.name == "report.json":
            raise PermissionError("report write failed")
        return write(path, text, **kwargs)

    monkeypatch.setattr(upgrade_guard.shutil, "copy2", fail_copy if stage == "copy" else copy)
    monkeypatch.setattr(upgrade_guard, "atomic_write_text", fail_write if stage == "report_write" else write)
    with pytest.raises(PermissionError):
        guard.scan()
    assert guard._state_path.read_bytes() == state
    assert (previous_dir / "app" / "a.py").read_text() == "USER"
    assert list(guard.reports_dir.iterdir()) == []


def test_corrupt_state_does_not_replace_previous_snapshot(prog, tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    (prog / "app" / "a.py").write_text("USER", encoding="utf-8")
    guard = upgrade_guard.UpgradeGuard(tmp_path / "data", root=prog)
    guard.scan()
    previous = guard.live_dir
    guard._state_path.write_text("broken state", encoding="utf-8")
    with pytest.raises(OSError, match="状态损坏"):
        guard.scan()
    assert (previous / "app" / "a.py").read_text() == "USER"
    assert guard._state_path.read_text() == "broken state"


def test_report_cleanup_does_not_follow_symlink(prog, tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    guard = upgrade_guard.UpgradeGuard(tmp_path / "data", root=prog)
    guard.state_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    historical = outside / "v0_to_v1_20260101_000000"
    historical.mkdir(parents=True)
    (historical / "sample.py").write_text("outside", encoding="utf-8")
    try:
        guard.reports_dir.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前系统不允许创建目录符号链接")
    with pytest.raises(ValueError, match="符号链接"):
        guard.scan()
    assert (historical / "sample.py").read_text() == "outside"
    assert not guard._state_path.exists()


def test_failed_restore_leaves_program_file_intact(prog, tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    target = prog / "app" / "a.py"
    target.write_text("USER", encoding="utf-8")
    upgrade_guard.UpgradeGuard(data, root=prog).scan()
    monkeypatch.setattr(upgrade_guard, "__version__", "v2.0.0")
    target.write_text("NEW_VERSION", encoding="utf-8")
    _write_manifest(prog, "v2.0.0")
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    report = guard.scan()["pending_report"]

    def fail_copy(src, dst, **kwargs):
        Path(dst).write_text("partial", encoding="utf-8")
        raise PermissionError("restore copy failed")

    monkeypatch.setattr(upgrade_guard.shutil, "copy2", fail_copy)
    with pytest.raises(PermissionError):
        guard.restore(report["id"], "app/a.py")
    assert target.read_text() == "NEW_VERSION"
    assert not list(target.parent.glob(".upgrade-restore-*.tmp"))


def test_api_scan_failure_returns_retryable_error_and_keeps_status(prog, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.upgrade_guard import router

    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    (prog / "app" / "a.py").write_text("USER", encoding="utf-8")
    guard = upgrade_guard.UpgradeGuard(tmp_path / "data", root=prog)
    initial = guard.scan()
    app = FastAPI()
    app.include_router(router)
    app.state.upgrade_guard = guard
    client = TestClient(app)
    with guard._operation():
        assert client.post("/api/upgrade-guard/scan").status_code == 409
        assert client.post("/api/upgrade-guard/dismiss-report").status_code == 409
        assert client.post("/api/upgrade-guard/restore", json={"report_id": "any", "relpath": "app/a.py"}).status_code == 409

    def fail(files):
        raise PermissionError("private diagnostic")

    with monkeypatch.context() as failure:
        failure.setattr(guard, "_sync_live", fail)
        response = client.post("/api/upgrade-guard/scan")
    assert response.status_code == 503
    assert "重试" in response.json()["detail"]
    assert "private diagnostic" not in response.text
    assert client.get("/api/upgrade-guard/status").json()["backup_dir"] == initial["backup_dir"]
    assert client.post("/api/upgrade-guard/scan").status_code == 200
    assert not Path(initial["backup_dir"]).exists()


def test_api_disabled_status(prog, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.upgrade_guard import router

    (prog / "app" / upgrade_guard.MANIFEST_NAME).unlink()
    app = FastAPI()
    app.include_router(router)
    app.state.upgrade_guard = upgrade_guard.UpgradeGuard(tmp_path / "data", root=prog)
    assert TestClient(app).get("/api/upgrade-guard/status").json() == {
        "enabled": False, "reason": "manifest_missing", "scanning": False,
    }


def test_unreadable_directory_does_not_erase_previous_backup(prog, tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    (prog / "app" / "a.py").write_text("USER", encoding="utf-8")
    guard = upgrade_guard.UpgradeGuard(tmp_path / "data", root=prog)
    guard.scan()
    previous = guard.live_dir
    state = guard._state_path.read_bytes()

    def fail_walk(root, *, onerror):
        onerror(PermissionError("unreadable app directory"))
        yield  # pragma: no cover — 保持生成器形态, 报错后不应遍历。

    monkeypatch.setattr(upgrade_guard.os, "walk", fail_walk)
    with pytest.raises(PermissionError):
        guard.scan()
    assert guard._state_path.read_bytes() == state
    assert (previous / "app" / "a.py").read_text() == "USER"


def test_cleanup_failure_keeps_committed_snapshot_and_can_retry(prog, tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    target = prog / "app" / "a.py"
    target.write_text("USER_1", encoding="utf-8")
    guard = upgrade_guard.UpgradeGuard(tmp_path / "data", root=prog)
    guard.scan()
    previous = guard.live_dir
    target.write_text("USER_2", encoding="utf-8")
    remove = upgrade_guard.shutil.rmtree

    def fail_remove(path, *args, **kwargs):
        if Path(path) == previous:
            raise PermissionError("backup in use")
        return remove(path, *args, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(upgrade_guard.shutil, "rmtree", fail_remove)
        with pytest.raises(PermissionError):
            guard.scan()
    assert guard.live_dir != previous
    assert (guard.live_dir / "app" / "a.py").read_text() == "USER_2"
    assert Path(guard.status()["backup_dir"]) == guard.live_dir
    assert (previous / "app" / "a.py").read_text() == "USER_1"
    guard.scan()
    assert list((guard.state_dir / "snapshots").iterdir()) == [guard.live_dir]


def test_unreadable_old_backup_stops_upgrade(prog, tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    (prog / "app" / "a.py").write_text("USER", encoding="utf-8")
    data = tmp_path / "data"
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    guard.scan()
    previous = guard.live_dir
    state = guard._state_path.read_bytes()
    monkeypatch.setattr(upgrade_guard, "__version__", "v2.0.0")
    _write_manifest(prog, "v2.0.0")
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    walk = upgrade_guard.os.walk

    def fail_walk(root, **kwargs):
        if root == previous:
            kwargs["onerror"](PermissionError("old backup unreadable"))
        yield from walk(root, **kwargs)

    monkeypatch.setattr(upgrade_guard.os, "walk", fail_walk)
    with pytest.raises(PermissionError):
        guard.scan()
    assert guard._state_path.read_bytes() == state
    assert (previous / "app" / "a.py").read_text() == "USER"


def test_api_upgrade_restore_and_dismiss_refresh_current_backup(prog, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.upgrade_guard import router

    data = tmp_path / "data"
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    target = prog / "app" / "a.py"
    target.write_text("USER", encoding="utf-8")
    app.state.upgrade_guard = upgrade_guard.UpgradeGuard(data, root=prog)
    client = TestClient(app)
    assert client.post("/api/upgrade-guard/scan").status_code == 200

    monkeypatch.setattr(upgrade_guard, "__version__", "v2.0.0")
    target.write_text("A_ORIG\n", encoding="utf-8")
    _write_manifest(prog, "v2.0.0")
    app.state.upgrade_guard = guard = upgrade_guard.UpgradeGuard(data, root=prog)
    report = client.post("/api/upgrade-guard/scan").json()["pending_report"]
    assert report["counts"]["restorable"] == 1
    assert client.post("/api/upgrade-guard/restore", json={"report_id": "../outside", "relpath": "app/a.py"}).status_code == 400
    assert client.post("/api/upgrade-guard/restore", json={"report_id": "missing", "relpath": "app/a.py"}).status_code == 404
    assert client.post("/api/upgrade-guard/restore", json={"report_id": report["id"], "relpath": "../outside"}).status_code == 400
    assert client.post("/api/upgrade-guard/restore", json={"report_id": report["id"], "relpath": "app/a.py"}).status_code == 200
    status = client.get("/api/upgrade-guard/status").json()
    assert status["modified"] == ["app/a.py"]
    assert (Path(status["backup_dir"]) / "app" / "a.py").read_text() == "USER"
    assert status["pending_report"]["id"] == report["id"]
    assert client.post("/api/upgrade-guard/dismiss-report").status_code == 200
    assert client.get("/api/upgrade-guard/status").json()["pending_report"] is None
    assert list(guard.reports_dir.iterdir()) == [Path(report["dir"])]


def test_custom_data_directory_is_never_backed_up_into_itself(prog, monkeypatch):
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    (prog / "app" / "a.py").write_text("USER", encoding="utf-8")
    data = prog / "custom-runtime"
    data.mkdir()
    (data / "manual.zip").write_bytes(b"manual export")
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    for _ in range(3):
        status = guard.scan()
        assert status["modified"] == ["app/a.py"]
        assert status["added"] == []
        assert sorted(path.relative_to(guard.live_dir).as_posix() for path in guard.live_dir.rglob("*") if path.is_file()) == ["app/a.py"]
        assert list((guard.state_dir / "snapshots").iterdir()) == [guard.live_dir]
    assert (data / "manual.zip").read_bytes() == b"manual export"
