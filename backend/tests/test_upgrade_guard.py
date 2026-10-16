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
    backup = data / "upgrade_guard" / "live" / "app" / "a.py"
    assert backup.read_text(encoding="utf-8") == "A_USER\n"


def test_live_backup_follows_reverts(prog: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_guard, "__version__", "v1.0.0")
    data = tmp_path / "data"
    f = prog / "app" / "a.py"
    f.write_text("A_USER\n", encoding="utf-8")
    guard = upgrade_guard.UpgradeGuard(data, root=prog)
    guard.scan()
    assert (data / "upgrade_guard" / "live" / "app" / "a.py").exists()

    f.write_text("A_ORIG\n", encoding="utf-8")  # 用户还原 → 备份应清掉该条目
    guard.scan()
    assert not (data / "upgrade_guard" / "live" / "app" / "a.py").exists()


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
    (prog / ".env").write_text("SECRET=1", encoding="utf-8")
    manifest = _generate_manifest.generate(prog, version="v0")
    rels = set(manifest["files"])
    assert "app/__pycache__/x.pyc" not in rels
    assert ".env" not in rels
    assert {"app/a.py", "static/index.html"} <= rels
