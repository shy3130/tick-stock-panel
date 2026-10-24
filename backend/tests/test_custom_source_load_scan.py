"""自定义数据源扫描必须报出实际目录，reload 返回找到/跳过的文件数。"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.data_providers.custom import loader

_OK_YAML = """\
name: ok_source
display_name: OK
auth:
  type: none
datasets:
  financial:
    url: http://127.0.0.1:9/financial
    response_path: data
    field_map:
      ts_code: symbol
"""

_BAD_YAML = """\
name: bad_source
display_name: BAD
auth:
  type: none
datasets:
  financial:
    url: http://127.0.0.1:9/financial
    response_path: data
    field_map:
      ts_code: not_symbol
"""


@pytest.fixture
def isolated_loader(monkeypatch: pytest.MonkeyPatch):
    saved_providers = dict(loader._PROVIDERS)
    saved_errors = list(loader._LOAD_ERRORS)
    saved_status = dict(loader._PLUGIN_STATUS)
    saved_summary = loader._SCAN_SUMMARY
    monkeypatch.setattr(loader, "_load_builtin_plugins", lambda: None)
    yield
    for provider in list(loader._PROVIDERS.values()):
        provider.close()
    loader._PROVIDERS.clear()
    loader._PROVIDERS.update(saved_providers)
    loader._LOAD_ERRORS[:] = saved_errors
    loader._PLUGIN_STATUS.clear()
    loader._PLUGIN_STATUS.update(saved_status)
    loader._SCAN_SUMMARY = saved_summary


def test_load_all_reports_empty_directory(tmp_path: Path, isolated_loader, caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.data_providers.custom.loader")
    loader.load_all(tmp_path)
    summary = loader.scan_summary()
    assert summary == {
        "config_dir": str(tmp_path),
        "found": 0,
        "loaded": 0,
        "skipped": 0,
    }
    assert any(str(tmp_path) in rec.message for rec in caplog.records)


def test_load_all_counts_loaded_and_skipped_files(tmp_path: Path, isolated_loader) -> None:
    (tmp_path / "ok.yaml").write_text(_OK_YAML, encoding="utf-8")
    (tmp_path / "bad.yaml").write_text(_BAD_YAML, encoding="utf-8")
    (tmp_path / "broken.yml").write_text("{{\n", encoding="utf-8")
    loader.load_all(tmp_path)
    assert loader.scan_summary() == {
        "config_dir": str(tmp_path),
        "found": 3,
        "loaded": 1,
        "skipped": 2,
    }
    assert [item["name"] for item in loader.list_sources()] == ["ok_source"]


def test_scan_failure_keeps_previous_sources_and_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_loader,
) -> None:
    ready = tmp_path / "ready"
    ready.mkdir()
    (ready / "ok.yaml").write_text(_OK_YAML, encoding="utf-8")
    loader.load_all(ready)
    previous = loader.scan_summary()

    blocked = tmp_path / "blocked"
    real_mkdir = Path.mkdir

    def mkdir(self: Path, *args, **kwargs):
        if self == blocked:
            raise PermissionError("denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(PermissionError):
        loader.load_all(blocked)
    assert loader.scan_summary() == previous
    assert [item["name"] for item in loader.list_sources()] == ["ok_source"]


def test_duplicate_name_counts_one_loaded_source(tmp_path: Path, isolated_loader) -> None:
    (tmp_path / "a.yaml").write_text(_OK_YAML, encoding="utf-8")
    later = _OK_YAML.replace("display_name: OK", "display_name: Later")
    (tmp_path / "b.yaml").write_text(later, encoding="utf-8")
    loader.load_all(tmp_path)

    sources, load_errors, summary = loader.published_scan()
    assert summary == {
        "config_dir": str(tmp_path),
        "found": 2,
        "loaded": 1,
        "skipped": 1,
    }
    assert [item["name"] for item in sources] == ["ok_source"]
    assert sources[0]["display_name"] == "Later"
    assert load_errors[0]["name"] == "ok_source"
    assert "b.yaml" in load_errors[0]["errors"][0]
    assert loader.list_sources() == sources
    assert loader.scan_summary() == summary
    from app.api.settings import list_data_sources

    body = list_data_sources()
    assert body["loaded"] == 1
    assert body["skipped"] == 1
    assert body["found"] == 2
    assert body["custom"] == sources
    assert "b.yaml" in body["errors"][0]["errors"][0]


def test_close_failure_still_publishes_new_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_loader,
) -> None:
    first = tmp_path / "first"
    first.mkdir()
    (first / "ok.yaml").write_text(_OK_YAML, encoding="utf-8")
    loader.load_all(first)
    old = next(iter(loader._PROVIDERS.values()))

    def fail_close() -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(old, "close", fail_close)
    second = tmp_path / "second"
    second.mkdir()
    other = _OK_YAML.replace("name: ok_source", "name: other_source")
    other = other.replace("display_name: OK", "display_name: Other")
    (second / "other.yaml").write_text(other, encoding="utf-8")
    loader.load_all(second)

    sources, _load_errors, summary = loader.published_scan()
    assert [item["name"] for item in sources] == ["other_source"]
    assert summary["config_dir"] == str(second)
    assert summary["loaded"] == 1
    assert summary["found"] == 1


def test_reload_returns_scan_directory_and_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_loader,
) -> None:
    monkeypatch.setattr(loader, "data_sources_dir", lambda: tmp_path)
    (tmp_path / "ok.yaml").write_text(_OK_YAML, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignore", encoding="utf-8")
    from app.api.settings import reload_data_sources

    body = reload_data_sources()
    assert body["config_dir"] == str(tmp_path)
    assert body["found"] == 1
    assert body["loaded"] == 1
    assert body["skipped"] == 0
    assert [item["name"] for item in body["custom"]] == ["ok_source"]
