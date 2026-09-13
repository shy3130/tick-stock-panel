"""配置/凭证 JSON 存储的原子写测试 — 写到一半不该把已有配置换成空文件。

`fs_utils` 的模块 docstring 写着「新代码统一用本模块的 atomic_write_text」,
`lots.py`、`monitor_rules.py` 也都照做了; 但 secrets_store、auth、preferences、
ExtConfigStore 这四处还在裸 `write_text`。它们的 `load` 又都吞掉解析错误返回
默认值 (`{}` 或跳过该项), 所以半截文件不会报错, 只会安静地把配置清空。

这里用「写入过程中失败」模拟磁盘写满/断电: 让 `Path.write_text` 只写前几个
字节就抛 OSError。裸写会把目标文件本身截断; 原子写截断的是 .tmp, `os.replace`
不会执行, 目标文件原封不动。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import secrets_store
from app.services import preferences
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    preferences._invalidate_cache()
    yield tmp_path
    preferences._invalidate_cache()


@pytest.fixture()
def torn_write(monkeypatch: pytest.MonkeyPatch):
    """让下一次 write_text 只写前 8 个字节然后失败。"""
    real = Path.write_text

    def _torn(self: Path, data: str, *args, **kwargs):
        real(self, data[:8], *args, **kwargs)
        raise OSError(28, "No space left on device")

    def _arm() -> None:
        monkeypatch.setattr(Path, "write_text", _torn)

    return _arm


def test_preferences_survive_a_torn_write(data_dir: Path, torn_write) -> None:
    preferences.save({"theme": "dark", "kline_compress": True})
    assert preferences.load()["theme"] == "dark"

    torn_write()
    with pytest.raises(OSError):
        preferences.save({"theme": "light"})

    preferences._invalidate_cache()
    assert preferences.load() == {"theme": "dark", "kline_compress": True}


def test_secrets_survive_a_torn_write(data_dir: Path, torn_write) -> None:
    secrets_store.save({"tickflow_token": "keep-me"})
    assert secrets_store.load()["tickflow_token"] == "keep-me"

    torn_write()
    with pytest.raises(OSError):
        secrets_store.save({"tickflow_token": "replacement"})

    assert secrets_store.load() == {"tickflow_token": "keep-me"}


def test_ext_config_survives_a_torn_write(data_dir: Path, torn_write) -> None:
    store = ExtConfigStore(data_dir / "ext_data")
    config = ExtConfig(
        id="hot",
        label="人气",
        mode="timeseries",
        fields=[ExtField("symbol", "string"), ExtField("heat", "float")],
    )
    store.upsert(config)
    assert [c.id for c in store.load_all()] == ["hot"]

    config.label = "人气榜"
    torn_write()
    with pytest.raises(OSError):
        store.upsert(config)

    reloaded = ExtConfigStore(data_dir / "ext_data").load_all()
    assert [c.id for c in reloaded] == ["hot"]
    assert reloaded[0].label == "人气"


def test_a_normal_save_still_writes_what_it_was_given(data_dir: Path) -> None:
    """没有失败时行为不变 —— 内容、合并语义和文件位置都照旧。"""
    preferences.save({"theme": "dark"})
    preferences.save({"kline_compress": True})
    assert preferences.load() == {"theme": "dark", "kline_compress": True}

    secrets_store.save({"a": "1"})
    secrets_store.save({"b": "2"})
    assert secrets_store.load() == {"a": "1", "b": "2"}

    written = json.loads(
        (data_dir / "user_data" / "preferences.json").read_text(encoding="utf-8")
    )
    assert written == {"theme": "dark", "kline_compress": True}


def test_no_tmp_file_is_left_behind(data_dir: Path) -> None:
    preferences.save({"theme": "dark"})
    secrets_store.save({"a": "1"})

    leftovers = list((data_dir / "user_data").glob("*.tmp"))
    assert leftovers == []
