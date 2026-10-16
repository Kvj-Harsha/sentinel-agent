"""The default replay scenario must reach a SUPPORTED verdict with no network, using only labelled fixtures."""

import pytest

from coded_tools.sentinel.correlation_check import CorrelationCheck
from coded_tools.sentinel.github_deployments import GithubDeployments
from coded_tools.sentinel.loki_logs import LokiLogs
from coded_tools.sentinel.prometheus_metrics import PrometheusMetrics
from coded_tools.sentinel.runbook_search import RunbookSearch

STARTS_AT = "2026-10-12T14:02:00Z"
HYPOTHESIS = "The checkout-service v2.14.0 deploy introduced a NullPointerException in PaymentValidator.charge()."


@pytest.fixture(autouse=True)
def replay_mode(monkeypatch):
    monkeypatch.setenv("SENTINEL_MODE", "demo")
    monkeypatch.setenv("LOKI_URL", "http://127.0.0.1:1")  # any accidental live call would fail loudly


def test_replay_scenario_reaches_supported_verdict():
    sly_data: dict = {}
    args = {"service_name": "checkout-service", "starts_at": STARTS_AT}
    for tool, tool_args in (
        (LokiLogs(), args),
        (PrometheusMetrics(), args),
        (GithubDeployments(), {"service_name": "checkout-service", "environment": "prod"}),
    ):
        result = tool.invoke(tool_args, sly_data)
        assert result["source_status"] == "fixture"

    verdict = CorrelationCheck().invoke({"service_name": "checkout-service", "hypothesis": HYPOTHESIS}, sly_data)

    assert verdict["verdict"] == "SUPPORTED"
    assert verdict["delta_minutes"] == 4.0
    kinds = {record["evidence_type"] for record in sly_data["sentinel_verified"]["evidence"]}
    assert {"log", "metric", "deployment_change"} <= kinds
    assert all(record["source_status"] == "fixture" for record in sly_data["sentinel_verified"]["evidence"])


def test_replay_runbook_is_fixture_even_when_confluence_is_configured(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://example.invalid/wiki")
    result = RunbookSearch().invoke({"query": "checkout-service NullPointerException tax library deploy"}, {})
    assert result["source_status"] == "fixture"
    assert result["matches"]
