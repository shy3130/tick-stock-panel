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
