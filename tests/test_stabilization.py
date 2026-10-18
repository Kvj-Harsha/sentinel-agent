"""Regression coverage for web integration and safe local startup."""
import copy
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
import requests

import web
import conversation_store
from coded_tools.sentinel import servicenow_client
from coded_tools.sentinel.sqlite_store import SQLiteDatabase
from coded_tools.sentinel.open_ticket import OpenTicket


@pytest.fixture
def isolated_web(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTINEL_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv("SENTINEL_SEED_LEGACY_DATA", "false")
    monkeypatch.setenv("SERVICENOW_ENABLED", "false")
    monkeypatch.setenv("SENTINEL_GITHUB_TOKEN", "")
    monkeypatch.setenv("SENTINEL_AUTO_PR", "false")
    monkeypatch.setattr(web, "_conversation", {
        "id": None, "messages": [], "chat_context": {}, "sly_data": {},
        "ticket_number": None, "alert_context": None, "running": False, "updated": 0,
    })
    return web


@pytest.fixture
def http_app(isolated_web):
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.parametrize("body", [[], None, 42, "bad"])
def test_api_requires_json_object(http_app, body):
    response = requests.post(http_app + "/api/investigate", data=json.dumps(body),
                             headers={"X-Sentinel": "1"}, timeout=3)
    assert response.status_code == 400


def test_api_body_size_limit(http_app):
    response = requests.post(http_app + "/api/new", data=b" " * (web.MAX_BODY_BYTES + 1),
                             headers={"X-Sentinel": "1"}, timeout=3)
    assert response.status_code == 413


def test_oversized_headers_without_body_are_rejected_immediately(http_app):
    host = http_app.removeprefix("http://")
    connection = http.client.HTTPConnection(host, timeout=2)
    try:
        connection.putrequest("POST", "/api/new")
        connection.putheader("X-Sentinel", "1")
        connection.putheader("Content-Length", str(web.MAX_BODY_BYTES * 100))
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 413
        assert "too large" in response.read().decode()
    finally:
        connection.close()


def test_reset_cannot_detach_running_worker(isolated_web):
    web._conversation.update(id="active", running=True)
    with pytest.raises(RuntimeError, match="running"):
        web.reset_incident()
    assert web._conversation["id"] == "active"


@pytest.mark.parametrize("body", [{"text": ["invalid"]}, {"text": "test", "fresh": "false"},
                                       {"text": "a" * 4001}])
def test_investigate_rejects_invalid_contract(isolated_web, monkeypatch, body):
    calls = []
    monkeypatch.setattr(web, "start_turn", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        web.api_investigate(body)
    assert calls == []


def test_restart_releases_recent_orphaned_worker(isolated_web):
    conversation_store.persist("interrupted", {**web._conversation, "running": True})
    web._restore()
    assert web._conversation["running"] is False
    assert "interrupted" in web._conversation["messages"][-1]["content"]


def test_seed_flag_false_is_respected(isolated_web):
    assert SQLiteDatabase().seed_legacy_data is False


def test_servicenow_requires_explicit_enable(monkeypatch):
    monkeypatch.delenv("SERVICENOW_ENABLED", raising=False)
    for key in ("SERVICENOW_INSTANCE", "SERVICENOW_USER", "SERVICENOW_PASSWORD"):
        monkeypatch.setenv(key, "fixture")
    assert servicenow_client.is_configured() is False


def test_disabled_servicenow_cannot_write(monkeypatch):
    monkeypatch.setenv("SERVICENOW_ENABLED", "false")
    monkeypatch.setattr(requests, "request", lambda *a, **k: pytest.fail("External request attempted"))
    with pytest.raises(servicenow_client.ServiceNowError, match="disabled"):
        servicenow_client._request("POST", "incident", {})


def test_alert_cli_bootstraps_repository_path():
    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run([sys.executable, "apps/sentinel/alerts.py", "--help"],
                            cwd=root, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_http_auth_health_and_static(http_app):
    assert requests.get(http_app + "/healthz", timeout=3).json() == {"status": "ok"}
    assert requests.get(http_app + "/", timeout=3).status_code == 200
    assert requests.post(http_app + "/api/new", json={}, timeout=3).status_code == 403
    assert requests.post(http_app + "/api/pr/merge", json={"approved": False},
                         headers={"X-Sentinel": "1"}, timeout=3).status_code == 400


def test_http_agent_success_failure_and_retry(http_app, monkeypatch):
    def invoke(text, **kwargs):
        if text == "fail":
            raise requests.Timeout("test provider timeout")
        state = dict(kwargs["sly_data"])
        OpenTicket().invoke({"service_name": "checkout-service", "short_description": "Mock provider fixture"}, state)
        return {"text": "Investigation complete", "sly_data": state}
    monkeypatch.setattr(web, "invoke_agent", invoke)
    for text in ("success", "fail", "retry"):
        response = requests.post(http_app + "/api/investigate", json={"text": text},
                                 headers={"X-Sentinel": "1"}, timeout=3)
        assert response.status_code == 200
        deadline = time.monotonic() + 3
        while web._conversation["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not web._conversation["running"]
        expected = "test provider timeout" if text == "fail" else "Investigation complete"
        assert expected in web._conversation["messages"][-1]["content"]
    ticket = web.TicketStore().get(web._conversation["ticket_number"])
    assert ticket["session_id"] == web._conversation["sly_data"]["incident_session_id"]
    assert len(web.TicketStore().load()) == 1
