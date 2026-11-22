import hashlib
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from tivelo_backtest.extension_host import installation as engine_installation

from app.services.engine_installation import EngineInstallation


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_installation, "deployment_mode", lambda: "server")
    if sys.platform == "win32":
        _fake_vault(monkeypatch)
    return EngineInstallation(tmp_path)


def _fake_vault(monkeypatch):
    from types import SimpleNamespace

    passwords = {}
    vault = SimpleNamespace(
        get_password=lambda s, a: passwords.get((s, a)),
        set_password=lambda s, a, v: passwords.__setitem__((s, a), v),
    )
    monkeypatch.setattr(engine_installation, "_credential_vault", lambda: vault)
    return passwords


def test_first_use_device_creation_is_explicit_concurrent_and_bound(installation):
    assert installation.status() == {"identity": None, "leases": []}
    assert not (installation.root / "research").exists()
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(lambda _: installation.identity(create=True), range(4)))
    assert all(identity == identities[0] for identity in identities)
    binding = hashlib.sha256(b"locked run").digest()
    public, prove = installation.device("run", binding)
    assert hashlib.sha256(public).hexdigest() == identities[0]["sha256"]
    challenge = b"tivelo-device-proof-v1\0" + os.urandom(32) + binding + os.urandom(16)
    Ed25519PublicKey.from_public_bytes(public).verify(prove(challenge), challenge)
    for invalid in (b"other", challenge[:-1], challenge.replace(binding, os.urandom(32))):
        with pytest.raises(ValueError):
            prove(invalid)
    assert EngineInstallation(installation.root).identity() == identities[0]


def test_vault_identity_never_places_private_material_in_database(tmp_path, monkeypatch):
    pytest.importorskip("keyring")
    monkeypatch.setattr(engine_installation, "deployment_mode", lambda: "desktop")
    vault = _fake_vault(monkeypatch)
    installation = EngineInstallation(tmp_path)
    identity = installation.identity(create=True)
    assert len(vault) == 1 and identity["storage"] == "system_vault"
    with sqlite3.connect(tmp_path / "research/engine/installation/installation.sqlite3") as db:
        assert db.execute("SELECT secret FROM identity").fetchone() == (None,)
    vault.clear()
    with pytest.raises(ValueError):
        installation.device("import", b"0" * 32)
    assert installation.identity(create=True) == identity


def test_generation_copy_proves_the_same_device_and_rollback_sees_latest_lease(tmp_path, monkeypatch):
    pytest.importorskip("keyring")
    monkeypatch.setattr(engine_installation, "deployment_mode", lambda: "desktop")
    monkeypatch.setattr(engine_installation, "_desktop_workspace", None)
    vault = _fake_vault(monkeypatch)
    original = EngineInstallation(tmp_path)
    device = original.identity(create=True)
    summary = dict(scope_id="0" * 64, generation=2, revocation_epoch=1)
    original.save_verified("fixture", b"one", summary, 0)
    copy = tmp_path / "retained-data"
    copy.mkdir()
    engine_installation.bind_desktop_installation(copy, tmp_path)
    current = EngineInstallation(copy)
    assert current.identity() == device and current.lease("fixture") == b"one"
    binding = hashlib.sha256(b"same-device").digest()
    public, prove = current.device("import", binding)
    challenge = b"tivelo-device-activation-v1\0" + os.urandom(32) + binding
    Ed25519PublicKey.from_public_bytes(public).verify(prove(challenge), challenge)
    assert current.save_verified("fixture", b"two", {**summary, "revocation_epoch": 2}, 1) == 2
    assert original.saved_lease("fixture") == (b"two", 2)
    with pytest.raises(ValueError, match="早于"):
        original.save_verified("fixture", b"one", summary, 2)
    assert not (copy / "research").exists()
    assert EngineInstallation(tmp_path / "another").identity() is None
    assert len(vault) == 1
    vault.clear()
    with pytest.raises(ValueError, match="缺少"):
        current.device("import", binding)


def test_credential_replacement_is_atomic_and_does_not_export_envelope(installation):
    installation.identity(create=True)
    summary = dict(scope_id="0" * 64, generation=2, revocation_epoch=2)
    assert installation.save_verified("example", b"signed-first", summary, 0) == 1
    assert installation.save_verified("example", b"signed-first", summary, 0) == 1
    with pytest.raises(ValueError):
        installation.save_verified("example", b"new", summary, 0)
    with pytest.raises(ValueError):
        installation.save_verified("example", b"old", {**summary, "generation": 1}, 1)
    assert installation.lease("example") == b"signed-first"
    assert "signed-first" not in str(installation.status())
    assert installation.save_verified("example", b"new", {**summary, "generation": 3}, 1) == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_unsafe_secret_directory_and_file_are_refused(installation):
    installation.identity(create=True)
    directory = installation.root / "research/engine/installation"
    path = directory / "installation.sqlite3"
    path.chmod(0o644)
    with pytest.raises(ValueError):
        installation.identity()
    path.chmod(0o600)
    directory.chmod(0o755)
    with pytest.raises(ValueError):
        installation.device("run", b"0" * 32)
