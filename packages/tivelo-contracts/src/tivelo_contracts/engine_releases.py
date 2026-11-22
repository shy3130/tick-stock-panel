"""Static verification for a signed Tivelo native extension, before any code is loaded."""

import hashlib
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import parse_tag, sys_tags
from packaging.version import InvalidVersion, Version
from pydantic import Field, ValidationError, model_validator

from tivelo_contracts import API_MAJOR
from tivelo_contracts.base import Contract, Digest, RelativePath, ReleaseId, parse_json
from tivelo_contracts.signatures import MAX_ENVELOPE, decode_base64url, verify_envelope

from .extension_archive import read_engine_archive
from .native_targets import matches_native_targets

MARKER = "tivelo-engine.json"
RUNTIME_GENERATION = "tivelo-alpha1"
ShortName = Annotated[
    str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
]


class PublisherKey(Contract):
    issuer: ReleaseId
    kid: ShortName
    public_key: str = Field(strict=True, max_length=44)
    extension_ids: tuple[ReleaseId, ...] = Field(min_length=1, max_length=128)
    capability_ids: tuple[ReleaseId, ...] = Field(min_length=1, max_length=128)


class ReleaseTrust(Contract):
    format: Literal["tivelo-release-trust-v1"] = "tivelo-release-trust-v1"
    keys: tuple[PublisherKey, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        if len({key.kid for key in self.keys}) != len(self.keys):
            raise ValueError("Duplicate release key id")
        for key in self.keys:
            decode_base64url(key.public_key, size=32)
        return self


def release_trust() -> ReleaseTrust:
    location = os.environ.get("TIVELO_RELEASE_TRUST_FILE")
    if not location:
        return ReleaseTrust()
    try:
        with Path(location).open("rb") as stream:
            payload = stream.read(MAX_ENVELOPE + 1)
        if len(payload) > MAX_ENVELOPE:
            raise ValueError
        return ReleaseTrust.model_validate(parse_json(payload.decode("utf-8")))
    except (ValueError, OSError):
        raise ValueError("引擎发行公钥配置不可用") from None


class ReleaseRequirements(Contract):
    contracts_api: int = Field(strict=True, ge=1)
    extension_api: int = Field(strict=True, ge=1)
    engine_version: str = Field(strict=True, min_length=1, max_length=128)
    runtime_generation: ShortName
    target_tags: tuple[ShortName, ...] = Field(min_length=1, max_length=32)


class ReleaseCapability(Contract):
    kind: Literal["price_view", "dataset_provider"]
    id: ReleaseId
    api_version: int = Field(strict=True, ge=1)
    feature_id: ReleaseId | None = None


class ReleaseFile(Contract):
    path: RelativePath
    sha256: Digest
    size_bytes: int = Field(strict=True, ge=1, le=20 * 1024**2)


class EngineRelease(Contract):
    format: Literal["tivelo-engine-extension"]
    format_version: int = Field(strict=True, ge=1, le=1)
    id: ReleaseId
    version: ShortName
    issuer: ReleaseId
    kind: Literal["backtest-extension", "data-provider"]
    license_kind: Literal["commercial", "community"]
    requires: ReleaseRequirements
    provides: tuple[ReleaseCapability, ...] = Field(min_length=1, max_length=64)
    entrypoint: RelativePath
    files: tuple[ReleaseFile, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if str(Version(self.version)) != self.version:
            raise ValueError("Release version must have its canonical spelling")
        SpecifierSet(self.requires.engine_version)
        paths = [item.path for item in self.files]
        if paths != sorted(set(paths)) or self.entrypoint not in paths:
            raise ValueError("Entrypoint or release inventory is inconsistent")
        directories = {}
        for path in paths:
            if (
                not path.startswith("payload/")
                or not path.isascii()
                or any(not (char.isalnum() or char in "._/-") for char in path)
            ):
                raise ValueError("Invalid release payload path")
            for parent in PurePosixPath(path).parents:
                spelling = parent.as_posix()
                if directories.setdefault(spelling.casefold(), spelling) != spelling:
                    raise ValueError("Inconsistent release directory spelling")
        ids = [item.id for item in self.provides]
        if self.issuer != "tivelo" and any(
            name == "tivelo" or name.startswith("tivelo.") for name in (self.id, *ids)
        ):
            raise ValueError("Tivelo identifiers require the official publisher")
        if any(name in {"tivelo.raw", "tivelo.qfq", "tivelo.hfq"} for name in ids):
            raise ValueError("Built-in price views cannot be replaced")
        claims_dynamic = any(
            c.id == "tivelo.pro.dynamic" or c.feature_id == "backtest.price_view.dynamic"
            for c in self.provides
        )
        if claims_dynamic and (
            self.id != "tivelo.pro.dynamic-adjustment"
            or self.license_kind != "commercial"
            or not all(
                c.kind == "price_view"
                and c.id == "tivelo.pro.dynamic"
                and c.feature_id == "backtest.price_view.dynamic"
                for c in self.provides
            )
        ):
            raise ValueError("Official dynamic price-view identity is inconsistent")
        expected = "price_view" if self.kind == "backtest-extension" else "dataset_provider"
        if ids != sorted(set(ids)) or any(
            item.kind != expected
            or item.api_version != self.requires.extension_api
            or (self.license_kind == "commercial" and item.feature_id is None)
            for item in self.provides
        ):
            raise ValueError("Inconsistent extension capabilities")
        return self


def verify_release(data: bytes, trust: ReleaseTrust) -> tuple[EngineRelease, str]:
    files = read_engine_archive(data)
    marker = files.pop(MARKER, None)
    if marker is None:
        raise ValueError("缺少引擎包签名清单")
    keys = {key.kid: key for key in trust.keys}
    verified = verify_envelope(
        marker,
        purpose="engine-release",
        public_keys={kid: decode_base64url(key.public_key, size=32) for kid, key in keys.items()},
    )
    try:
        release = EngineRelease.model_validate(verified.claims)
    except (ValidationError, InvalidVersion, InvalidSpecifier):
        raise ValueError("签名清单的引擎包格式无效") from None
    key = keys[verified.kid]
    if (
        release.issuer != key.issuer
        or release.id not in key.extension_ids
        or not {cap.id for cap in release.provides}.issubset(key.capability_ids)
    ):
        raise ValueError("发行公钥未获准签发此扩展或能力")
    if set(files) != {item.path for item in release.files}:
        raise ValueError("引擎包文件与签名清单不一致")
    for item in release.files:
        content = files[item.path]
        if len(content) != item.size_bytes or hashlib.sha256(content).hexdigest() != item.sha256:
            raise ValueError("引擎包文件大小或摘要校验失败")
        extension = PurePosixPath(item.path).suffix
        if extension != ".json" and not matches_native_targets(
            content, release.requires.target_tags
        ):
            raise ValueError("原生文件格式或架构与签名目标平台不一致")
    return release, verified.kid


def compatibility(release: EngineRelease) -> tuple[str, list[str]]:
    """Compare declared runtime compatibility; the native loader runs in a later stage."""
    required = release.requires
    package, api = (
        ("tivelo-backtest", 1) if release.kind == "backtest-extension" else ("tivelo-data", 2)
    )
    try:
        installed = version(package)
    except PackageNotFoundError:
        installed = ""
    errors = []
    if not installed or not SpecifierSet(required.engine_version).contains(
        installed, prereleases=True
    ):
        errors.append("已安装的引擎版本不满足声明范围")
    if (
        required.contracts_api != API_MAJOR
        or required.extension_api != api
        or required.runtime_generation != RUNTIME_GENERATION
    ):
        errors.append("引擎接口或运行环境代际不兼容")
    try:
        targets = {tag for text in required.target_tags for tag in parse_tag(text)}
    except ValueError:
        targets = set()
    if (
        not targets
        or any(tag.abi == "none" or tag.platform == "any" for tag in targets)
        or targets.isdisjoint(sys_tags())
    ):
        errors.append("引擎包的 Python ABI、平台或架构不兼容")
    suffix = ".pyd" if sys.platform == "win32" else ".so"
    if PurePosixPath(release.entrypoint).suffix != suffix:
        errors.append("原生入口文件类型与当前平台不一致")
    return installed, errors
