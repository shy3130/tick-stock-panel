"""Upgrade with real configuration files; never use a contributor's runtime data."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import desktop, secrets_store
from app.config import settings
from app.services import preferences
from app.tickflow.repository import DataStore


@pytest.fixture
def installed(tmp_path, monkeypatch):
    exe = tmp_path / "Apps" / "TSP" / "TSP.exe"
    exe.parent.mkdir(parents=True)
    target = exe.parent / "data"
    target.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(settings, "data_dir", target)
    monkeypatch.setattr(secrets_store, "_cache", None)
    monkeypatch.setattr(secrets_store, "_cache_sig", None)
    monkeypatch.setattr(preferences, "_cache", None)
    monkeypatch.setattr(preferences, "_cache_sig", None)
    yield SimpleNamespace(exe=exe, target=target, legacy=exe.parent.parent / "TickFlowStockPanel_Data")
    desktop._release_single_instance()


def put(path: Path, value: dict) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False).encode()
    path.write_bytes(raw)
    return raw


def migrate(target: Path):
    store = DataStore.__new__(DataStore)
    store.data_dir = target
    store._migrate_legacy_data_dir()


def seed(root: Path):
    secrets = put(root / "user_data" / "secrets.json", {
        "ai_api_key": "upgrade-test-ai-key-not-real",
        "custom_source_keys": {"old-source": "upgrade-test-source-key-not-real"},
    })
    prefs = put(root / "user_data" / "preferences.json", {
        "onboarding_completed": True, "realtime_quotes_enabled": True,
        "ui_prefs": {"tf-theme": "dark", "columns": ["price", "volume"]},
    })
    put(root / "user_data" / "strategy_overrides" / "mine.json", {"params": {"window": 17}})
    put(root / "ext_data" / "example" / "config.json", {"id": "example", "mode": "snapshot"})
    return secrets, prefs


def test_finds_the_directory_used_by_historical_windows_versions(installed):
    old_key, old_prefs = seed(installed.legacy)
    # The startup log calls this before DataStore(), creating target/user_data.
    secrets_store.get_tickflow_key()
    migrate(installed.target)
    assert secrets_store.get_ai_key() == "upgrade-test-ai-key-not-real"
    assert preferences.get_realtime_quotes_enabled() is True
    assert (installed.target / "user_data" / "secrets.json").read_bytes() == old_key
    assert (installed.target / "user_data" / "preferences.json").read_bytes() == old_prefs
    assert (installed.target / "user_data" / "strategy_overrides" / "mine.json").is_file()
    assert (installed.target / "ext_data" / "example" / "config.json").is_file()
    assert (installed.legacy / "user_data" / "secrets.json").read_bytes() == old_key
    assert not (installed.target / "user_data" / "user_data").exists()


def test_precreated_subdirectories_are_merged_without_an_extra_level(installed):
    legacy = installed.target.parent / "TickFlowStockPanel_Data"
    seed(legacy)
    (installed.target / "user_data").mkdir()
    (installed.target / "ext_data").mkdir()
    migrate(installed.target)
    assert secrets_store.get_ai_key() == "upgrade-test-ai-key-not-real"
    assert not (installed.target / "user_data" / "user_data").exists()
    assert not (installed.target / "ext_data" / "ext_data").exists()


@pytest.mark.parametrize("depth", [1, 2])
def test_recovers_already_nested_configuration_and_keeps_originals(installed, depth):
    nested = installed.target / "user_data"
    for _ in range(depth):
        nested /= "user_data"
    old_key = put(nested / "secrets.json", {"ai_api_key": "nested-test-key-not-real"})
    old_prefs = put(nested / "preferences.json", {"onboarding_completed": True})
    put(nested / "strategy_overrides" / "mine.json", {"enabled": True})
    migrate(installed.target)
    assert secrets_store.get_ai_key() == "nested-test-key-not-real"
    assert preferences.load()["onboarding_completed"] is True
    assert (installed.target / "user_data" / "strategy_overrides" / "mine.json").is_file()
    assert (nested / "secrets.json").read_bytes() == old_key
    assert (nested / "preferences.json").read_bytes() == old_prefs


def test_existing_credentials_are_preserved_as_a_whole_and_preferences_keep_existing_values(installed):
    seed(installed.legacy)
    existing_key = put(installed.target / "user_data" / "secrets.json", {
        "ai_api_key": "current-test-key-not-real", "custom_source_keys": {"new-source": "current-source-test-key"},
    })
    existing_prefs = put(installed.target / "user_data" / "preferences.json", {
        "onboarding_completed": False, "realtime_quotes_enabled": False,
        "ui_prefs": {"tf-theme": "light", "columns": []}, "count": 0, "empty": "",
    })
    migrate(installed.target)
    assert secrets_store.get_ai_key() == "current-test-key-not-real"
    assert (installed.target / "user_data" / "secrets.json").read_bytes() == existing_key
    assert secrets_store.load()["custom_source_keys"] == {"new-source": "current-source-test-key"}
    assert preferences.load() == json.loads(existing_prefs)
    backups = list((installed.target / "upgrade_backups").rglob("*"))
    assert any(p.is_file() and p.read_bytes() == existing_key for p in backups)
    assert (installed.legacy / "user_data" / "secrets.json").is_file()


def test_preferences_fill_missing_nested_fields_without_changing_existing_choices(installed):
    seed(installed.legacy)
    before = put(installed.target / "user_data" / "preferences.json", {"ui_prefs": {"tf-theme": "light"}})
    migrate(installed.target)
    assert preferences.load() == {
        "onboarding_completed": True, "realtime_quotes_enabled": True,
        "ui_prefs": {"tf-theme": "light", "columns": ["price", "volume"]},
    }
    assert any(p.read_bytes() == before for p in (installed.target / "upgrade_backups").rglob("current-*.backup"))


def test_conflicting_ordinary_files_are_never_overwritten_or_deleted(installed):
    seed(installed.legacy)
    old = put(installed.legacy / "ext_data" / "example" / "config.json", {"id": "old"})
    current = put(installed.target / "ext_data" / "example" / "config.json", {"id": "current"})
    migrate(installed.target)
    assert (installed.target / "ext_data" / "example" / "config.json").read_bytes() == current
    assert (installed.legacy / "ext_data" / "example" / "config.json").read_bytes() == old


def test_restarts_do_not_resurrect_a_key_the_user_removed_after_recovery(installed):
    seed(installed.legacy)
    migrate(installed.target)
    secrets_store.clear("ai_api_key")
    saved = (installed.target / "user_data" / "secrets.json").read_bytes()
    migrate(installed.target)
    assert (installed.target / "user_data" / "secrets.json").read_bytes() == saved
    assert "ai_api_key" not in secrets_store.load()


def test_bad_existing_json_is_left_untouched(installed):
    seed(installed.legacy)
    dest = installed.target / "user_data" / "secrets.json"
    dest.parent.mkdir()
    dest.write_bytes(b"current-invalid-json-preserve")
    migrate(installed.target)
    assert dest.read_bytes() == b"current-invalid-json-preserve"
    assert (installed.legacy / "user_data" / "secrets.json").is_file()


def test_old_configuration_unreadable_by_utf8_consumers_is_not_published(installed):
    path = installed.legacy / "user_data" / "secrets.json"
    path.parent.mkdir(parents=True)
    raw = '{"ai_api_key":"encoding-test-key-not-real"}'.encode("utf-16")
    path.write_bytes(raw)
    migrate(installed.target)
    assert not (installed.target / "user_data" / "secrets.json").exists()
    assert path.read_bytes() == raw


def test_malformed_completed_record_does_not_repeat_recovery(installed):
    seed(installed.legacy)
    migrate(installed.target)
    secret = installed.target / "user_data" / "secrets.json"
    secret.write_bytes(b'{}')
    marker = next((installed.target / "upgrade_backups").rglob("completed.json"))
    marker.write_bytes(b"broken-recovery-record")
    with pytest.raises(RuntimeError, match="升级恢复记录无效"):
        migrate(installed.target)
    assert secret.read_bytes() == b'{}'


def test_symbolic_links_cannot_redirect_recovery_outside_data_dir(installed, tmp_path):
    seed(installed.legacy)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (installed.target / "user_data").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    migrate(installed.target)
    assert not list(outside.iterdir())
    assert (installed.legacy / "user_data" / "secrets.json").is_file()


def test_same_installation_keeps_key_preferences_and_browser_profile(installed):
    old_key, old_prefs = seed(installed.target)
    browser = installed.target / "webview" / "Default" / "Local Storage" / "existing"
    browser.parent.mkdir(parents=True)
    browser.write_bytes(b"old-browser-storage")
    migrate(installed.target)
    assert (installed.target / "user_data" / "secrets.json").read_bytes() == old_key
    assert (installed.target / "user_data" / "preferences.json").read_bytes() == old_prefs
    assert browser.read_bytes() == b"old-browser-storage"


def test_desktop_recovers_before_backend_reads_keys_and_preferences(installed, monkeypatch):
    seed(installed.legacy)
    monkeypatch.delenv("TDX_PROBE_WORKER", raising=False)
    monkeypatch.setattr(desktop, "_guard_streams", lambda: None)
    monkeypatch.setattr(desktop, "_setup_logging", lambda: None)
    monkeypatch.setattr(desktop, "_precheck_mining_lock", lambda: None)
    monkeypatch.setattr(desktop, "_find_free_port", lambda _: 59998)
    monkeypatch.setattr(desktop, "_wait_for_server", lambda port, timeout, done_event: done_event.wait(1))
    seen = []

    def backend(port, ready):
        seen.append((secrets_store.get_ai_key(), preferences.load().get("onboarding_completed")))
        ready.set()

    monkeypatch.setattr(desktop, "_run_uvmicorn", backend)
    monkeypatch.setattr(desktop, "_open_window", lambda _: None)
    assert desktop.main() == 0
    assert seen == [("upgrade-test-ai-key-not-real", True)]


def test_saved_access_password_is_restored_before_env_bootstrap(installed, monkeypatch):
    from app import config
    from app.services import auth

    seed(installed.legacy)
    with monkeypatch.context() as patch:
        patch.setattr(settings, "data_dir", installed.legacy)
        auth.set_password("old-upgrade-test-password")
    original = (installed.legacy / "user_data" / "auth.json").read_bytes()
    monkeypatch.setattr(auth, "_configured_cache", None)
    monkeypatch.setattr(auth, "_sessions", {})
    monkeypatch.setattr(config, "_ENV_FILE", installed.target / "absent.env")
    monkeypatch.setattr(settings, "auth_password", "different-environment-test-password")
    monkeypatch.delenv("TDX_PROBE_WORKER", raising=False)
    monkeypatch.setattr(desktop, "_guard_streams", lambda: None)
    monkeypatch.setattr(desktop, "_setup_logging", lambda: None)
    monkeypatch.setattr(desktop, "_precheck_mining_lock", lambda: None)
    monkeypatch.setattr(desktop, "_find_free_port", lambda _: 59998)
    monkeypatch.setattr(desktop, "_wait_for_server", lambda port, timeout, done_event: done_event.wait(1))
    seen = []

    def backend(port, ready):
        seen.append(auth.bootstrap_from_env())
        ready.set()

    monkeypatch.setattr(desktop, "_run_uvmicorn", backend)
    monkeypatch.setattr(desktop, "_open_window", lambda _: None)
    assert desktop.main() == 0
    assert seen == [False]
    assert (installed.target / "user_data" / "auth.json").read_bytes() == original


def test_recovery_does_not_print_keys(installed, caplog):
    seed(installed.legacy)
    migrate(installed.target)
    assert "upgrade-test-ai-key-not-real" not in caplog.text
    assert "upgrade-test-source-key-not-real" not in caplog.text
    if os.name != "nt":
        assert (installed.target / "user_data" / "secrets.json").stat().st_mode & 0o777 == 0o600


def test_failed_write_preserves_originals_and_retries_on_next_start(installed, monkeypatch):
    from app.services import data_dir_upgrade as upgrade

    original_key, _ = seed(installed.legacy)
    copy_file = upgrade._copy_missing

    def fail_for_key(source, dest, **kwargs):
        if dest == installed.target / "user_data" / "secrets.json":
            raise OSError("simulated disk failure")
        return copy_file(source, dest, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(upgrade, "_copy_missing", fail_for_key)
        with pytest.raises(OSError, match="simulated disk failure"):
            migrate(installed.target)
    assert (installed.legacy / "user_data" / "secrets.json").read_bytes() == original_key
    assert not list((installed.target / "upgrade_backups").rglob("completed.json"))
    migrate(installed.target)
    assert secrets_store.get_ai_key() == "upgrade-test-ai-key-not-real"


def test_concurrent_configuration_edit_aborts_without_overwriting_it(installed, monkeypatch):
    from app.services import data_dir_upgrade as upgrade

    seed(installed.legacy)
    dest = installed.target / "user_data" / "preferences.json"
    put(dest, {"ui_prefs": {"tf-theme": "light"}})
    backup = upgrade._backup
    changed = json.dumps({"ui_prefs": {"tf-theme": "dark"}}).encode()

    def backup_then_edit(path, folder, label):
        backup(path, folder, label)
        if label == "previous":
            dest.write_bytes(changed)

    monkeypatch.setattr(upgrade, "_backup", backup_then_edit)
    with pytest.raises(RuntimeError, match="配置在恢复期间发生变化"):
        migrate(installed.target)
    assert dest.read_bytes() == changed
    assert (installed.legacy / "user_data" / "secrets.json").is_file()
    assert not list((installed.target / "upgrade_backups").rglob("completed.json"))


def test_corrupt_existing_backup_aborts_before_overwriting_preferences(installed):
    import hashlib

    seed(installed.legacy)
    dest = installed.target / "user_data" / "preferences.json"
    current = put(dest, {"ui_prefs": {"tf-theme": "light"}})
    token = hashlib.sha256(os.path.normcase(str(installed.legacy.resolve())).encode()).hexdigest()[:20]
    folder = installed.target / "upgrade_backups" / "data-dir-v1" / token
    folder.mkdir(parents=True)
    backup = folder / f"current-preferences.json-{hashlib.sha256(current).hexdigest()}.backup"
    backup.write_bytes(b"incomplete-old-backup")

    with pytest.raises(RuntimeError, match="现有升级备份与原文件不一致"):
        migrate(installed.target)

    assert dest.read_bytes() == current
    assert backup.read_bytes() == b"incomplete-old-backup"
    assert not list(folder.glob("completed.json"))
    assert (installed.legacy / "user_data" / "preferences.json").is_file()


def test_existing_file_created_during_publication_wins(installed, monkeypatch):
    from app.services import data_dir_upgrade as upgrade

    source = installed.legacy / "example.json"
    put(source, {"version": "old"})
    dest = installed.target / "example.json"
    link = upgrade.os.link

    def create_before_link(src, dst):
        if Path(dst) == dest:
            dest.write_bytes(b"newly-created-user-configuration")
        return link(src, dst)

    if os.name == "nt":
        rename = Path.rename

        def create_before_rename(src, dst):
            if Path(dst) == dest:
                dest.write_bytes(b"newly-created-user-configuration")
            return rename(src, dst)

        monkeypatch.setattr(Path, "rename", create_before_rename)
    else:
        monkeypatch.setattr(upgrade.os, "link", create_before_link)
    migrate(installed.target)
    assert dest.read_bytes() == b"newly-created-user-configuration"
    assert source.is_file()
    assert not list(installed.target.rglob(".upgrade-*"))


@pytest.mark.parametrize("cross_volume", [False, True])
def test_market_partition_migration_preserves_bytes_and_does_not_duplicate_directory_names(installed, monkeypatch, cross_volume):
    import errno

    from app.services import data_dir_upgrade as upgrade

    seed(installed.legacy)
    source = installed.legacy / "kline_daily" / "date=2026-11-06" / "part.parquet"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable-partition-byte-sample")
    if cross_volume:
        link = upgrade.os.link

        def no_link_between_volumes(src, dst):
            if Path(src) == source:
                raise OSError(errno.EXDEV, "cross-volume move")
            return link(src, dst)

        monkeypatch.setattr(upgrade.os, "link", no_link_between_volumes)
    migrate(installed.target)
    assert (installed.target / "kline_daily" / "date=2026-11-06" / "part.parquet").read_bytes() == b"immutable-partition-byte-sample"
    assert not source.exists()
    assert not (installed.target / "kline_daily" / "kline_daily").exists()


def test_readonly_old_partition_is_copied_without_deleting_the_old_file(installed, monkeypatch):
    import errno

    source = installed.legacy / "kline_daily" / "part.parquet"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"readonly-partition")
    unlink = Path.unlink

    def readonly(path, *args, **kwargs):
        if path == source:
            raise OSError(errno.EROFS, "read-only old volume")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", readonly)
    migrate(installed.target)
    assert source.read_bytes() == b"readonly-partition"
    assert (installed.target / "kline_daily" / "part.parquet").read_bytes() == b"readonly-partition"
