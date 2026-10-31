"""模拟仓策略执行扩展；订单、撮合、台账仍由原 paper 域负责。"""

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "official.paper-execution"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())
