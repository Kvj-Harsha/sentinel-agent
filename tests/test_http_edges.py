"""Exercise real HTTP boundaries without model calls or external writes."""
import concurrent.futures
import http.client
import json
import socket
import threading
from http.server import ThreadingHTTPServer

import pytest
import requests

import web
import alertmanager_receiver as receiver


@pytest.fixture(params=["ui", "webhook"])
def endpoint(request, monkeypatch):
    webhook = request.param == "webhook"
    monkeypatch.setattr(receiver, "_webhook_token", lambda: "fixture-token-24-characters")
    monkeypatch.setattr(receiver, "_queue_alert", lambda alert: {"status": "accepted"})
    monkeypatch.setattr(receiver, "compose_alertmanager_alert", lambda alert: alert)
    monkeypatch.setattr(web, "reset_incident", lambda: None)
    module = receiver if webhook else web
    monkeypatch.setattr(module, "REQUEST_TIMEOUT_SECONDS", 0.2, raising=False)
    handler = receiver.AlertmanagerHandler if webhook else web.Handler
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {
        "address": server.server_address,
        "url": f"http://127.0.0.1:{server.server_port}",
        "route": "/alerts/alertmanager" if webhook else "/api/new",
        "auth": [("Authorization", "Bearer fixture-token-24-characters")] if webhook else [("X-Sentinel", "1")],
        "webhook": webhook,
    }
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def raw_post(endpoint, headers, body=b"", *, half_close=True):
    connection = socket.create_connection(endpoint["address"], timeout=1)
    try:
        lines = [f"POST {endpoint['route']} HTTP/1.0", "Host: localhost"]
        lines += [f"{key}: {value}" for key, value in endpoint["auth"] + headers]
        connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        if half_close:
            connection.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(connection)
        response.begin()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize("payload", [b"\xff", b"{bad", b"[]", b"null"])
def test_malformed_payload(endpoint, payload):
    status, _ = raw_post(endpoint, [("Content-Length", str(len(payload)))], payload)
    assert status == 400


@pytest.mark.parametrize("headers", [[], [("Content-Length", "bad")],
    [("Content-Length", "-1")], [("Content-Length", "2"), ("Content-Length", "2")],
    [("Content-Length", "2"), ("Transfer-Encoding", "chunked")]])
def test_ambiguous_or_invalid_framing(endpoint, headers):
    payload = b'{"alerts":[{}]}'
    headers = [(key, str(len(payload)) if value == "2" else value) for key, value in headers]
    status, _ = raw_post(endpoint, headers, payload)
    assert status == 400


def test_truncated_valid_json_is_rejected(endpoint):
    # A syntactically valid prefix must not bypass the declared length.
    payload = b'{"alerts":[{}]}' if endpoint["webhook"] else b"{}"
    status, _ = raw_post(endpoint, [("Content-Length", str(len(payload) + 20))], payload)
    assert status == 400


def test_stalled_body_times_out_and_server_recovers(endpoint):
    status, _ = raw_post(endpoint, [("Content-Length", "20")], b"{", half_close=False)
    assert status == 408
    assert requests.get(endpoint["url"] + "/healthz", timeout=2).status_code == 200


def test_routes_auth_and_unsupported_method(endpoint):
    url = endpoint["url"]
    assert requests.post(url + endpoint["route"], json={}, timeout=2).status_code == (401 if endpoint["webhook"] else 403)
    assert requests.get(url + "/missing", timeout=2).status_code == 404
    assert requests.post(url + "/missing", json={}, headers=dict(endpoint["auth"]), timeout=2).status_code == 404
    assert requests.put(url + endpoint["route"], json={}, timeout=2).status_code == 501


def test_valid_body_after_rejections(endpoint):
    payload = {"alerts": [{}]} if endpoint["webhook"] else {}
    response = requests.post(endpoint["url"] + endpoint["route"], json=payload,
                             headers=dict(endpoint["auth"]), timeout=2)
    assert response.status_code == (202 if endpoint["webhook"] else 200)
    if endpoint["webhook"]:
        assert response.json()["accepted"] == [{"status": "accepted"}]
    else:
        assert response.json() == {"ok": True}


def test_concurrent_bad_requests_do_not_break_health(endpoint):
    def send(index):
        if index % 2:
            return requests.get(endpoint["url"] + "/healthz", timeout=3).status_code
        return requests.post(endpoint["url"] + endpoint["route"], data=b"broken",
                             headers=dict(endpoint["auth"]), timeout=3).status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(send, range(12)))
    assert results == [400, 200] * 6
