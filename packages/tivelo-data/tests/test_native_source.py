"""Exercise the installed data CLI with real signed, locally compiled providers."""

import json
import multiprocessing
import runpy
import subprocess
import sys
from pathlib import Path
from time import monotonic

import pytest

pytest.importorskip("tivelo_extensions")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_extensions.store import EnginePackageStore

from tivelo_data.native_source import NativeDataInput, NativeDataProvider
from tivelo_data.providers import SyncContext

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/native-provider/build.py"


@pytest.fixture(scope="module")
def example(tmp_path_factory):
    root = tmp_path_factory.mktemp("signed-data-example")
    sample = runpy.run_path(str(EXAMPLE))
    key = Ed25519PrivateKey.generate()
    (root / "trust.json").write_text(json.dumps(sample["trust_document"](key)))
    capture = dict(
        kind="engine_data_provider", provider_id=sample["CAPABILITY"], request=sample["REQUEST"]
    )
    (root / "capture.json").write_text(json.dumps(capture))
    binaries = {}
    for version in ("1.0.0", "2.0.0"):
        build = root / version
        build.mkdir()
        binary = sample["compile_module"](build, version)
        binaries[version] = binary
        (root / (version + ".zip")).write_bytes(
            sample["bundle"](binary, key, version, "10" if version == "1.0.0" else "12")
        )
    return root, sample, key, binaries


def cli(module, *args, ok=True):
    result = subprocess.run(
        [sys.executable, "-I", "-m", module, *map(str, args)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if ok:
        assert result.returncode == 0, result.stderr
        return [json.loads(line) for line in result.stdout.splitlines()]
    assert result.returncode == 1, result.stdout
    return json.loads(result.stderr.splitlines()[-1])


def stage(store, data):
    info = store.inspect(data)
    assert not info["errors"]
    return store.stage(data, info["digest"], info["revision"])["digest"]


def test_signed_cli_pins_upgrades_historical_revisions_rollback_and_disable(
    tmp_path, monkeypatch, example
):
    root, sample, key, binaries = example
    monkeypatch.setenv("TIVELO_RELEASE_TRUST_FILE", str(root / "trust.json"))
    state = tmp_path / "installation"

    def admin(*args, ok=True):
        output = cli("tivelo_extensions.cli", "--state", state, *args, ok=ok)
        return output[0] if ok else output

    def acquire(command, *options, ok=True):
        output = cli(
            "tivelo_data.cli",
            "--root",
            tmp_path / "data",
            command,
            "--extension-state",
            state,
            "--capture",
            root / "capture.json",
            *options,
            ok=ok,
        )
        return output[0] if ok else output

    def query(snapshot, as_of):
        path = tmp_path / "query.json"
        request = sample["REQUEST"]
        path.write_text(
            json.dumps(
                {k: request[k] for k in ("instrument_ids", "timeframe", "start", "end")}
                | {"as_of": as_of}
            )
        )
        results = cli(
            "tivelo_data.cli",
            "--root",
            tmp_path / "data",
            "query",
            "--request",
            path,
            "--snapshot",
            snapshot,
        )
        return [r["close"] for r in results]

    digests = []
    for version in ("1.0.0", "2.0.0"):
        path = root / (version + ".zip")
        inspected = admin("inspect", "--archive", path)
        assert not inspected["errors"]
        saved = admin(
            "stage",
            "--archive",
            path,
            "--digest",
            inspected["digest"],
            "--revision",
            inspected["revision"],
        )
        digests.append(saved["digest"])
    old, new = digests
    admin("switch", "activate", "--digest", old, "--generation", 0)
    first = acquire("plan-provider")
    assert first["extension"]["archive_sha256"] == old
    catalog = admin("describe-source", "--provider", sample["CAPABILITY"])
    assert catalog["extension"] == first["extension"] and catalog["provider"] == first["provider"]
    assert all(row["provider_ids"] == [sample["CAPABILITY"]] for row in admin("list"))
    plan = tmp_path / "locked.json"
    plan.write_text(json.dumps(first))
    admin("switch", "activate", "--digest", new, "--generation", 1)
    assert acquire("plan-provider")["extension"]["archive_sha256"] == new
    prior = acquire("import-provider", "--locked-plan", plan)["snapshot_id"]
    current = acquire("import-provider")["snapshot_id"]
    assert current != prior
    assert query(prior, "2025-01-07T00:00:00Z") == ["10", "10"]
    assert query(current, "2025-01-04T00:00:00Z") == ["10", "10"]
    assert query(current, "2025-01-07T00:00:00Z") == ["12", "12"]
    paths = []
    for version, options in (
        ("2.1.0", {"omit_catalog": True}),
        ("2.2.0", {}),
        ("2.3.0", {"catalog_version": "1.0.0"}),
    ):
        broken = sample["bundle"](binaries["2.0.0"], key, version, "12", **options)
        digest = stage(EnginePackageStore(state), broken)
        error = admin("switch", "activate", "--digest", digest, "--generation", 2, ok=False)
        assert error["code"] == "EXTENSION_INCOMPATIBLE"
        paths.append(digest)
    versions = admin("list")
    assert len(paths) == 3 and next(v for v in versions if v["state"] == "active")["digest"] == new
    assert admin("switch", "rollback", "--digest", new, "--generation", 2)["active_digest"] == old
    assert acquire("plan-provider") == first
    admin("switch", "disable", "--digest", old, "--generation", 3)
    assert (
        acquire("import-provider", "--locked-plan", plan, ok=False)["code"]
        == "EXTENSION_INCOMPATIBLE"
    )
    assert query(current, "2025-01-07T00:00:00Z") == ["12", "12"]


def test_profile_digest_separate_credential_and_revoked_trust(tmp_path, monkeypatch, example):
    root, sample, _, _ = example
    trust = tmp_path / "trust.json"
    trust.write_bytes((root / "trust.json").read_bytes())
    monkeypatch.setenv("TIVELO_RELEASE_TRUST_FILE", str(trust))
    store = EnginePackageStore(tmp_path)
    digest = stage(store, (root / "1.0.0.zip").read_bytes())
    store.switch(digest, "activate", 0)

    def run(cmd, *args, ok=True):
        value = cli(
            "tivelo_data.cli",
            "--root",
            tmp_path / "data",
            cmd,
            "--extension-state",
            tmp_path,
            "--capture",
            root / "capture.json",
            *args,
            ok=ok,
        )
        return value[0] if ok else value

    saved = tmp_path / "locked.json"
    saved.write_text(json.dumps(run("plan-provider")))
    profile = (
        tmp_path / "research/engine/extensions/profiles" / sample["CAPABILITY"] / "default.json"
    )
    profile.parent.mkdir(parents=True)
    profile.write_text('{"require_token": true}')
    assert run("sync-provider", "--locked-plan", saved, ok=False)["code"] == "DATA_CONFLICT"
    plan = run("plan-provider")
    binding = tmp_path / "credentials.json"
    monkeypatch.setenv("TIVELO_DATA_CREDENTIALS_FILE", str(binding))
    assert run("sync-provider", ok=False)["code"] == "AUTHENTICATION_FAILED"
    binding.write_text(
        json.dumps({sample["CAPABILITY"]: {"default": {"token": "synthetic-secret"}}})
    )
    assert "synthetic-secret" not in json.dumps(run("import-provider")) + json.dumps(plan)
    trust.write_text('{"keys":[]}')
    assert run("plan-provider", ok=False)["code"] == "EXTENSION_INCOMPATIBLE"


def test_timeout_cancel_and_commercial_missing_lease(tmp_path, monkeypatch, example):
    root, sample, key, _ = example
    monkeypatch.setenv("TIVELO_RELEASE_TRUST_FILE", str(root / "trust.json"))
    source = (
        sample["SOURCE"]
        .read_text()
        .replace(
            "        if self.options.get(",
            "        import time\n        time.sleep(2)\n        if self.options.get(",
        )
    )
    source += '\n\ndef verify_installation_lease(**kwargs):\n    raise ValueError("This fixture issues no licenses")\n'
    build = tmp_path / "compile"
    build.mkdir()
    binary = sample["compile_module"](build, "3.0.0", source=source)
    store = EnginePackageStore(tmp_path)
    digest = stage(store, sample["bundle"](binary, key, "3.0.0", "12"))
    store.switch(digest, "activate", 0)
    spec = NativeDataInput.model_validate_json((root / "capture.json").read_bytes())

    def run(cancel, timeout, code):
        provider = NativeDataProvider(tmp_path, spec, timeout_seconds=timeout)
        with pytest.raises(TiveloError) as failed:
            provider.check_ready(SyncContext(lambda ref: {}, cancel, lambda n, stage: None))
        assert failed.value.code == code
        assert all(p.name != "tivelo-data-provider" for p in multiprocessing.active_children())

    run(lambda: False, 0.25, ErrorCode.SOURCE_UNAVAILABLE)
    start = monotonic()
    run(lambda: monotonic() - start > 0.3, 5, ErrorCode.CANCELLED)
    commercial = tmp_path / "commercial"
    commercial.mkdir()
    licensed = EnginePackageStore(commercial)
    digest = stage(
        licensed, sample["bundle"](binary, key, "3.0.0", "12", license_kind="commercial")
    )
    licensed.switch(digest, "activate", 0)
    provider = NativeDataProvider(commercial, spec)
    with pytest.raises(TiveloError) as failed:
        provider.check_ready(SyncContext(lambda ref: {}, lambda: False, lambda n, stage: None))
    assert failed.value.code == ErrorCode.LICENSE_REQUIRED


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Portable positive lease fixture uses the server's private-file device identity",
)
def test_commercial_source_validates_signed_lease_and_attempt_device_proof(
    tmp_path, monkeypatch, example
):
    import hashlib
    from datetime import UTC, datetime, timedelta

    import rfc8785
    from tivelo_extensions.installation import EngineInstallation

    root, sample, release_key, _ = example
    monkeypatch.setenv("TIVELO_RELEASE_TRUST_FILE", str(root / "trust.json"))
    issuer = Ed25519PrivateKey.generate()
    public = issuer.public_key().public_bytes_raw()
    # A synthetic supplier uses a separate signing key and the documented device proof.
    # Neither product licensing code nor a persistent private key is part of this fixture.
    source = (
        sample["SOURCE"].read_text()
        + r"""
import hashlib, os
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from tivelo_contracts.signatures import verify_envelope

def verify_installation_lease(*, lease, device_public_key, prove_device, deployment):
    claims = verify_envelope(lease, purpose="license", public_keys={"source-test": ISSUER}).claims
    if claims["installation"] != hashlib.sha256(device_public_key).hexdigest() or claims["feature"] != "example.data.read":
        raise ValueError("Wrong installation or capability")
    challenge = b"tivelo-device-activation-v1\0"+os.urandom(32)+hashlib.sha256(lease).digest()
    Ed25519PublicKey.from_public_bytes(device_public_key).verify(prove_device(challenge), challenge)
    return claims["summary"]

def create_provider(*, plan, options, credential_ref, attempt_id, deployment, lease, device_public_key, prove_device):
    challenge = b"tivelo-device-proof-v1\0"+os.urandom(32)+bytes.fromhex(plan.plan_id)+attempt_id.bytes
    Ed25519PublicKey.from_public_bytes(device_public_key).verify(prove_device(challenge), challenge)
    return ArchiveProvider(plan,options,credential_ref)
"""
    )
    source += "\nISSUER = " + repr(public) + "\n"
    build = tmp_path / "compile"
    build.mkdir()
    binary = sample["compile_module"](build, "4.0.0", source=source)
    store = EnginePackageStore(tmp_path)
    digest = stage(
        store, sample["bundle"](binary, release_key, "4.0.0", "12", license_kind="commercial")
    )
    store.switch(digest, "activate", 0)
    device = EngineInstallation(tmp_path).identity(create=True)
    expiry = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    claims = dict(
        installation=device["sha256"],
        feature="example.data.read",
        summary=dict(
            scope_id=hashlib.sha256(b"synthetic subscription").hexdigest(),
            license_policy="subscription",
            lease_expires_at=expiry,
            updates_until=expiry,
            usage_until=expiry,
            generation=1,
            revocation_epoch=0,
        ),
    )

    def lease(data):
        message = rfc8785.dumps(
            dict(
                protected=dict(version=1, type="license", kid="source-test", alg="Ed25519"),
                claims=data,
            )
        )
        return json.dumps(
            dict(
                signed=sample["encoded"](message),
                signature=sample["encoded"](issuer.sign(b"tivelo-license-v1\0" + message)),
            )
        ).encode()

    with pytest.raises(ValueError):
        store.import_lease(digest, lease(dict(claims, installation="0" * 64)), 0)
    assert store.import_lease(digest, lease(claims), 0)["revision"] == 1
    result = cli(
        "tivelo_data.cli",
        "--root",
        tmp_path / "data",
        "import-provider",
        "--extension-state",
        tmp_path,
        "--capture",
        root / "capture.json",
    )
    assert len(result[0]["snapshot_id"]) == 64
