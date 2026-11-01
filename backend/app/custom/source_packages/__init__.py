"""External data-source package management, registered through existing L2 routes."""
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "official.source-packages"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar):
    from .routes import router
    registrar.include_router(router)
