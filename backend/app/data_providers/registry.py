"""Provider registry."""
from __future__ import annotations

import logging
import os
import threading
import weakref
from collections.abc import Mapping

from app.data_providers.fquant_provider import FQuantProvider

logger = logging.getLogger(__name__)

_PROVIDERS = {
    "fquant": FQuantProvider,
    "fquant_local": lambda: FQuantProvider(name="fquant_local"),
}

# Live module-level provider 单例（FQuantProvider 等）持有长生命周期 DuckDB 连接，
# lifespan 关闭链需要把它们一并关掉。用 WeakSet 跟踪：进程内模块级单例只要还被
# 引用就留在集合里；临时请求级 provider 没有其他引用时会自动回收，不占用内存。
_LIVE_PROVIDERS: weakref.WeakSet = weakref.WeakSet()
_LIVE_PROVIDERS_LOCK = threading.Lock()


def normalize_provider_name(name: str | None, default: str = "fquant_local") -> str:
    provider_name = (name or default).strip().lower() or default
    if provider_name not in _PROVIDERS:
        raise ValueError(f"Unsupported data provider: {name}")
    return provider_name


def get_active_provider_name(capability: str | None = None) -> str:
    """Return the process-effective provider name.

    ``DATA_PROVIDER`` is an ops-level override. When it is absent, the persisted
    preferences value drives local UI/runtime switching.
    """
    env_provider = os.environ.get("DATA_PROVIDER")
    if env_provider:
        try:
            return normalize_provider_name(env_provider)
        except ValueError:
            logger.warning("Unsupported DATA_PROVIDER=%s, falling back to fquant_local", env_provider)
            return "fquant_local"

    try:
        from app.services import preferences
        if capability == "daily":
            return normalize_provider_name(preferences.get_daily_data_provider())
        if capability == "minute":
            return normalize_provider_name(preferences.get_minute_data_provider())
        if capability == "realtime":
            return normalize_provider_name(preferences.get_realtime_data_provider())
        if capability == "financial":
            return normalize_provider_name(preferences.get_financial_data_provider())
        if capability == "depth":
            return normalize_provider_name(preferences.get_depth_data_provider())
        if capability == "adj_factor":
            provider = preferences.get_adj_factor_provider()
            if provider == "same_as_daily":
                provider = preferences.get_daily_data_provider()
            return normalize_provider_name(provider)
        return normalize_provider_name(preferences.get_data_provider())
    except Exception:  # noqa: BLE001
        return "fquant_local"


def get_provider(
    name: str = "fquant_local",
    *,
    snapshot_paths: Mapping[str, str] | None = None,
):
    provider_name = normalize_provider_name(name)
    if snapshot_paths is not None:
        provider = FQuantProvider(name=provider_name, snapshot_paths=snapshot_paths)
    else:
        provider = _PROVIDERS[provider_name]()
    # 跟踪返回的对象：模块级单例只要还被引用就留存；WeakSet 不阻碍回收，也不持有
    # 强引用，因此不会改变 get_provider() 的所有权语义。
    if hasattr(provider, "close"):
        with _LIVE_PROVIDERS_LOCK:
            _LIVE_PROVIDERS.add(provider)
    return provider


def close_all_providers() -> None:
    """关闭所有已注册 provider 持有的长生命周期连接。

    先在锁内拷贝并清空 WeakSet，再在锁外逐个调用 ``close``：单个 provider 的关闭
    可能较慢或抛异常，不应阻塞/拖累其它 provider 的关闭，也不应持锁引发死锁。
    """
    with _LIVE_PROVIDERS_LOCK:
        providers = [p for p in _LIVE_PROVIDERS]
        _LIVE_PROVIDERS.clear()
    for provider in providers:
        close = getattr(provider, "close", None)
        if close is None:
            continue
        try:
            close()
        except Exception:  # noqa: BLE001
            logger.warning("close_all_providers: 关闭 %r 失败", provider, exc_info=True)
