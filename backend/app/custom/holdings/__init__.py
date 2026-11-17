"""Manual holdings ledger, isolated from reminder and paper accounts."""
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "personal.holdings"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar):
    from .routes import router
    registrar.include_router(router)
