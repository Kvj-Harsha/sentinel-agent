"""Static contracts for the existing Docker topology and readiness dependency chain."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return yaml.safe_load((ROOT / "extras" / name).read_text(encoding="utf-8"))


def test_alerting_readiness_uses_image_available_wget_and_bounded_probe():
    services = read("docker-compose.alerting.yml")["services"]
    for service, port in (("prometheus", 9090), ("alertmanager", 9093)):
        health = services[service]["healthcheck"]
        assert health["test"] == ["CMD", "/bin/wget", "-q", "-O", "/dev/null", "-T", "2", f"http://127.0.0.1:{port}/-/ready"]
        assert health["timeout"] == "3s"
    assert services["prometheus"]["depends_on"]["alertmanager"]["condition"] == "service_healthy"


def test_app_readiness_chain_has_no_monitoring_cycle():
    services = read("docker-compose.app.yml")["services"]
    assert services["sentinel-webhook"]["depends_on"]["sentinel"]["condition"] == "service_healthy"
    assert services["alertmanager"]["depends_on"]["sentinel-webhook"]["condition"] == "service_healthy"
    assert not services["sentinel"].get("depends_on"), "UI must start without waiting on the monitoring chain it enables"
    assert services["sentinel"]["environment"]["SENTINEL_RECEIVER_ENABLED"] == "false"


def test_published_ports_remain_loopback_and_webhook_private():
    files = ["docker-compose.alerting.yml", "docker-compose.app.yml", "docker-compose.loki.yml", "docker-compose.alloy.yml"]
    for file in files:
        for service in read(file)["services"].values():
            assert all(port.startswith("127.0.0.1:") for port in service.get("ports", []))
    assert not read("docker-compose.app.yml")["services"]["sentinel-webhook"].get("ports")


def test_agent_store_and_shared_volume_identity_are_consistent():
    config = read("docker-compose.app.yml")
    for service in ("sentinel", "sentinel-webhook"):
        item = config["services"][service]
        assert item["environment"]["SENTINEL_DB_PATH"] == "/var/lib/sentinel/sentinel.db"
        assert "sentinel-data:/var/lib/sentinel" in item["volumes"]
        assert "checkout-demo-settings:/demo-config" in item["volumes"]
        assert "sentinel-runtime" in item["networks"]
        assert "host.docker.internal:host-gateway" in item["extra_hosts"]
    assert config["volumes"]["checkout-demo-settings"]["external"] is True
