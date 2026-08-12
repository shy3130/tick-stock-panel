from pathlib import Path

import yaml


def test_compose_app_has_local_http_healthcheck() -> None:
    project_root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((project_root / "docker-compose.yml").read_text(encoding="utf-8"))

    healthcheck = compose["services"]["app"]["healthcheck"]
    command = healthcheck["test"]

    assert command[:2] == ["CMD", "python"]
    assert "http://127.0.0.1:3018/health" in " ".join(command)
    assert healthcheck["interval"] == "30s"
    assert healthcheck["timeout"] == "5s"
    assert healthcheck["retries"] == 3
    assert healthcheck["start_period"] == "90s"


def test_compose_keeps_public_edge_on_loopback_and_trusts_only_fixed_proxy() -> None:
    project_root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((project_root / "docker-compose.yml").read_text(encoding="utf-8"))

    service = compose["services"]["app"]
    assert service["ports"] == ["127.0.0.1:${PORT:-3018}:3018"]

    environment = service["environment"]
    forwarded = next(item for item in environment if item.startswith("FORWARDED_ALLOW_IPS="))
    assert forwarded == "FORWARDED_ALLOW_IPS=${FORWARDED_ALLOW_IPS:-127.0.0.1,172.21.0.1}"
    assert "*" not in forwarded

    network_config = compose["networks"]["default"]["ipam"]["config"]
    assert network_config == [{"subnet": "172.21.0.0/16", "gateway": "172.21.0.1"}]
