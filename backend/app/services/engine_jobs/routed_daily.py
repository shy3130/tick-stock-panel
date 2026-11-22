"""Bind workbench stock data capabilities to independent providers."""

import os
from datetime import UTC, datetime

from pydantic import ValidationError
from tivelo_contracts.errors import ErrorCode, TiveloError

from app.config import settings
from app.data_providers import custom as custom_sources
from app.data_providers.capabilities import resolve_capability_sources
from app.data_providers.custom import provider as custom_http
from app.data_providers.custom.config import CustomSourceConfig
from app.services import preferences


def _tickflow_source(source, *, factors=False):
    from tivelo_data.tickflow_daily import TickFlowDailyConfig

    from app import secrets_store
    from app.tickflow import client, policy
    from app.tickflow.capabilities import Cap, CapabilitySet
    from app.tickflow.rate_limits import resolve_limit

    # Preview reads only the routed mode and cached limits. Never start an API probe.
    caps = policy.cached_tickflow_capabilities()
    capability = Cap.ADJ_FACTOR if factors else Cap.KLINE_DAILY_BATCH
    if caps is not None and not caps.has(capability):
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY, "TickFlow access to this dataset is unavailable"
        )
    # A grant for this endpoint can exist on a custom subscription.
    mode = (
        "paid" if (factors and caps is not None) or client.current_mode() == "api_key" else "free"
    )
    if factors and mode != "paid":
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY, "TickFlow factor access requires a paid connection"
        )
    endpoint = (
        (client.paid_endpoint() if factors else client.current_endpoint())
        if mode == "paid"
        else os.environ.get("TICKFLOW_FREE_BASE_URL") or client.FREE_ENDPOINT
    )
    limit = resolve_limit(
        caps or CapabilitySet(),
        capability,
        default_batch=50 if factors else 100,
        default_rpm=60 if mode == "free" else 30,
    )
    try:
        config = TickFlowDailyConfig(
            mode=mode,
            endpoint=endpoint,
            credential_ref="TICKFLOW_API_KEY" if mode == "paid" else None,
            batch=min(limit.batch, 100),
            rpm=limit.rpm,
        )
    except (TypeError, ValidationError):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid TickFlow source settings") from None
    if factors:
        from tivelo_data.tickflow_factors import TickFlowFactorProvider as Provider
    else:
        from tivelo_data.tickflow_daily import TickFlowDailyProvider as Provider
    return Provider(source, config, resolve_token=lambda _: secrets_store.get_tickflow_key())


def _bundled_source(source, *, factors=False):
    if not custom_sources.is_builtin(source.expected_provider):
        raise TiveloError(
            ErrorCode.SOURCE_UNAVAILABLE, "Selected bundled stock source is unavailable"
        )
    try:
        if source.expected_provider == "fuyao":
            from tivelo_data.fuyao_daily import FuyaoDailyConfig as Config

            from app.plugins.fuyao.provider import get_api_key

            if factors:
                from tivelo_data.fuyao_factors import FuyaoFactorProvider as Provider
            else:
                from tivelo_data.fuyao_daily import FuyaoDailyProvider as Provider
        else:
            from tivelo_data.tdx_daily import TdxQuantDailyConfig as Config

            from app.plugins.tdxquant.provider import get_api_key

            if factors:
                from tivelo_data.tdx_factors import TdxQuantFactorProvider as Provider
            else:
                from tivelo_data.tdx_daily import TdxQuantDailyProvider as Provider
    except ModuleNotFoundError as exc:
        if exc.name not in {
            "tivelo_data.fuyao_factors",
            "tivelo_data.tdx_factors",
            "tivelo_market_cn",
            "httpx",
            "tdxaidata",
        }:
            raise
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "Installed Tivelo Data lacks this stock source capability",
        ) from None
    return Provider(source, Config(), resolve_token=lambda _: get_api_key())


def check_workspace(store):
    if store.data_dir != settings.data_dir.resolve():
        raise TiveloError(
            ErrorCode.DATA_CONFLICT, "Source routing belongs to a different workspace"
        )


def open_minute_source(store, source):
    check_workspace(store)
    if preferences.configured_minute_data_provider() != source.expected_provider:
        raise TiveloError(ErrorCode.DATA_CONFLICT, "The selected minute source changed")
    if source.expected_provider == "tickflow" or custom_sources.is_builtin(source.expected_provider):
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Source has no declared HTTP minute adapter")
    selected = next(row for row in resolve_capability_sources() if row["id"] == "minute")
    if selected["source"] != source.expected_provider or not selected["usable"]:
        raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "The selected minute source is unavailable")
    handle = custom_sources.get_provider(source.expected_provider)
    if handle.builtin or not isinstance(handle.config, CustomSourceConfig):
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Source has no checked minute adapter")
    try:
        from tivelo_data.http.cn_minute import CNHTTPMinuteProvider
    except ModuleNotFoundError as exc:
        if exc.name not in {"tivelo_data.http.cn_minute", "tivelo_market_cn", "httpx"}:
            raise
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Installed Tivelo Data lacks the HTTP minute adapter") from None
    return CNHTTPMinuteProvider(source, handle.config, resolve_token=custom_http._token_from_env)


def open_factor_source(store, source):
    from tivelo_data.adapters import cn_daily

    check_workspace(store)
    if preferences.configured_adj_factor_provider() != source.expected_provider:
        raise TiveloError(ErrorCode.DATA_CONFLICT, "The selected factor source changed")
    if not hasattr(cn_daily, "CNFactorRequest"):
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "This Tivelo Data release has no live factor adapter")
    if source.expected_provider == "tickflow":
        return _tickflow_source(source, factors=True)
    selected = next(row for row in resolve_capability_sources() if row["id"] == "adj_factor")
    if selected["source"] != source.expected_provider or not selected["usable"]:
        raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "The selected factor source is unavailable")
    if source.expected_provider in ("fuyao", "tdxquant"):
        return _bundled_source(source, factors=True)
    handle = custom_sources.get_provider(source.expected_provider)
    if handle.builtin or not isinstance(handle.config, CustomSourceConfig):
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Source has no checked factor adapter")
    from tivelo_data.http.cn_factors import CNHTTPFactorProvider

    return CNHTTPFactorProvider(source, handle.config, resolve_token=custom_http._token_from_env)


def open_daily_source(store, source):
    check_workspace(store)
    if preferences.configured_daily_data_provider() != source.expected_provider:
        raise TiveloError(ErrorCode.DATA_CONFLICT, "The selected daily source changed")
    daily = next(row for row in resolve_capability_sources() if row["id"] == "daily")
    if daily["source"] != source.expected_provider or not daily["usable"]:
        raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "The selected daily source is unavailable")
    if source.expected_provider == "tickflow":
        return _tickflow_source(source)
    if source.expected_provider in ("fuyao", "tdxquant"):
        return _bundled_source(source)
    if not custom_sources.provider_has_dataset(source.expected_provider, "daily"):
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Source has no checked daily adapter")
    handle = custom_sources.get_provider(source.expected_provider)
    config = handle.config
    if handle.builtin or not isinstance(config, CustomSourceConfig):
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Source has no checked daily adapter")
    from tivelo_data.http.cn_daily import CNDailyProvider

    return CNDailyProvider(
        source, config, resolve_token=custom_http._token_from_env, now=lambda: datetime.now(UTC)
    )
