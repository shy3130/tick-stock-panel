"""版本号单一来源: app.__version__ 运行时从 frontend/package.json 加载。

dev 模式读仓库文件, PyInstaller 冻结后读 _MEIPASS/package.json (tsp.spec 收集),
两者必须返回同一版本 —— 保证安装包、页面徽标、更新检查的"当前版本"永不漂移。
"""

import json
import sys
from pathlib import Path

import app as app_pkg
from app import __version__

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_JSON = REPO_ROOT / "frontend" / "package.json"


def test_dev_version_matches_package_json():
    """dev 模式: __version__ 与 frontend/package.json 完全一致。"""
    assert __version__ == json.loads(PKG_JSON.read_text(encoding="utf-8"))["version"]


def test_frozen_reads_mepass_package_json(monkeypatch, tmp_path):
    """frozen 模式: 从 _MEIPASS/package.json 读版本 (tsp.spec 收集的那份)。"""
    (tmp_path / "package.json").write_text(
        json.dumps({"version": "9.9.9"}), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert app_pkg._load_version() == "9.9.9"


def test_frozen_missing_file_falls_back(monkeypatch, tmp_path):
    """frozen 模式文件缺失/损坏: 回退硬编码值, 不抛异常 (版本读取不能阻塞启动)。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)  # 空目录
    assert app_pkg._load_version() == app_pkg._FALLBACK_VERSION
