"""Token-protected Prometheus Alertmanager webhook for Sentinel's local demo stack."""

import hmac
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from datetime import timezone
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any
from typing import Dict
from typing import List
from urllib.parse import urlparse

from alerts import compose_alertmanager_alert
from alerts import compose_incident_report
from run import format_verified_facts
from run import invoke_agent
from coded_tools.sentinel.ticket_store import TicketStore

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 1_048_576
MAX_ALERTS_PER_REQUEST = 5
FINGERPRINT_TTL_SECONDS = 3600
_lock = threading.Lock()
_jobs: List[Dict[str, Any]] = []
_fingerprints: Dict[str, float] = {}
_server: ThreadingHTTPServer = None


def _utc_now() -> str:
    """:return: current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot_jobs() -> List[Dict[str, Any]]:
    """Returns safe copies of recent webhook jobs and links tickets as soon as they are stored."""
    with _lock:
        jobs = list(reversed(_jobs[-20:]))
    for job in jobs:
        if job.get("ticket_number") or job.get("state") not in ("queued", "investigating"):
            continue
        ticket_count_before = job.get("ticket_count_before")
        if ticket_count_before is None:
            continue
        tickets = TicketStore().load()
        for ticket in reversed(tickets[ticket_count_before:]):
            if ticket.get("service_name") == job.get("service"):
                job["ticket_number"] = ticket.get("number")
                job["ticket_detected_at"] = _utc_now()
                break
    return [dict(job) for job in jobs]


def _json_bytes(body: Dict[str, Any]) -> bytes:
    """:return: compact UTF-8 JSON response bytes."""
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _process_job(job: Dict[str, Any]) -> None:
    """Runs the existing Sentinel investigation workflow for one accepted alert."""
    try:
        job["state"] = "investigating"
        report = compose_incident_report(job["alert"])
        job["report"] = report
        response = invoke_agent(report)
        sly_data = response.get("sly_data") or {}
        job["response"] = response.get("text", "")
        job["verified_facts"] = sly_data.get("sentinel_verified") or {}
        job["verified_markdown"] = format_verified_facts(sly_data)
        job["ticket_number"] = sly_data.get("sentinel_ticket_number")
        job["state"] = "completed"
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        LOGGER.exception("Alertmanager job %s failed", job["id"])
        job["error"] = str(exc)
        job["state"] = "failed"
    except Exception as exc:  # pylint: disable=broad-exception-caught
        LOGGER.exception("Unexpected Alertmanager job failure: %s", job["id"])
        job["error"] = f"Unexpected investigation error: {exc}"
        job["state"] = "failed"
    finally:
        job["updated_at"] = _utc_now()


def _queue_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    """Queues a normalized alert and suppresses Alertmanager retry duplicates."""
    fingerprint = str(alert.get("fingerprint") or "")
    ticket_count_before = len(TicketStore().load())
    now = time.monotonic()
    with _lock:
        expired = [key for key, seen_at in _fingerprints.items() if now - seen_at > FINGERPRINT_TTL_SECONDS]
        for key in expired:
            _fingerprints.pop(key, None)
        if fingerprint and fingerprint in _fingerprints:
            return {"status": "duplicate", "fingerprint": fingerprint}

        job = {
            "id": uuid.uuid4().hex[:12],
            "received_at": _utc_now(),
            "updated_at": _utc_now(),
            "state": "queued",
            "ticket_count_before": ticket_count_before,
            "alert_name": alert["title"],
            "service": alert["tags"][0].split(":", 1)[1],
            "severity": (alert.get("body") or "").split(".", 1)[0].replace("Severity: ", ""),
            "alert": alert,
        }
        _jobs.append(job)
        del _jobs[:-50]
        if fingerprint:
            _fingerprints[fingerprint] = now

    threading.Thread(target=_process_job, args=(job,), daemon=True, name=f"sentinel-alert-{job['id']}").start()
    return {"status": "accepted", "job_id": job["id"], "fingerprint": fingerprint}


class AlertmanagerHandler(BaseHTTPRequestHandler):
    """Accepts Alertmanager webhook requests and exposes a lightweight health endpoint."""

    server_version = "SentinelAlertReceiver/1.0"

    def _send(self, status: int, body: Dict[str, Any]) -> None:
        """Writes a JSON response with explicit content length."""
        encoded = _json_bytes(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        """Reports receiver health without returning alert or credential data."""
        if urlparse(self.path).path == "/healthz":
            self._send(200, {"status": "ok", "service": "sentinel-alertmanager-webhook"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        """Validates and queues Alertmanager's webhook JSON payload."""
        if urlparse(self.path).path != "/alerts/alertmanager":
            self._send(404, {"error": "not found"})
            return

        expected_token = os.environ.get("SENTINEL_WEBHOOK_TOKEN", "")
        provided = self.headers.get("Authorization", "")
        valid_auth = bool(expected_token) and hmac.compare_digest(provided, f"Bearer {expected_token}")
        if not valid_auth:
            self._send(401, {"error": "missing or invalid bearer token"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return
        if content_length < 1 or content_length > MAX_REQUEST_BYTES:
            self._send(413, {"error": "request body must be between 1 byte and 1 MiB"})
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
            raw_alerts = payload.get("alerts", [])
            if not isinstance(raw_alerts, list) or not raw_alerts:
                raise ValueError("payload must contain at least one alert in 'alerts'")
            if len(raw_alerts) > MAX_ALERTS_PER_REQUEST:
                raise ValueError(f"at most {MAX_ALERTS_PER_REQUEST} alerts per webhook request are accepted")

            accepted = []
            ignored = []
            for raw_alert in raw_alerts:
                try:
                    normalized = compose_alertmanager_alert(raw_alert)
                except ValueError as exc:
                    ignored.append({"alert": (raw_alert.get("labels") or {}).get("alertname", "unknown"),
                                    "reason": str(exc)})
                    continue
                accepted.append(_queue_alert(normalized))
            self._send(202, {"accepted": accepted, "ignored": ignored, "receiver": "sentinel"})
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError, ValueError) as exc:
            self._send(400, {"error": str(exc)})

    def log_message(self, format_string: str, *args: Any) -> None:
        """Logs concise request metadata without logging headers or alert payloads."""
        LOGGER.info("Alertmanager webhook: %s", format_string % args)


def start_receiver() -> None:
    """Starts the webhook server once per Streamlit process; requires a configured shared token."""
    global _server
    if _server is not None:
        return
    token = os.environ.get("SENTINEL_WEBHOOK_TOKEN", "")
    if len(token) < 24:
        LOGGER.warning("Alertmanager receiver disabled: configure a 24+ character SENTINEL_WEBHOOK_TOKEN")
        return
    host = os.environ.get("SENTINEL_WEBHOOK_HOST", "127.0.0.1")
    port = int(os.environ.get("SENTINEL_WEBHOOK_PORT", "8090"))
    try:
        _server = ThreadingHTTPServer((host, port), AlertmanagerHandler)
    except OSError as exc:
        LOGGER.error("Could not start Alertmanager receiver on %s:%s: %s", host, port, exc)
        return
    threading.Thread(target=_server.serve_forever, daemon=True, name="sentinel-alertmanager-http").start()
    LOGGER.info("Alertmanager webhook listening on %s:%s", host, port)
