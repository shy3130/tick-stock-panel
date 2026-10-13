"""macOS 数据目录移出 .app 包 — 路径解析与包内旧数据迁移。

背景: ≤0.3.6 的 frozen data 目录 = exe 同级, mac 上落在 TSP.app 包内,
覆盖安装替换 App 时旧包(含数据)整体进废纸篓, 用户数据全部"清空"。
修复: darwin frozen 默认 → ~/Library/Application Support/TSP (包外),
启动时自动把包内/兄弟目录的旧数据搬过来。
"""
from __future__ import annotations

import sys
from pathlib import Path

from app import config as config_mod
from app.config import _user_data_root
from app.tickflow.repository import DataStore


# ── 路径解析 ─────────────────────────────────────────────────────────

def test_darwin_frozen_uses_app_support_outside_bundle(tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    monkeypatch.setattr(config_mod, "_IS_FROZEN", True)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: home)
    assert _user_data_root() == home / "Library" / "Application Support" / "TSP"


def test_windows_frozen_keeps_exe_sibling_data(tmp_path, monkeypatch):
    exe = tmp_path / "TSP" / "TSP.exe"
    monkeypatch.setattr(config_mod, "_IS_FROZEN", True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(exe))
    assert _user_data_root() == exe.parent / "data"


def test_dev_mode_uses_project_root(monkeypatch):
    monkeypatch.setattr(config_mod, "_IS_FROZEN", False)
    assert _user_data_root() == config_mod._PROJECT_ROOT / "data"


# ── 迁移 (直接构造最小实例调方法, 不触发完整 __init__) ────────────────

def _repo_with_data_dir(new_dir: Path) -> DataStore:
    repo = DataStore.__new__(DataStore)
    repo.data_dir = new_dir
    return repo


def _make_real_data(root: Path) -> None:
    (root / "kline_daily_enriched" / "date=2026-10-10").mkdir(parents=True)
    (root / "kline_daily_enriched" / "date=2026-10-10" / "part.parquet").write_bytes(b"x")
    (root / "user_data").mkdir(parents=True)
    (root / "user_data" / "secrets.json").write_text("{}", encoding="utf-8")


def test_darwin_bundle_data_migrates_to_new_location(tmp_path, monkeypatch):
    """包内旧数据(用户从废纸篓恢复/从 dmg 运行过)首次启动自动搬出。"""
    new_dir = tmp_path / "newdata"
    new_dir.mkdir()
    bundle_data = tmp_path / "TSP.app" / "Contents" / "MacOS" / "data"
    _make_real_data(bundle_data)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", str(bundle_data.parent / "TSP"))

    _repo_with_data_dir(new_dir)._migrate_legacy_data_dir()

    assert (new_dir / "user_data" / "secrets.json").exists()
    assert (new_dir / "kline_daily_enriched" / "date=2026-10-10" / "part.parquet").exists()
    assert not bundle_data.exists()  # 旧包内目录已清空


def test_migration_skips_when_new_location_has_data(tmp_path, monkeypatch):
    """新位置已有真实数据 → 绝不覆盖 (用户已在两套路径都跑过)。"""
    new_dir = tmp_path / "newdata"
    _make_real_data(new_dir)
    (new_dir / "user_data" / "secrets.json").write_text('{"mine": 1}', encoding="utf-8")
    bundle_data = tmp_path / "TSP.app" / "Contents" / "MacOS" / "data"
    _make_real_data(bundle_data)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", str(bundle_data.parent / "TSP"))

    _repo_with_data_dir(new_dir)._migrate_legacy_data_dir()

    assert (new_dir / "user_data" / "secrets.json").read_text(encoding="utf-8") == '{"mine": 1}'
    assert bundle_data.exists()  # 旧数据原样保留


def test_migration_noop_without_legacy(tmp_path, monkeypatch):
    new_dir = tmp_path / "newdata"
    new_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "TSP.app" / "Contents" / "MacOS" / "TSP"))
    _repo_with_data_dir(new_dir)._migrate_legacy_data_dir()
    assert list(new_dir.iterdir()) == []


def test_windows_sibling_legacy_still_migrates(tmp_path, monkeypatch):
    """既有行为回归: Windows 旧版兄弟目录 TickFlowStockPanel_Data 照常搬迁。"""
    new_dir = tmp_path / "TSP" / "data"
    new_dir.mkdir(parents=True)
    legacy = tmp_path / "TSP" / "TickFlowStockPanel_Data"
    _make_real_data(legacy)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "TSP" / "TSP.exe"))

    _repo_with_data_dir(new_dir)._migrate_legacy_data_dir()

    assert (new_dir / "user_data" / "secrets.json").exists()
    assert not legacy.exists()


def test_migration_same_path_guard(tmp_path, monkeypatch):
    """防御: legacy 候选与当前 data_dir 同一路径时不动 (Windows 逻辑回归中的边界)。"""
    data_dir = tmp_path / "TSP" / "data"
    _make_real_data(data_dir)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(data_dir / "TSP.exe"))

    _repo_with_data_dir(data_dir)._migrate_legacy_data_dir()
    # 数据原样, 无自搬自
    assert (data_dir / "user_data" / "secrets.json").exists()


def test_dev_mode_never_migrates(tmp_path, monkeypatch):
    """非 frozen (开发/Docker) 直接返回, 即使存在旧目录也不动。"""
    new_dir = tmp_path / "newdata"
    new_dir.mkdir()
    legacy = new_dir.parent / "TickFlowStockPanel_Data"
    _make_real_data(legacy)

    monkeypatch.delattr(sys, "frozen", raising=False)
    _repo_with_data_dir(new_dir)._migrate_legacy_data_dir()
    assert legacy.exists()
    assert list(new_dir.iterdir()) == []
