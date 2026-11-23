import base64
import hashlib
import json
import shlex
import sqlite3
import subprocess
import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace

import pytest
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.tags import sys_tags
from test_import_center import (  # noqa: F401 - shared management API fixture
    archive,
    client,
    inspect,
)
from tivelo_contracts.signatures import verify_envelope

from app.services.engine_installation import EngineInstallation
from app.services.engine_jobs import manager as jobs
from app.services.engine_native import matches_native_targets
from app.services.engine_package_store import EnginePackageStore
from app.services.engine_releases import MARKER
from app.services.package_archive import read_code_archive


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign(claims, key, *, purpose="engine-release", header_changes=None, canonical=True):
    protected = {"version": 1, "alg": "Ed25519", "type": purpose, "kid": "test-issuer-1"}
    protected.update(header_changes or {})
    value = {"protected": protected, "claims": claims}
    raw = rfc8785.dumps(value) if canonical else json.dumps(value, indent=1).encode()
    signature = key.sign(("tivelo-" + purpose + "-v1").encode() + b"\0" + raw)
    return json.dumps({"signed": b64(raw), "signature": b64(signature)}).encode()


@pytest.fixture
def release(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    # A locally installed, ordinary native module acts only as inert file content.
    binary = Path(find_spec("_cffi_backend").origin)
    public = key.public_key().public_bytes_raw()
    trust = tmp_path / "issuer.json"
    trust.write_text(
        json.dumps(
            {
                "format": "tivelo-release-trust-v1",
                "keys": [
                    {
                        "issuer": "example",
                        "kid": "test-issuer-1",
                        "public_key": b64(public),
                        "extension_ids": ["example.one", "example.two"],
                        "capability_ids": ["example.prices", "example.data"],
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("TIVELO_RELEASE_TRUST_FILE", str(trust))

    def make(
        *,
        id="example.one",
        cap="example.prices",
        file_changes=None,
        other_files=None,
        domain="engine-release",
        signing_key=None,
        requires=None,
        claim_changes=None,
        native=None,
        kid="test-issuer-1",
    ):
        source = native or binary
        payload = source.read_bytes()
        entry = "payload/" + source.name
        plan = {
            "format": "tivelo-engine-extension",
            "format_version": 1,
            "id": id,
            "issuer": "example",
            "version": "1.0.0",
            "kind": "backtest-extension",
            "license_kind": "commercial",
            "requires": {
                "contracts_api": 2,
                "extension_api": 1,
                "engine_version": ">=0.1.0a1,<0.2",
                "runtime_generation": "tivelo-alpha1",
                "target_tags": [str(next(sys_tags()))],
                **(requires or {}),
            },
            "provides": [
                {"kind": "price_view", "id": cap, "feature_id": "example.paid", "api_version": 1}
            ],
            "entrypoint": entry,
            "files": [
                {
                    "path": entry,
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ],
            **(claim_changes or {}),
        }
        return archive(
            {
                MARKER: sign(plan, signing_key or key, purpose=domain, header_changes={"kid": kid}),
                entry: file_changes if file_changes is not None else payload,
                **(other_files or {}),
            }
        )

    return make, key, trust


PRO_ID = "tivelo.pro.dynamic-adjustment"
PRO_VIEW = "tivelo.pro.dynamic"
UNINSTALL_PRO = "/api/engine-extension-packages/pro-dynamic/uninstall"


@pytest.fixture
def pro_release(release):
    make, _, trust = release
    config = json.loads(trust.read_text())
    config["keys"].append({
        **config["keys"][0],
        "issuer": "tivelo", "kid": "pro-release",
        "extension_ids": [PRO_ID], "capability_ids": [PRO_VIEW],
    })
    trust.write_text(json.dumps(config))

    def make_version(version):
        return make(id=PRO_ID, kid="pro-release", claim_changes={
            "issuer": "tivelo",
            "version": version,
            "provides": [{
                "kind": "price_view", "id": PRO_VIEW, "api_version": 1,
                "feature_id": "backtest.price_view.dynamic",
            }],
        })

    return make_version


def stage_release(store, data):
    preview = store.inspect(data)
    return store.stage(data, preview["digest"], preview["revision"])["digest"]


def test_uninstall_removes_pro_versions_but_keeps_reports_and_authorization(
    client, tmp_path, release, pro_release, monkeypatch,  # noqa: F811
):
    manager = jobs.EngineJobManager(tmp_path, autostart=False)
    client.app.state.engine_job_manager = manager
    try:
        monkeypatch.setattr("tivelo_extensions.store.probe_release", lambda *_: None)
        store = EnginePackageStore(tmp_path)
        original = pro_release("1.0.0")
        first = stage_release(store, original)
        store.switch(first, "activate", 0)
        pin = store.resolve(PRO_VIEW)
        first_entry, _ = store.loadable_entry(pin)
        second = stage_release(store, pro_release("1.1.0"))
        store.switch(second, "activate", 1)
        second_entry, _ = store.loadable_entry(store.resolve(PRO_VIEW))
        other = stage_release(store, release[0]())
        install = EngineInstallation(tmp_path)
        identity = install.identity(create=True)
        install.save_verified(PRO_ID, b"existing-device-lease", {}, 0)
        report = manager.store.path("jobs", "published-report.json")
        report.write_text('{"result":"saved"}')

        request = {"generation": 2, "digests": [first, second]}
        assert client.post(UNINSTALL_PRO, json=request, headers={"authorization": "Bearer test"}).status_code == 403
        assert client.post(UNINSTALL_PRO, json={**request, "generation": 1}).status_code == 409
        assert client.post(UNINSTALL_PRO, json={**request, "digests": [first, second, other]}).status_code == 409
        assert first_entry.exists() and second_entry.exists()

        with monkeypatch.context() as active:
            active.setattr(manager.store, "active_ids", lambda: ["queued"])
            active.setattr(manager.store, "plan", lambda _: SimpleNamespace(inputs=(
                SimpleNamespace(preparation=None, request=SimpleNamespace(run="archived-run")),
            )))
            active.setattr(jobs, "read_input_run", lambda *_: (None, SimpleNamespace(
                contents=SimpleNamespace(extensions=(pin,)),
            )))
            blocked = client.post(UNINSTALL_PRO, json=request)
            assert blocked.status_code == 409 and "研究任务" in blocked.json()["detail"]
            assert first_entry.exists() and second_entry.exists()

        deleted = client.post(UNINSTALL_PRO, json=request)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["removed"] == 2
        assert {row["digest"] for row in store.list_staged()} == {other}
        assert not first_entry.exists() and not second_entry.exists()
        assert install.identity() == identity
        assert install.saved_lease(PRO_ID) == (b"existing-device-lease", 1)
        assert report.read_text() == '{"result":"saved"}'
        states = {row["provider_id"]: row["state"] for row in store.price_choices()}
        assert states[PRO_VIEW] == "not_installed"
        assert states["tivelo.qfq"] == states["tivelo.hfq"] == "available"
        with pytest.raises(ValueError):
            store.verified_pin(pin)

        assert stage_release(store, original) == first
        assert client.post(UNINSTALL_PRO, json={"generation": 0, "digests": [first]}).status_code == 409
        assert {row["digest"] for row in store.list_staged()} == {first, other}
    finally:
        manager.shutdown()


def test_uninstall_checks_cache_paths_and_retries_partial_cleanup(
    tmp_path, pro_release, monkeypatch,
):
    cache = tmp_path / "research" / "engine" / "extensions" / "versions"
    monkeypatch.setattr("tivelo_extensions.store.probe_release", lambda *_: None)
    store = EnginePackageStore(tmp_path)
    digest = stage_release(store, pro_release("1.0.0"))
    store.switch(digest, "activate", 0)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "other").write_text("unrelated")
    cache.mkdir()
    (cache / digest).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        store.uninstall(PRO_ID, 1, [digest])
    assert (outside / "other").read_text() == "unrelated"
    (cache / digest).unlink()

    pin = store.resolve(PRO_VIEW)
    path, _ = store.loadable_entry(pin)
    with monkeypatch.context() as failing:
        def fail(*_):
            raise OSError("simulated busy native file")

        failing.setattr("tivelo_extensions.store.shutil.rmtree", fail)
        with pytest.raises(OSError):
            store.uninstall(PRO_ID, 1, [digest])
    assert store.list_staged()[0]["state"] == "active"
    assert not path.exists()
    assert store.loadable_entry(pin)[0].is_file()
    store.uninstall(PRO_ID, 1, [digest])
    assert not list(cache.iterdir())


def test_real_signature_static_preview_stage_and_restart(client, release, tmp_path, monkeypatch):  # noqa: F811
    import ctypes

    monkeypatch.setattr(
        ctypes,
        "CDLL",
        lambda *args, **kwargs: pytest.fail("static verification loaded a native module"),
    )
    make, _, trust = release
    data = make()
    view = inspect(client, "commercial.zip", data)
    assert view.status_code == 200, view.text
    assert view.json()["kind"] == "engine"
    preview = view.json()["preview"]
    assert preview["state"] == "inspected" and preview["errors"] == []
    assert not (tmp_path / "research").exists()
    query = {"digest": preview["digest"], "revision": preview["revision"]}
    endpoint = "/api/engine-extension-packages/stage"
    upload = {"file": ("commercial.zip", data)}
    assert (
        client.post(
            endpoint, files=upload, data=query, headers={"sec-fetch-site": "cross-site"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            endpoint, files=upload, data=query, headers={"authorization": "Bearer test"}
        ).status_code
        == 403
    )
    assert not (tmp_path / "research").exists()
    assert (
        client.post(endpoint, files=upload, data={**query, "digest": "0" * 64}).status_code == 409
    )
    response = client.post(endpoint, files=upload, data=query)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "staged"
    assert client.post(endpoint, files=upload, data=query).json() == response.json()
    (first,) = EnginePackageStore(tmp_path).list_staged()
    assert first["digest"] == hashlib.sha256(data).hexdigest()
    assert client.get("/api/engine-extension-packages").json() == [first]
    assert not list(tmp_path.rglob("*.so")) and not list(tmp_path.rglob("*.pyd"))
    trust.write_text('{"format":"tivelo-release-trust-v1","keys":[]}')
    assert client.post(endpoint, files=upload, data=query).status_code == 409
    assert EnginePackageStore(tmp_path).list_staged() == [first]


@pytest.mark.parametrize(
    "variation",
    [
        "unsigned",
        "changed",
        "wrong_key",
        "license",
        "untrusted_id",
        "untrusted_cap",
        "extra_file",
        "mixed_legacy",
        "no_commercial_feature",
        "custom_key_url",
    ],
)
def test_invalid_candidate_cannot_enter_store(client, release, tmp_path, variation):  # noqa: F811
    make, _, _ = release
    options = {
        "changed": {"file_changes": b"modified"},
        "wrong_key": {"signing_key": Ed25519PrivateKey.generate()},
        "license": {"domain": "license"},
        "untrusted_id": {"id": "elsewhere.ext"},
        "untrusted_cap": {"cap": "elsewhere.price"},
        "extra_file": {"other_files": {"payload/extra.json": b"{}"}},
        "mixed_legacy": {"other_files": {"plugin.yaml": b"name: fixture"}},
        "no_commercial_feature": {
            "claim_changes": {
                "provides": [{"kind": "price_view", "id": "example.prices", "api_version": 1}]
            }
        },
        "custom_key_url": {"claim_changes": {"key_url": "https://invalid.example/key"}},
    }
    data = archive({MARKER: b"{}"}) if variation == "unsigned" else make(**options[variation])
    result = inspect(client, "candidate.zip", data)
    assert result.status_code == 400
    assert not (tmp_path / "research").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"contracts_api": 3},
        {"extension_api": 3},
        {"engine_version": "<0.0.1"},
        {"runtime_generation": "other"},
        {"target_tags": ["cp399-cp399-" + next(sys_tags()).platform]},
    ],
)
def test_incompatible_signed_candidate_cannot_be_staged(tmp_path, release, changes):
    make, _, _ = release
    claims = (
        {}
        if "extension_api" not in changes
        else {
            "provides": [
                {
                    "kind": "price_view",
                    "id": "example.prices",
                    "api_version": 3,
                    "feature_id": "example.paid",
                }
            ]
        }
    )
    data = make(requires=changes, claim_changes=claims)
    store = EnginePackageStore(tmp_path)
    view = store.inspect(data)
    assert view["errors"]
    with pytest.raises(ValueError):
        store.stage(data, view["digest"], view["revision"])
    assert store.list_staged() == []


def test_exact_digest_conflict_capability_ownership_and_concurrent_preview(tmp_path, release):
    make, _, _ = release
    store = EnginePackageStore(tmp_path)
    first, second = make(), make(id="example.two", cap="example.data")
    a, b = store.inspect(first), store.inspect(second)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.stage(first, a["digest"], a["revision"]), range(2)))
    assert all(r["state"] == "staged" for r in results)
    with pytest.raises(ValueError, match="预检"):
        store.stage(second, b["digest"], b["revision"])
    changed = make(claim_changes={"license_kind": "community"})
    assert store.inspect(changed)["errors"]
    competing = make(id="example.two")
    p = store.inspect(competing)
    with pytest.raises(ValueError, match="能力"):
        store.stage(competing, p["digest"], p["revision"])
    fresh = store.inspect(second)
    assert store.stage(second, fresh["digest"], fresh["revision"])["state"] == "staged"
    assert len(store.list_staged()) == 2
    with pytest.raises(ValueError):
        read_code_archive(first, frontend=True)


def test_envelope_purpose_jcs_and_trust_constraints(release):
    _, key, _ = release
    keys = {"test-issuer-1": key.public_key().public_bytes_raw()}
    payload = {"audience": "tivelo.runtime", "name": "测试"}
    envelope = sign(payload, key, purpose="license")
    assert verify_envelope(envelope, purpose="license", public_keys=keys).claims == payload
    for invalid in (
        sign(payload, key),
        sign(payload, key, purpose="license", canonical=False),
        sign(payload, key, purpose="license", header_changes={"version": True}),
        sign(payload, key, purpose="license", header_changes={"kid": "unknown"}),
        sign(payload, key, purpose="license", header_changes={"alg": "none"}),
        envelope.replace(b'"signed":', b'"signed":"bad","signed":'),
    ):
        with pytest.raises(ValueError):
            verify_envelope(invalid, purpose="license", public_keys=keys)


def test_native_headers_check_real_machine_and_universal_slices():
    import struct

    def thin(cpu):
        return struct.pack("<8I", 0xFEEDFACF, cpu, 0, 8, 0, 0, 0, 0)

    arm, intel = 0x0100000C, 0x01000007
    fat = (
        struct.pack(">II", 0xCAFEBABE, 2)
        + struct.pack(">5I", arm, 0, 48, 32, 0)
        + struct.pack(">5I", intel, 0, 80, 32, 0)
        + thin(arm)
        + thin(intel)
    )
    universal = ("cp311-cp311-macosx_11_0_universal2",)
    assert matches_native_targets(fat, universal)
    assert not matches_native_targets(thin(arm), universal)
    assert not matches_native_targets(thin(arm), ("cp311-cp311-macosx_11_0_x86_64",))
    assert not matches_native_targets(fat[:-1], universal)
    assert not matches_native_targets(fat[:80] + thin(arm), universal)
    elf = bytearray(64)
    elf[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", elf, 16, 3, 62)
    assert matches_native_targets(bytes(elf), ("cp311-cp311-manylinux_2_28_x86_64",))
    assert not matches_native_targets(bytes(elf), ("cp311-cp311-manylinux_2_28_aarch64",))
    pe = bytearray(480)
    pe[:2] = b"MZ"
    struct.pack_into("<I", pe, 60, 128)
    pe[128:132] = b"PE\0\0"
    struct.pack_into("<H", pe, 132, 0xAA64)
    struct.pack_into("<HHH", pe, 148, 240, 0x2000, 0x20B)
    assert matches_native_targets(bytes(pe), ("cp311-cp311-win_arm64",))
    assert not matches_native_targets(bytes(pe), ("cp311-cp311-win_amd64",))


def test_release_key_rotation_invalidates_previews_and_preserves_staged_bytes(release, tmp_path):
    make, _, trust = release
    store = EnginePackageStore(tmp_path)
    payload = make()
    before = store.inspect(payload)
    config = json.loads(trust.read_text())
    rotated = {
        **config["keys"][0],
        "kid": "new-key",
        "public_key": b64(Ed25519PrivateKey.generate().public_key().public_bytes_raw()),
    }
    config["keys"].append(rotated)
    trust.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        store.stage(payload, before["digest"], before["revision"])
    updated = store.inspect(payload)
    store.stage(payload, updated["digest"], updated["revision"])
    trust.write_text(json.dumps({**config, "keys": [rotated]}))
    with pytest.raises(ValueError):
        store.inspect(payload)
    assert [row["digest"] for row in store.list_staged()] == [updated["digest"]]


def _compile_fixture(directory, version):
    """Build an inert CPython module using the interpreter's own SDK."""
    directory.mkdir()
    name = "tivelo_fixture"
    source = directory / (name + ".c")
    target = directory / (name + sysconfig.get_config_var("EXT_SUFFIX"))
    source.write_text(
        """
#include <Python.h>
static PyObject *probe(PyObject *self, PyObject *unused) {
    return Py_BuildValue("{s:s,s:s,s:s,s:s,s:i,s:i,s:s,s:[s]}",
        "format", "tivelo-native-probe-v1", "id", "example.one", "version", "VERSION",
        "kind", "backtest-extension", "contracts_api", 2, "extension_api", 1,
        "runtime_generation", "tivelo-alpha1", "capabilities", "example.prices");
}
static PyObject *factory(PyObject *self, PyObject *unused) { Py_RETURN_NONE; }
static PyMethodDef methods[] = {{"tivelo_probe", probe, METH_NOARGS, NULL},
    {"create_provider", factory, METH_NOARGS, NULL}, {NULL, NULL, 0, NULL}};
static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "tivelo_fixture", NULL, -1, methods};
PyMODINIT_FUNC PyInit_tivelo_fixture(void) { return PyModule_Create(&module); }
""".replace("VERSION", version)
    )
    includes = sysconfig.get_path("include")
    if sys.platform == "win32":
        command = [
            "cl",
            "/nologo",
            "/LD",
            "/I" + includes,
            str(source),
            "/link",
            "/LIBPATH:" + str(Path(sys.base_prefix) / "libs"),
            "/OUT:" + str(target),
        ]
    else:
        command = [
            *shlex.split(sysconfig.get_config_var("LDSHARED")),
            "-fPIC",
            "-I" + includes,
            str(source),
            "-o",
            str(target),
        ]
    subprocess.run(command, cwd=directory, check=True, capture_output=True, timeout=60)
    return target


def test_real_native_activation_update_failed_probe_previous_version_and_disable(
    client, release, tmp_path  # noqa: F811
):
    make, _, _ = release
    store = EnginePackageStore(tmp_path)

    def stage(binary, version):
        payload = make(native=binary, claim_changes={"version": version})
        seen = store.inspect(payload)
        return store.stage(payload, seen["digest"], seen["revision"])["digest"]

    def switch(digest, action, generation, cross_site=False):
        return client.post(
            f"/api/engine-extension-packages/{digest}/switch",
            json={"operation": action, "generation": generation},
            headers={"sec-fetch-site": "cross-site"} if cross_site else {},
        )

    first = stage(_compile_fixture(tmp_path / "v1", "1.0.0"), "1.0.0")
    binary = _compile_fixture(tmp_path / "v2", "1.1.0")
    second, incompatible = stage(binary, "1.1.0"), stage(binary, "1.2.0")
    database = tmp_path / "research/engine/extensions/candidates.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE active_releases")
        db.execute("DROP TABLE approved_releases")
        db.execute("PRAGMA user_version=1")
    assert all(row["state"] == "staged" for row in store.list_staged())
    assert switch(first, "activate", 0, cross_site=True).status_code == 403
    assert switch(first, "activate", 0).json() == {
        "id": "example.one",
        "active_digest": first,
        "generation": 1,
    }
    old_pin = store.resolve("example.prices")
    assert store.verified_pin(old_pin)[1].version == "1.0.0"
    assert switch(second, "activate", 0).status_code == 409
    assert switch(second, "activate", 1).json()["generation"] == 2
    assert store.resolve("example.prices").archive_sha256 == second
    assert store.verified_pin(old_pin)[1].version == "1.0.0"
    rejected = switch(incompatible, "activate", 2)
    assert rejected.status_code == 409 and "自检" in rejected.json()["detail"]
    assert store.resolve("example.prices").archive_sha256 == second
    assert switch(second, "rollback", 2).json()["active_digest"] == first
    assert {row["state"] for row in EnginePackageStore(tmp_path).list_staged()} == {
        "staged",
        "previous",
        "active",
    }
    assert switch(first, "disable", 3).json()["active_digest"] is None
    with pytest.raises(ValueError):
        store.resolve("example.prices")
    with pytest.raises(ValueError):
        store.verified_pin(old_pin)
