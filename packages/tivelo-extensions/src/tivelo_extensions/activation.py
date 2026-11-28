"""Run an authenticated native release's read-only self-test outside the web process."""

import hashlib
import json
import multiprocessing
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError
from tivelo_contracts.base import Contract, Digest, UTCDateTime
from tivelo_contracts.engine_releases import EngineRelease
from tivelo_contracts.extension_archive import read_engine_archive


def materialize_native(data: bytes, release: EngineRelease, directory: Path) -> Path:
    """The caller owns this fresh directory and has verified the whole archive."""
    files = read_engine_archive(data)
    for item in release.files:
        target = directory / item.path
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as output:
            output.write(files[item.path])
    return directory / release.entrypoint


class LeaseSummary(Contract):
    scope_id: Digest
    license_policy: Literal["subscription", "perpetual_version"]
    lease_expires_at: UTCDateTime
    updates_until: UTCDateTime
    usage_until: UTCDateTime | None
    generation: int = Field(strict=True, ge=0)
    revocation_epoch: int = Field(strict=True, ge=0)


def isolate_native_process():
    # A reviewed native publisher runs with the worker account's OS permissions.
    # Self-test needs no inherited service credentials or user input.
    keep = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TMP",
        "TEMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
    }
    for key in tuple(os.environ):
        if key.upper() not in keep:
            os.environ.pop(key, None)
    with open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)


def _probe_child(entry, installation_dir, envelope, descriptors, commercial, pipe):
    try:
        if envelope is not None:
            from .installation import EngineInstallation, deployment_mode

            device = EngineInstallation(installation_dir)
            public, prove = device.device("import", hashlib.sha256(envelope).digest())
        isolate_native_process()
        from .native_entry import load_native_entry

        module = load_native_entry(Path(entry))
        if envelope is None:
            result = module.tivelo_probe()
            if not callable(getattr(module, "create_provider", None)):
                raise ValueError
            if descriptors is not None:
                from tivelo_contracts.providers import ProviderDescriptor

                for key, expected in descriptors.items():
                    if ProviderDescriptor.model_validate(module.describe_provider(key)) != expected:
                        raise ValueError
                if commercial and not callable(getattr(module, "verify_installation_lease", None)):
                    raise ValueError
        else:
            os.environ["TIVELO_LICENSE_STATE_DIR"] = str(
                device.root / "research/engine/installation/observations"
            )
            result = module.verify_installation_lease(
                lease=envelope,
                device_public_key=public,
                prove_device=prove,
                deployment=deployment_mode(),
            )
        encoded = json.dumps(result, ensure_ascii=True, allow_nan=False).encode()
        if len(encoded) > 16_384:
            raise ValueError
        pipe.send_bytes(encoded)
    except BaseException:  # noqa: BLE001 - keep third-party messages inside the probe process
        pipe.send_bytes(b"null")
    finally:
        pipe.close()


def _native_check(
    data, release, *, timeout, installation_dir=None, envelope=None, descriptors=None
):
    with tempfile.TemporaryDirectory(prefix="tivelo-native-probe-") as temporary:
        entry = materialize_native(data, release, Path(temporary))
        context = multiprocessing.get_context("spawn")
        reader, writer = context.Pipe(duplex=False)
        child = context.Process(
            target=_probe_child,
            args=(
                str(entry),
                installation_dir,
                envelope,
                descriptors,
                release.license_kind == "commercial",
                writer,
            ),
            name="tivelo-native-probe",
            daemon=True,
        )
        try:
            child.start()
            writer.close()
            if not reader.poll(timeout):
                raise ValueError("原生扩展自检超时")
            try:
                result = json.loads(reader.recv_bytes(16_384))
            except (ValueError, EOFError, OSError, UnicodeError, RecursionError):
                raise ValueError("原生扩展自检未返回有效结果") from None
            child.join(min(timeout, 3))
            if child.exitcode != 0 or result is None:
                raise ValueError(
                    "本机使用凭证校验失败" if envelope is not None else "原生扩展自检失败"
                )
            return result
        finally:
            reader.close()
            writer.close()
            if child.pid is not None:
                if child.is_alive():
                    child.kill()
                child.join()
                child.close()


def probe_release(data: bytes, release: EngineRelease, timeout: float = 15):
    expected = {
        "format": "tivelo-native-probe-v1",
        "id": release.id,
        "version": release.version,
        "kind": release.kind,
        "contracts_api": release.requires.contracts_api,
        "extension_api": release.requires.extension_api,
        "runtime_generation": release.requires.runtime_generation,
        "capabilities": [cap.id for cap in release.provides],
    }
    from .data_descriptors import data_descriptors

    descriptors = data_descriptors(data, release) if release.kind == "data-provider" else None
    actual = _native_check(data, release, timeout=timeout, descriptors=descriptors)
    # A boolean cannot substitute for a protocol version.
    if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError("原生扩展自检与签名清单不符")


def verify_local_lease(data, release, root, envelope):
    result = _native_check(data, release, timeout=30, installation_dir=root, envelope=envelope)
    try:
        return LeaseSummary.model_validate(result).model_dump(mode="json")
    except ValidationError:
        raise ValueError("专业凭证验证器返回的状态格式无效") from None
