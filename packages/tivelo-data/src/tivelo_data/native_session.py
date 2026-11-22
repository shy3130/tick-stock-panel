"""Operate a single signed data source in a cancellable, bounded native process."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import Field, ValidationError
from tivelo_contracts.base import Contract
from tivelo_contracts.data import Coverage
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.facts import RECORD_MODELS
from tivelo_contracts.providers import ProviderDescriptor
from tivelo_extensions.activation import LeaseSummary, isolate_native_process
from tivelo_extensions.installation import EngineInstallation, deployment_mode
from tivelo_extensions.native_entry import load_native_entry

from ._source_process import SourceProcess, _receive, _send, watch_owner
from .providers import FetchPage, SyncContext


class _Page(Contract):
    records: tuple[dict, ...] = Field(max_length=10_000)
    next_cursor: str | None = Field(max_length=16_384)
    coverage: Coverage | None


def _device_proofs(release, state, plan):
    device = EngineInstallation(state)
    try:
        lease = device.lease(release.id)
        public, admission = device.device("import", hashlib.sha256(lease).digest())
        run_public, run_proof = device.device("run", bytes.fromhex(plan.plan_id))
        if public != run_public:
            raise ValueError("Device identity changed during admission")
    except (ValueError, OSError):
        raise TiveloError(ErrorCode.LICENSE_REQUIRED, "Local data source lease required") from None
    return (device.root, dict(lease=lease, device_public_key=public, prove_device=admission),
            dict(lease=lease, device_public_key=public, prove_device=run_proof))


def _device_arguments(module, proofs):
    root, admission, run = proofs
    os.environ["TIVELO_LICENSE_STATE_DIR"] = str(root / "research/engine/installation/observations")
    try:
        summary = LeaseSummary.model_validate(
            module.verify_installation_lease(**admission, deployment=deployment_mode())
        )
    except TiveloError:
        raise
    except Exception:
        raise TiveloError(ErrorCode.LICENSE_REQUIRED, "Local data source lease required") from None
    expiry = min(v for v in (summary.lease_expires_at, summary.usage_until) if v is not None)
    return run, expiry


def _check_expiry(expiry):
    if expiry is not None and datetime.now(UTC) >= expiry:
        raise TiveloError(ErrorCode.LICENSE_EXPIRED, "Data source lease expired")


def _worker(pipe, path, state, release, plan, options, credential_ref):
    provider = None
    try:
        watch_owner()
        # Bind the proof while the host still has its login session bus. Vendor
        # code is loaded only after unrelated environment values have been removed.
        proofs = (
            _device_proofs(release, state, plan) if release.license_kind == "commercial" else None
        )
        isolate_native_process()
        module = load_native_entry(path)

        def credentials(reference):
            if reference != credential_ref:
                raise TiveloError(ErrorCode.AUTHENTICATION_FAILED, "Credential outside this source")
            _send(pipe, {"type": "credentials"})
            reply = _receive(pipe)
            if reply.get("type") != "credentials" or not isinstance(reply.get("values"), dict):
                raise TiveloError(
                    ErrorCode.AUTHENTICATION_FAILED, "Data source credential unavailable"
                )
            return reply["values"]

        context = SyncContext(credentials, lambda: False, lambda count, stage: None)
        args, expiry = {}, None
        if proofs is not None:
            args, expiry = _device_arguments(module, proofs)
            _check_expiry(expiry)
        provider = module.create_provider(
            plan=plan,
            options=options,
            credential_ref=credential_ref,
            attempt_id=uuid4(),
            deployment=deployment_mode(),
            **args,
        )
        if ProviderDescriptor.model_validate(provider.describe()) != plan.provider:
            raise TiveloError(
                ErrorCode.EXTENSION_INCOMPATIBLE, "Loaded data provider differs from signed catalog"
            )
        provider.check_ready(context)
        _send(pipe, {"type": "ready"})
        while True:
            command = _receive(pipe)
            if command.get("type") == "close":
                break
            if command.get("type") != "fetch":
                raise ValueError
            _check_expiry(expiry)
            page = provider.fetch(plan.request, command["cursor"], context)
            _check_expiry(expiry)
            if (
                not isinstance(page, FetchPage)
                or not isinstance(page.records, tuple)
                or len(page.records) > 10_000
            ):
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid data provider page")
            model = RECORD_MODELS[plan.request.record_type]
            if any(not isinstance(record, model) for record in page.records):
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid data provider fact type")
            value = _Page(
                records=tuple(r.model_dump(mode="json") for r in page.records),
                next_cursor=page.next_cursor,
                coverage=page.coverage,
            )
            _send(pipe, {"type": "page", "page": value.model_dump(mode="json")})
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:
        # Third-party exception text and supplier payloads never enter job logs.
        code = exc.code if isinstance(exc, TiveloError) else ErrorCode.SOURCE_UNAVAILABLE
        try:
            _send(
                pipe,
                {
                    "type": "error",
                    "code": code.value,
                    "retry_after": exc.retry_after if isinstance(exc, TiveloError) else None,
                },
            )
        except (ValueError, OSError):
            pass
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            pipe.close()


class DataProviderSession(SourceProcess):
    def open(self, path, state, release, plan, options, context):
        self.start(_worker, (path, state, release, plan, options, self.credential_ref), context)

    def fetch(self, request, cursor, context):
        try:
            _send(self.pipe, {"type": "fetch", "cursor": cursor})
        except (ValueError, OSError):
            raise TiveloError(
                ErrorCode.SOURCE_UNAVAILABLE, "Signed data provider stopped"
            ) from None
        response = self.response(context, "page")
        try:
            page = _Page.model_validate(response["page"])
            model = RECORD_MODELS[request.record_type]
            return FetchPage(
                tuple(model.model_validate(row) for row in page.records),
                page.next_cursor,
                page.coverage,
            )
        except (KeyError, ValidationError):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Signed data provider returned invalid facts"
            ) from None
