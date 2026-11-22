"""Connection settings and single-request error handling for the TickFlow SDK."""

from contextlib import ExitStack, contextmanager
from tempfile import TemporaryDirectory
from typing import Literal, Self
from urllib.parse import urlsplit

import httpx
import tickflow
from pydantic import Field, model_validator
from tivelo_contracts.base import Contract
from tivelo_contracts.errors import ErrorCode, TiveloError

from .legacy.pacing import RequestPacer

FREE_ENDPOINT = "https://free-api.tickflow.org"
PAID_ENDPOINT = "https://api.tickflow.org"


class TickFlowDailyConfig(Contract):
    mode: Literal["free", "paid"] = "free"
    endpoint: str | None = Field(default=None, strict=True, max_length=2048)
    credential_ref: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z_0-9]{0,127}$")
    batch: int = Field(default=100, strict=True, ge=1, le=100)
    rpm: int = Field(default=48, strict=True, ge=1, le=60000)

    @property
    def base_url(self) -> str:
        default = FREE_ENDPOINT if self.mode == "free" else PAID_ENDPOINT
        return (default if self.endpoint is None else self.endpoint).rstrip("/")

    @model_validator(mode="after")
    def validate_connection(self) -> Self:
        url = urlsplit(self.base_url)
        loopback = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not url.hostname
            or (url.scheme != "https" and not (url.scheme == "http" and loopback))
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(c.isspace() or ord(c) < 32 for c in self.base_url)
        ):
            raise ValueError("Supply a credential-free HTTPS endpoint or local HTTP endpoint")
        if (self.mode == "paid") != (self.credential_ref is not None):
            raise ValueError("Only paid connections require a credential reference")
        if (self.mode == "free" and self.base_url == PAID_ENDPOINT) or (
            self.mode == "paid" and self.base_url == FREE_ENDPOINT
        ):
            raise ValueError("TickFlow connection mode and endpoint disagree")
        # Validate the port while still processing the source declaration.
        if url.port == 0:
            raise ValueError("Invalid endpoint port")
        return self


class TickFlowConnection:
    def __init__(self, config, resolve_token):
        self.config = config
        self.resolve_token = resolve_token
        self.resources = ExitStack()
        self.pacer = RequestPacer()
        self.client = None

    def open(self, context):
        self.close()
        # An empty string prevents the free SDK from falling back to an ambient API key.
        token = ""
        ref = self.config.credential_ref
        if ref is not None:
            token = (
                self.resolve_token(ref)
                if self.resolve_token
                else context.resolve_credentials(ref).get("token")
            )
            if not token:
                raise TiveloError(ErrorCode.AUTHENTICATION_FAILED, "TickFlow requires a credential")
        with ExitStack() as resources:
            cache = resources.enter_context(TemporaryDirectory(prefix="tivelo-tickflow-"))
            client = tickflow.TickFlow(
                api_key=token,
                base_url=self.config.base_url,
                timeout=30,
                max_retries=0,
                cache_dir=cache,
            )
            resources.callback(client.close)
            self.resources = resources.pop_all()
            self.client = client

    @contextmanager
    def request(self, context):
        self.pacer.wait("tickflow", self.config.rpm, context.check_cancelled)
        context.check_cancelled()
        try:
            yield self.client.klines
        except tickflow.APIError as exc:
            status = exc.status_code
            if status in {401, 403}:
                raise TiveloError(
                    ErrorCode.AUTHENTICATION_FAILED, "TickFlow denied access"
                ) from None
            if status == 429:
                raise TiveloError(ErrorCode.RATE_LIMITED, "TickFlow rate limit reached") from None
            if status >= 500:
                raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "TickFlow is unavailable") from None
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "TickFlow rejected the request") from None
        except (tickflow.TickFlowError, httpx.TransportError):
            raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "TickFlow connection failed") from None
        except (TypeError, KeyError, ValueError, AttributeError):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid TickFlow response") from None

    def close(self):
        self.client = None
        self.resources.close()
