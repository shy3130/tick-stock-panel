"""HTTP source transport and validated daily-bar requests."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import polars as pl
from tivelo_contracts.errors import ErrorCode, TiveloError

from .._http_request import checked_request_logging, retry_after
from ..legacy.pacing import RequestPacer
from .config import CN_EX_FACTOR_PROFILE, CN_RAW_MINUTE_PROFILES, CustomSourceConfig, DatasetConfig
from .mapper import apply_transforms, datetime_payload, extract_rows, map_rows

logger = logging.getLogger(__name__)
DAILY_FIELDS = frozenset({"symbol", "date", "open", "high", "low", "close", "volume", "amount"})


class CheckedHTTPClient:
    """Fetch source pages using a caller-supplied credential resolver."""

    def __init__(
        self, config: CustomSourceConfig, resolve_token: Callable[[str], str | None]
    ) -> None:
        self.resolve_token = resolve_token
        self.config = config
        self.name = config.name
        self._client = httpx.Client(timeout=30.0)
        self._minute_pacer = RequestPacer()

    def close(self) -> None:
        self._client.close()

    def get_daily_checked(
        self,
        symbols: list[str],
        start_time: datetime,
        end_time: datetime,
        *,
        checkpoint=None,
    ) -> pl.DataFrame:
        """Fetch one bounded research page. Every failed page invalidates the observation."""
        cfg = self._dataset("daily")
        rows = self.get_checked_rows("daily", symbols, start_time, end_time, checkpoint=checkpoint)
        try:
            if not rows:
                return pl.DataFrame()
            if len(rows) > 10000:
                raise ValueError("Oversized daily page")
            fields = [name for name, target in cfg.field_map.items() if target in DAILY_FIELDS]
            if any(isinstance(row.get(name), bool) for row in rows for name in fields):
                raise ValueError("Boolean daily field")
            df = apply_transforms(map_rows(rows, cfg.field_map), cfg.transforms, strict=True)
            required = DAILY_FIELDS
            if not required.issubset(df.columns):
                raise ValueError("Missing daily columns")
            numeric = ("open", "high", "low", "close", "volume", "amount")
            date_column = pl.col("date")
            date_column = (
                date_column.str.to_date(strict=True)
                if df.schema["date"] == pl.String
                else date_column.cast(pl.Date, strict=True)
            )
            validated = df.with_columns(
                date_column,
                *(pl.col(key).cast(pl.Float64, strict=True) for key in numeric),
            )
            if validated.select(
                pl.any_horizontal(
                    *(pl.col(key).is_null() for key in required),
                    *(~pl.col(key).is_finite() | (pl.col(key) < 0) for key in numeric),
                    pl.col("high") < pl.max_horizontal("open", "close", "low"),
                    pl.col("low") > pl.min_horizontal("open", "close", "high"),
                ).any()
            ).item():
                raise ValueError("Invalid daily row")
            return validated
        except (ValueError, TypeError, OverflowError, RecursionError, pl.exceptions.PolarsError):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid daily source response") from None

    def get_checked_rows(self, dataset, symbols, start_time, end_time, *, checkpoint):
        cfg = self._dataset(dataset)
        if not symbols or (cfg.batch and len(symbols) > cfg.batch):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid source batch")
        if checkpoint:
            checkpoint()
        self._minute_pacer.wait(dataset, cfg.rpm, checkpoint)
        scope = {}
        if dataset == "minute":
            if cfg.asset_type_param:
                scope[cfg.asset_type_param] = "stock"
            if cfg.freq_param:
                scope[cfg.freq_param] = "1m"
        try:
            return self._request_rows(
                cfg,
                symbols=symbols,
                start_time=start_time,
                end_time=end_time,
                override_params=scope,
                override_body=scope,
                strict=True,
                checkpoint=checkpoint,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise TiveloError(
                    ErrorCode.AUTHENTICATION_FAILED, "HTTP source denied access"
                ) from None
            if status == 429:
                raise TiveloError(
                    ErrorCode.RATE_LIMITED,
                    "HTTP source rate limited the request",
                    retry_after=retry_after(exc.response.headers.get("Retry-After", "")),
                ) from None
            raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "HTTP source request failed") from None
        except httpx.InvalidURL:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid HTTP source URL") from None
        except httpx.RequestError:
            raise TiveloError(
                ErrorCode.SOURCE_UNAVAILABLE, "HTTP source transport failed"
            ) from None
        except (ValueError, TypeError, OverflowError, RecursionError, pl.exceptions.PolarsError):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid HTTP source response") from None

    def _dataset(self, name: str) -> DatasetConfig:
        cfg = self.config.datasets.get(name)
        if not cfg:
            raise ValueError(
                f"Custom data source '{self.name}' does not configure dataset '{name}'"
            )
        return cfg

    def _request_rows(
        self,
        cfg: DatasetConfig,
        *,
        symbols: list[str] | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        override_params: dict[str, Any] | None = None,
        override_body: dict[str, Any] | None = None,
        strict: bool = False,
        checkpoint=None,
    ) -> list[dict]:
        headers, auth_params = self._auth_parts(required=True) if strict else self._auth_parts()
        params = dict(cfg.params)
        params.update(auth_params)
        if override_params:
            params.update(override_params)
        body = dict(cfg.body)
        if override_body:
            body.update(override_body)
        if symbols:
            body[cfg.symbols_param] = symbols
            params.setdefault(cfg.symbols_param, ",".join(symbols))
        start_value = datetime_payload(start_time)
        end_value = datetime_payload(end_time)
        if start_value:
            body[cfg.start_param] = start_value
            params.setdefault(cfg.start_param, start_value)
        if end_value:
            body[cfg.end_param] = end_value
            params.setdefault(cfg.end_param, end_value)
        if strict:
            params.update(
                {
                    cfg.symbols_param: ",".join(symbols),
                    cfg.start_param: start_value,
                    cfg.end_param: end_value,
                }
            )

        method = cfg.method.upper()
        request_kwargs: dict[str, Any] = {"headers": headers, "timeout": cfg.timeout}
        if method == "GET":
            request_kwargs["params"] = params
        else:
            request_kwargs["params"] = auth_params
            request_kwargs["json"] = body
        if strict:
            with (
                checked_request_logging(),
                self._client.stream(method, cfg.url, **request_kwargs) as resp,
            ):
                resp.raise_for_status()
                content = bytearray()
                for block in resp.iter_bytes(chunk_size=65536):
                    if checkpoint:
                        checkpoint()
                    if len(content) + len(block) > 16 * 1024**2:
                        raise ValueError("Oversized source page")
                    content.extend(block)
            exact = cfg.research_profile == CN_EX_FACTOR_PROFILE or cfg.research_profile in CN_RAW_MINUTE_PROFILES
            return extract_rows(
                json.loads(content, parse_float=Decimal if exact else float), cfg.response_path, strict=True
            )
        resp = self._client.request(method, cfg.url, **request_kwargs)
        resp.raise_for_status()
        return extract_rows(resp.json(), cfg.response_path)

    def _auth_parts(self, *, required: bool = False) -> tuple[dict[str, str], dict[str, str]]:
        auth = self.config.auth
        if auth.type == "none":
            return {}, {}
        token = self.resolve_token(auth.token_env) if auth.token_env else None
        if not token:
            if required:
                raise TiveloError(
                    ErrorCode.AUTHENTICATION_FAILED, "Daily source credentials are unavailable"
                )
            logger.warning("custom data source %s auth token is not set", self.name)
            return {}, {}
        if auth.type == "bearer":
            return {auth.header: f"Bearer {token}"}, {}
        if auth.type == "header":
            return {auth.header: token}, {}
        if auth.type == "query":
            return {}, {auth.param: token}
        if required:
            raise TiveloError(ErrorCode.AUTHENTICATION_FAILED, "Unsupported source authentication")
        return {}, {}


# Existing v1 providers import this public name.
CheckedDailyClient = CheckedHTTPClient
