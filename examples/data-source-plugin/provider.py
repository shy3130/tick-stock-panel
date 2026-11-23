"""Example only: vendor data must already match the documented normalized schema."""
from types import SimpleNamespace

from app import secrets_store
from app.data_providers.base import ProviderRequestError

from .client import fetch


def probe_api_key(key):
    try:
        fetch(key)
        return True, "接口可连接；其他能力权限需另行验证"
    except Exception:  # noqa: BLE001 — never return raw HTTP errors containing credentials
        return False, "验证失败，请检查接口地址、Key、权限与网络"


class Provider:
    name = "example_http"

    def __init__(self):
        self.config = SimpleNamespace(datasets={"realtime": {}})
        self.key = secrets_store.get_env_backed_secret("example_http_api_key", "EXAMPLE_HTTP_API_KEY")

    def get_realtime(self):
        try:
            return fetch(self.key)
        except Exception as exc:
            raise ProviderRequestError("行情请求失败，请检查供应商服务") from exc

    def close(self):
        pass
