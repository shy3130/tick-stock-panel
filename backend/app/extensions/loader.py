"""Discover in-repository backend customizations without touching user data."""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.extensions.contracts import BACKEND_EXTENSION_API_VERSION, ExtensionContext
from app.extensions.registry import BackendExtensionRegistrar, BackendExtensionRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackendExtensionLoadError:
    module: str
    error: str


def _custom_module_names() -> list[str]:
    try:
        package = importlib.import_module("app.custom")
    except ModuleNotFoundError:
        return []
    return sorted(
        item.name
        for item in pkgutil.iter_modules(package.__path__, f"{package.__name__}.")
        if not item.name.rsplit(".", 1)[-1].startswith("_")
    )


def configure_backend_extensions(
    app: FastAPI,
) -> tuple[BackendExtensionRegistry, tuple[BackendExtensionLoadError, ...]]:
    """Import custom modules and register validated routes and policies."""
    registry = BackendExtensionRegistry()
    errors: list[BackendExtensionLoadError] = []

    for module_name in _custom_module_names():
        try:
            module = importlib.import_module(module_name)
            extension_id = getattr(module, "EXTENSION_ID", None)
            api_version = getattr(module, "EXTENSION_API_VERSION", None)
            if not isinstance(extension_id, str):
                raise ValueError("backend extension module must define EXTENSION_ID")
            registrar = BackendExtensionRegistrar(extension_id, api_version=api_version)
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise ValueError("backend extension module must define setup(registrar)")
            setup(registrar)
            _validate_router_conflicts(app, registrar)
            registry.register(registrar)
            for router in registrar.routers:
                app.include_router(router)
        except Exception as exc:
            logger.warning("backend extension load failed %s: %s", module_name, exc)
            errors.append(BackendExtensionLoadError(module_name, str(exc)))

    registry.freeze()
    return registry, tuple(errors)


def _validate_router_conflicts(app: FastAPI, registrar: BackendExtensionRegistrar) -> None:
    existing = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    staged: set[tuple[str, str]] = set()
    for router in registrar.routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods:
                key = (route.path, method)
                if key in existing or key in staged:
                    raise ValueError(
                        f"extension {registrar.extension_id!r} route conflicts: "
                        f"{method} {route.path}"
                    )
                staged.add(key)


def start_backend_extensions(
    context: ExtensionContext,
    registry: BackendExtensionRegistry,
) -> None:
    """Run optional post-core startup hooks after the stable context is available."""
    for module_name in _custom_module_names():
        try:
            module = importlib.import_module(module_name)
            if getattr(module, "EXTENSION_ID", None) not in registry.extension_ids():
                continue
            startup = getattr(module, "startup", None)
            if callable(startup):
                startup(context)
        except Exception as exc:
            logger.warning("backend extension startup failed %s: %s", module_name, exc)


def current_extension_context(*, data_dir, repository) -> ExtensionContext:
    from app.extensions.market_access import CoreMarketAccess

    return ExtensionContext(
        api_version=BACKEND_EXTENSION_API_VERSION,
        data_dir=data_dir,
        repository=repository,
        market=CoreMarketAccess(data_dir, repository),
    )
