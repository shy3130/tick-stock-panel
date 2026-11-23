"""A data engine source bound to a signed local release and a named operator profile."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from tivelo_contracts.base import Contract, ReleaseId, fingerprint, parse_json
from tivelo_contracts.errors import ErrorCode, TiveloError

from .providers import ResolvedSource, SyncRequest, plan_source


class NativeDataInput(Contract):
    kind: Literal["engine_data_provider"]
    provider_id: ReleaseId
    profile: ReleaseId = "default"
    request: SyncRequest

    @property
    def credential_ref(self):
        return f"{self.provider_id}/{self.profile}"


class _Configuration(Contract):
    provider_id: ReleaseId
    profile: ReleaseId
    options_json: str


def _read_mapping(path):
    with path.open("rb") as stream:
        data = stream.read(1_048_577)
    value = parse_json(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _options(state, capture):
    path = state
    for member in (
        "research",
        "engine",
        "extensions",
        "profiles",
        capture.provider_id,
        capture.profile + ".json",
    ):
        path /= member
        if path.is_symlink():
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid data provider profile path")
    try:
        return _read_mapping(path)
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Invalid local data provider options"
        ) from None


def installation_credentials(reference):
    """Look up only the selected source/profile in the operator's separate secret file."""
    try:
        publisher, profile = reference.split("/")
        secrets = _read_mapping(Path(os.environ["TIVELO_DATA_CREDENTIALS_FILE"]))
        binding = secrets[publisher][profile]
        if (
            not isinstance(binding, dict)
            or not binding
            or not all(
                isinstance(k, str) and k and isinstance(v, str) and v for k, v in binding.items()
            )
        ):
            raise ValueError
        return binding
    except (ValueError, TypeError, KeyError, OSError):
        raise TiveloError(
            ErrorCode.AUTHENTICATION_FAILED, "Data provider credentials unavailable"
        ) from None


class NativeDataProvider:
    def __init__(self, state, capture: NativeDataInput, *, pin=None, timeout_seconds=120):
        try:
            from tivelo_extensions.store import EnginePackageStore
        except ModuleNotFoundError as exc:
            if exc.name != "tivelo_extensions":
                raise
            raise TiveloError(
                ErrorCode.EXTENSION_INCOMPATIBLE,
                "Install tivelo-data[extensions] for signed sources",
            ) from None
        if not 0 < timeout_seconds <= 86400:
            raise ValueError("invalid provider call timeout")
        self.capture = capture
        self.timeout = timeout_seconds
        self.store = EnginePackageStore(state)
        try:
            self.extension_pin = pin or self.store.resolve(
                capture.provider_id, kind="dataset_provider"
            )
            if self.extension_pin.capability_id != capture.provider_id:
                raise ValueError
            self._descriptor = self.store.dataset_descriptor(self.extension_pin)
        except (ValueError, OSError) as exc:
            raise TiveloError(
                ErrorCode.EXTENSION_INCOMPATIBLE,
                "Signed data source is not available for this plan",
            ) from exc
        self._configuration = _Configuration(
            provider_id=capture.provider_id,
            profile=capture.profile,
            options_json=json.dumps(
                _options(self.store.root, capture),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
        )
        self.configuration_digest = fingerprint(self._configuration)
        self._session = None

    def describe(self):
        return self._descriptor

    def check_ready(self, context):
        from .native_session import DataProviderSession

        self.close()
        plan = plan_source(
            self.capture.request,
            ResolvedSource(self, self.configuration_digest, self.extension_pin),
        )
        try:
            path, release = self.store.loadable_entry(self.extension_pin)
        except (OSError, ValueError) as exc:
            raise TiveloError(
                ErrorCode.EXTENSION_INCOMPATIBLE, "Locked data source failed release verification"
            ) from exc
        self._session = DataProviderSession(self.timeout, self.capture.credential_ref)
        try:
            self._session.open(
                path,
                self.store.root,
                release,
                plan,
                json.loads(self._configuration.options_json),
                context,
            )
        except BaseException:
            self.close()
            raise

    def fetch(self, request, cursor, context):
        if self._session is None or request != self.capture.request:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Data source request differs from its active plan"
            )
        return self._session.fetch(request, cursor, context)

    def close(self):
        if self._session is not None:
            self._session.close()
            self._session = None
