"""One credential and native TdxAiData process for an entire stock observation."""

import os
import shutil
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import Field
from tivelo_contracts.base import Contract
from tivelo_contracts.errors import ErrorCode, TiveloError

from ._source_process import MAX_FRAME, SourceProcess, _receive, _send, watch_owner
from .legacy.pacing import RequestPacer
from .legacy.tdx_runtime import (
    LIB_ENV,
    TdxQuantAuthError,
    TdxQuantRateLimitError,
    _raise_response_error,
    historical_arguments,
    materialize_lib,
)
from .providers import SyncContext


class TdxQuantDailyConfig(Contract):
    credential_ref: str = Field(
        default="TDXQUANT_API_KEY", pattern=r"^[A-Za-z_][A-Za-z_0-9]{0,127}$"
    )
    rpm: int = Field(default=60, ge=1, le=60000, strict=True)
    timeout_seconds: int = Field(default=45, ge=1, le=300, strict=True)


def _tdx_worker(pipe, root):
    try:
        # Vendor startup and errors may write to stdout; the pipe carries only typed failures.
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        watch_owner(lambda: shutil.rmtree(root, ignore_errors=True))
        os.chdir(root)
        for name in tuple(os.environ):
            if name.upper() not in {
                "PATH",
                "TMP",
                "TEMP",
                "TMPDIR",
                "SYSTEMROOT",
                "WINDIR",
                "LANG",
                "LC_ALL",
                "HOME",
                "USERPROFILE",
                "LOCALAPPDATA",
                "APPDATA",
            }:
                os.environ.pop(name, None)
        _send(pipe, {"type": "credentials"})
        response = _receive(pipe)
        key = response["values"]["token"]
        # TdxAiData binds one library to its process. Only this child sees the copied INIs.
        os.environ[LIB_ENV] = str(materialize_lib(key, Path(root)))
        from tdxaidata.tdxaidata import _NativeTdxAiData

        _send(pipe, {"type": "ready"})
        while True:
            command = _receive(pipe)
            if command.get("type") == "close":
                break
            kind = command["type"]
            native = _NativeTdxAiData.get_instance()
            if kind == "history":
                args = historical_arguments(
                    command["symbol"], date.fromisoformat(command["end"]), command["count"], "1d"
                )
                raw = native.get_his_dats(*args)
            elif kind == "events":
                raw = native.get_cw_data(
                    command["symbol"], command["start"] + " 00:00:00", command["end"] + " 00:00:00"
                )
            else:
                raise ValueError("Invalid native request")
            if not isinstance(raw, str) or not raw:
                raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "Empty native response")
            if len(raw) > MAX_FRAME // 2:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Native response exceeds its page limit"
                )
            # Check errors before the SDK cleaner can print or collapse them into empty rows.
            _raise_response_error(raw, key)
            _send(pipe, {"type": kind, "data": raw})
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        code = ErrorCode.SOURCE_UNAVAILABLE
        if isinstance(exc, TdxQuantAuthError):
            code = ErrorCode.AUTHENTICATION_FAILED
        elif isinstance(exc, TdxQuantRateLimitError):
            code = ErrorCode.RATE_LIMITED
        elif isinstance(exc, TiveloError):
            code = exc.code
        try:
            _send(pipe, {"type": "error", "code": code.value})
        except OSError:
            pass
    finally:
        pipe.close()


class TdxQuantConnection:
    def __init__(self, config):
        try:
            sdk = version("tdxaidata")
        except PackageNotFoundError:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Install tivelo-data[cn-tdxquant]"
            ) from None
        if sdk != "1.2.2":
            raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Unsupported TdxAiData SDK version")
        self.sdk = sdk
        self.config = config
        self.pacer = RequestPacer()
        self.session = SourceProcess(config.timeout_seconds, config.credential_ref)
        self.directory = None

    def open(self, token, context):
        self.directory = TemporaryDirectory(prefix="tivelo-tdx-")
        scoped = SyncContext(lambda _: {"token": token}, context.cancelled, context.progress)
        self.session.start(_tdx_worker, (self.directory.name,), scoped)

    def request(self, kind, context, **params):
        self.pacer.wait("tdxquant", self.config.rpm, context.check_cancelled)
        context.check_cancelled()
        try:
            _send(self.session.pipe, {"type": kind, **params})
        except (ValueError, OSError):
            raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "TdxAiData worker stopped") from None
        return self.session.response(context, kind)["data"]

    def close(self):
        try:
            self.session.close()
        finally:
            if self.directory is not None:
                self.directory.cleanup()
                self.directory = None
