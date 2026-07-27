"""Sycee policy for upstream strategy-authoring endpoints."""


_AUTHORING_ENDPOINTS = frozenset(
    {
        ("POST", "/api/strategies/ai/generate"),
        ("POST", "/api/strategies/ai/save"),
        ("POST", "/api/strategies/ai/test"),
        ("POST", "/api/strategies/build"),
        ("POST", "/api/strategies/build/stream"),
        ("POST", "/api/strategies/code/save"),
        ("POST", "/api/strategies/code/validate"),
        ("POST", "/api/strategies/reload"),
    }
)

_AI_ENDPOINTS = frozenset(
    {
        ("POST", "/api/strategies/ai/generate"),
        ("POST", "/api/strategies/ai/test"),
        ("POST", "/api/strategies/build"),
        ("POST", "/api/strategies/build/stream"),
    }
)


def _normalized_request(path: str, method: str) -> tuple[str, str]:
    return method.upper(), path.rstrip("/")


def is_strategy_authoring_request(path: str, method: str) -> bool:
    return _normalized_request(path, method) in _AUTHORING_ENDPOINTS


def is_ai_strategy_request(path: str, method: str) -> bool:
    return _normalized_request(path, method) in _AI_ENDPOINTS


def strategy_authoring_requires_admin(path: str, method: str) -> bool:
    """Return whether an upstream strategy request can author Python code."""

    normalized_method = method.upper()
    if is_strategy_authoring_request(path, method):
        return True
    if normalized_method != "DELETE" or not path.startswith("/api/strategies/"):
        return False

    strategy_id = path.removeprefix("/api/strategies/").strip("/")
    return bool(strategy_id) and "/" not in strategy_id
