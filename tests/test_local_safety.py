"""The local Compose environment must block remote writes despite enabled host settings."""

from pathlib import Path

import pytest
import requests
import yaml

from coded_tools.sentinel import github_pr_client, servicenow_client


LOCAL_COMPOSE = Path(__file__).resolve().parents[1] / "extras" / "docker-compose.local.yml"


@pytest.mark.parametrize("service", ["sentinel", "sentinel-webhook"])
def test_local_compose_blocks_remote_writes(monkeypatch, service):
    # Simulate a host .env that enables both adapters before Compose overrides it.
    for key, value in {
        "SERVICENOW_ENABLED": "true",
        "SERVICENOW_INSTANCE": "fixture-instance",
        "SERVICENOW_USER": "fixture-user",
        "SERVICENOW_PASSWORD": "fixture-password",
        "SENTINEL_AUTO_PR": "true",
        "SENTINEL_GITHUB_TOKEN": "fixture-token",
        "SENTINEL_DEMO_GITHUB_OWNER": "fixture-owner",
        "SENTINEL_DEMO_GITHUB_REPO": "fixture-repo",
        "SENTINEL_STORAGE_BACKEND": "json",
    }.items():
        monkeypatch.setenv(key, value)
    assert servicenow_client.is_configured()
    assert github_pr_client.is_configured()

    environment = yaml.safe_load(LOCAL_COMPOSE.read_text(encoding="utf-8"))["services"][service]["environment"]
    for key, value in environment.items():
        assert isinstance(value, str), f"Compose environment {key} must be a string"
        monkeypatch.setenv(key, value)

    def unexpected_network(*args, **kwargs):
        pytest.fail("Disabled adapter attempted a network connection")

    monkeypatch.setattr(requests, "request", unexpected_network)
    monkeypatch.setattr(requests, "Session", unexpected_network)
    assert not servicenow_client.is_configured()
    assert not github_pr_client.is_configured()
    assert environment["SENTINEL_AUTO_PR"] == "false"
    assert environment["SENTINEL_STORAGE_BACKEND"] == "sqlite"
    for key in ("SERVICENOW_INSTANCE", "SERVICENOW_USER", "SERVICENOW_PASSWORD", "SENTINEL_GITHUB_TOKEN"):
        assert environment[key] == ""
    with pytest.raises(servicenow_client.ServiceNowError, match="disabled"):
        servicenow_client._request("POST", "incident", {})
    with pytest.raises(github_pr_client.GithubPullRequestError):
        github_pr_client._session()
