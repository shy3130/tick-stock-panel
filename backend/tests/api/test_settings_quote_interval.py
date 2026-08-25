from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import settings as settings_api


def _client_without_quote_service() -> TestClient:
    app = FastAPI()
    app.include_router(settings_api.router)
    return TestClient(app)


def test_update_quote_interval_without_running_service_is_safe():
    response = _client_without_quote_service().put(
        "/api/settings/preferences/quote-interval",
        json={"interval": 10},
    )

    assert response.status_code == 200
    assert response.json() == {
        "interval": 10.0,
        "min_interval": 5.0,
        "max_interval": 60.0,
    }


def test_removed_noop_preference_routes_are_not_exposed():
    paths = {route.path for route in settings_api.router.routes}

    assert "/api/settings/preferences/realtime-watchlist" not in paths
    assert "/api/settings/preferences/pipeline-index-symbols" not in paths
