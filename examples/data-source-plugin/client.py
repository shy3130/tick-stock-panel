"""Replace this URL and field mapping with your provider's documented contract."""
import httpx

BASE_URL = "https://example.invalid"


def fetch(key: str) -> list[dict]:
    if BASE_URL.endswith(".invalid"):
        raise ValueError("请先修改模板的接口地址")
    with httpx.Client(timeout=15) as client:
        response = client.get(f"{BASE_URL}/quotes", headers={"Authorization": f"Bearer {key}"})
        response.raise_for_status()
        return response.json()["data"]
