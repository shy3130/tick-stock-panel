from __future__ import annotations

import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config import settings
from app.services import ext_config_transfer as transfer
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig
from app.services.ext_pull import _apply_auth


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr("app.secrets_store.load", lambda: {"vendor_api_key": "private-test-key-123"})
    monkeypatch.setattr("app.services.ext_data.get_ext_api_key", lambda _id: "private-test-key-123")
    store = ExtConfigStore(tmp_path)
    store.upsert(ExtConfig(
        id="auction", label="集合竞价", mode="timeseries",
        fields=[ExtField("symbol"), ExtField("ts"), ExtField("price", "float")],
        pull=PullConfig(
            url="https://example.test/day?stride=5", auth={"type": "header", "header": "X-API-Key", "key_ref": "vendor_api_key"},
            enabled=True, time_field="ts", since_param="since", date_param="date",
            schedule_time="09:26", schedule_period="weekly", schedule_day=3,
            page_param="page", page_size_param="size", page_size=100, timeout_seconds=120,
            last_run="2026-11-04", last_message="private-test-key-123",
        ),
    ))
    return tmp_path


def entries(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {name: z.read(name) for name in z.namelist()}


def package(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, value in files.items():
            z.writestr(name, value)
    return out.getvalue()


def test_export_is_one_compatible_config_without_secrets_data_or_runtime_state(source):
    data, filename = transfer.export_config(source, "auction")
    files = entries(data)
    assert filename.endswith(".zip")
    assert set(files) == {"manifest.json", "ext_data/auction/config.json"}
    config = json.loads(files["ext_data/auction/config.json"])
    assert b"private-test-key-123" not in b"".join(files.values())
    assert config["pull"]["auth"] == {"type": "header", "header": "X-API-Key", "param": "token"}
    assert config["pull"]["enabled"] is False
    assert "last_run" not in config["pull"] and "last_message" not in config["pull"]
    assert config["pull"]["since_param"] == "since"
    assert config["pull"]["schedule_day"] == 3
    assert config["pull"]["page_size"] == 100
    assert config["pull"]["timeout_seconds"] == 120
    assert ExtConfigStore(source).get("auction").pull.enabled is True


def test_recipient_can_import_and_use_own_key_without_source_reference(source, tmp_path, monkeypatch):
    data, _ = transfer.export_config(source, "auction")
    target = tmp_path / "recipient"
    preview = transfer.inspect_config(target, data)
    assert preview["id"] == "auction" and preview["requires_key"]
    assert not (target / "ext_data").exists()
    result = transfer.import_config(target, data)
    config = ExtConfigStore(target).get("auction")
    assert result["id"] == config.id
    assert config.pull.time_field == "ts" and config.pull.enabled is False
    monkeypatch.setattr("app.services.ext_data.get_ext_api_key", lambda _id: "recipient-key")
    headers = {}
    _apply_auth(config.id, config.pull.auth, config.pull.url, headers)
    assert headers["X-API-Key"] == "recipient-key"


def test_conflicts_never_overwrite_existing_configs_data_or_keys(source):
    data, _ = transfer.export_config(source, "auction")
    marker = source / "ext_data" / "auction" / "part.parquet"
    marker.write_bytes(b"existing rows")
    old = (marker.parent / "config.json").read_bytes()
    with pytest.raises(FileExistsError):
        transfer.import_config(source, data)
    assert marker.read_bytes() == b"existing rows"
    assert (marker.parent / "config.json").read_bytes() == old
    imported = transfer.import_config(source, data, config_id="auction_copy")
    assert imported["id"] == "auction_copy"
    assert transfer.inspect_config(source, data)["conflict"] is True


@pytest.mark.parametrize("name", ["../outside.json", "user_data/secrets.json", "extra.py"])
def test_unexpected_or_unsafe_archive_members_are_rejected(source, name):
    data, _ = transfer.export_config(source, "auction")
    files = entries(data)
    files[name] = b"{}"
    with pytest.raises(ValueError):
        transfer.import_config(source / "target", package(files))


@pytest.mark.parametrize("change", [
    lambda c: c.update(id="../outside"),
    lambda c: c.update(mode="bad"),
    lambda c: c.update(fields=[]),
    lambda c: c["pull"].update(timeout_seconds=10000),
])
def test_invalid_contracts_fail_before_creating_files(source, change):
    data, _ = transfer.export_config(source, "auction")
    files = entries(data)
    config = json.loads(files["ext_data/auction/config.json"])
    change(config)
    files["ext_data/auction/config.json"] = json.dumps(config)
    target = source / "recipient"
    with pytest.raises(ValueError):
        transfer.import_config(target, package(files))
    assert not (target / "ext_data").exists()


def test_legacy_query_and_header_credentials_are_removed_and_auth_can_be_refilled(source):
    store = ExtConfigStore(source)
    config = store.get("auction")
    config.pull.auth = None
    config.pull.url = "https://example.test/day?token=private-test-key-123&stride=5"
    config.pull.headers = {"Cookie": "session=private", "Accept": "application/json"}
    config.pull.body = '{"nested":{"api_key":"body-private"},"limit":20}'
    config.description = "Key: private-test-key-123"
    store.upsert(config)
    files = entries(transfer.export_config(source, "auction")[0])
    joined = b"".join(files.values())
    for secret in (b"private-test-key-123", b"body-private", b"session=private"):
        assert secret not in joined
    exported = json.loads(files["ext_data/auction/config.json"])
    assert exported["pull"]["auth"]["type"] == "query"
    assert exported["pull"]["auth"]["param"] == "token"
    assert exported["pull"]["headers"] == {"Accept": "application/json"}
    assert "stride=5" in exported["pull"]["url"]


def test_concurrent_imports_create_only_one_config(source):
    data, _ = transfer.export_config(source, "auction")
    target = source / "recipient"
    def apply(_):
        try:
            transfer.import_config(target, data)
            return True
        except FileExistsError:
            return False
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(apply, range(8)))
    assert sum(results) == 1
    assert ExtConfigStore(target).get("auction") is not None


def test_failed_atomic_publication_can_be_retried_without_leftover_reservation(source, monkeypatch):
    data, _ = transfer.export_config(source, "auction")
    target = source / "recipient"
    import app.services.fs_utils as fs

    replace = fs.os.replace
    def fail(*_args):
        raise OSError("simulated write failure")
    monkeypatch.setattr(fs.os, "replace", fail)
    with pytest.raises(OSError):
        transfer.import_config(target, data)
    assert not (target / "ext_data" / "auction").exists()
    monkeypatch.setattr(fs.os, "replace", replace)
    assert transfer.import_config(target, data)["id"] == "auction"


def test_duplicate_paths_and_future_versions_fail_closed(source):
    data, _ = transfer.export_config(source, "auction")
    files = entries(data)
    files["manifest.json"] = json.dumps({"format": "tick-config-backup", "version": 100})
    with pytest.raises(ValueError, match="格式或版本"):
        transfer.inspect_config(source, package(files))
    duplicate = io.BytesIO()
    with zipfile.ZipFile(duplicate, "w") as archive:
        for name, value in entries(data).items():
            archive.writestr(name, value)
        with pytest.warns(UserWarning):
            archive.writestr("ext_data/auction/config.json", b"{}")
    with pytest.raises(ValueError, match="重复"):
        transfer.inspect_config(source, duplicate.getvalue())


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "", "auction\n", "CON", "lpt1"])
def test_import_rename_rejects_unsafe_and_windows_reserved_ids(source, bad_id):
    data, _ = transfer.export_config(source, "auction")
    target = source / "recipient"
    with pytest.raises(ValueError):
        transfer.import_config(target, data, config_id=bad_id)
    assert not (target / "ext_data").exists()


def test_invalid_url_error_does_not_echo_embedded_credentials(source):
    store = ExtConfigStore(source)
    config = store.get("auction")
    config.pull.url = "https://user:private-test-key-123@[invalid"
    store.upsert(config)
    with pytest.raises(ValueError) as error:
        transfer.export_config(source, "auction")
    assert "private-test-key-123" not in str(error.value)
