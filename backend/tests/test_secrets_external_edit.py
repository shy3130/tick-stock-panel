"""secrets.json 作为统一配置源的外部编辑契约。

用户可以直接用编辑器修改 secrets.json (AI + 数据源 Key 同文件), 保存后
下一次请求即读取最新内容 —— 不需要重启服务。这里锁定三条:
① file_path() 指向 data/user_data/secrets.json;
② 外部改写文件后 load()/get_ai_config() 立刻反映新值 (mtime+size 签名缓存失效);
③ GET /api/settings 暴露 secrets_path 供设置页展示。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import secrets_store


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    yield tmp_path


def test_file_path_points_to_user_data_secrets(data_dir: Path) -> None:
    assert secrets_store.file_path() == data_dir / "user_data" / "secrets.json"


def test_external_edit_is_picked_up_without_restart(data_dir: Path) -> None:
    secrets_store.save({"ai_model": "old-model", "ai_base_url": "https://old.example/v1"})
    assert secrets_store.get_ai_config("ai_model") == "old-model"

    # 模拟用户编辑器直接覆写文件 (绕过 secrets_store.save)。
    # 内容长度与 mtime 都变化, 签名缓存必然失效。
    path = data_dir / "user_data" / "secrets.json"
    path.write_text(json.dumps({
        "ai_model": "brand-new-model",
        "ai_base_url": "https://new.example/v1",
        "ai_api_key": "sk-new",
    }, indent=2), encoding="utf-8")

    assert secrets_store.get_ai_config("ai_model") == "brand-new-model"
    assert secrets_store.get_ai_config("ai_base_url") == "https://new.example/v1"
    assert secrets_store.get_ai_key() == "sk-new"


def test_external_edit_with_same_size_still_reloads(data_dir: Path) -> None:
    """同长度改写: 签名含 mtime_ns, 不能因 size 相同而吃到旧缓存。"""
    import os
    import time

    path = data_dir / "user_data" / "secrets.json"
    secrets_store.save({"ai_model": "model-aaaa"})
    assert secrets_store.get_ai_config("ai_model") == "model-aaaa"

    path.write_text(json.dumps({"ai_model": "model-bbbb"}), encoding="utf-8")
    # 强制 mtime 前进 (文件系统时间戳粒度可能吞掉快速连续写入)
    future = time.time() + 5
    os.utime(path, (future, future))

    assert secrets_store.get_ai_config("ai_model") == "model-bbbb"


def test_get_settings_exposes_secrets_path(data_dir: Path) -> None:
    from app.api import settings as settings_api

    payload = settings_api.get_settings()

    assert payload["secrets_path"].endswith("secrets.json")
    assert str(data_dir) in payload["secrets_path"] or Path(payload["secrets_path"]).name == "secrets.json"
