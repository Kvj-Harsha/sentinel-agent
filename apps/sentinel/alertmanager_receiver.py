"""Token-protected Prometheus Alertmanager webhook for Sentinel's local demo stack."""

import hmac
import atexit
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
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from urllib.parse import urlparse

from alerts import compose_alertmanager_alert
from alerts import compose_incident_report
from run import format_verified_facts
from run import invoke_agent
from coded_tools.sentinel.ticket_store import TicketStore
from coded_tools.sentinel.schemas import redact_text
from coded_tools.sentinel.sqlite_store import SQLiteDatabase

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 1_048_576
MAX_ALERTS_PER_REQUEST = 5
FINGERPRINT_TTL_SECONDS = 3600
MAX_ACTIVE_JOBS = 4
WORKER_SHUTDOWN_TIMEOUT_SECONDS = 30
_lock = threading.Lock()
_job_slots = threading.BoundedSemaphore(MAX_ACTIVE_JOBS)
_jobs: List[Dict[str, Any]] = []
_fingerprints: Dict[str, float] = {}
_worker_threads: set[threading.Thread] = set()
_server: ThreadingHTTPServer = None
_server_thread: threading.Thread = None


def _utc_now() -> str:
    """:return: current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_PERSISTED_FIELDS = (
    "id", "received_at", "updated_at", "state", "alert_name", "service", "severity", "fingerprint",
    "ticket_number", "response", "verified_markdown", "error",
)


def _persist(job: Dict[str, Any]) -> None:
    """Share the job through the SQLite database: the UI and the webhook may run in separate containers."""
    if os.environ.get("SENTINEL_STORAGE_BACKEND", "sqlite").lower() != "sqlite":
        return
    try:
        SQLiteDatabase().save_alert_job({key: job.get(key) for key in _PERSISTED_FIELDS})
    except Exception:  # pylint: disable=broad-exception-caught
        LOGGER.warning("Could not persist alert job %s", job.get("id"), exc_info=True)


def snapshot_jobs() -> List[Dict[str, Any]]:
    """Returns safe copies of recent webhook jobs (this process plus those persisted by another process)."""
    with _lock:
        jobs = list(reversed(_jobs[-20:]))
    if os.environ.get("SENTINEL_STORAGE_BACKEND", "sqlite").lower() == "sqlite":
        try:
            known = {job["id"] for job in jobs}
            jobs += [job for job in SQLiteDatabase().recent_alert_jobs() if job["id"] not in known]
            jobs.sort(key=lambda job: job.get("updated_at") or "", reverse=True)
        except Exception:  # pylint: disable=broad-exception-caught
            LOGGER.warning("Could not read persisted alert jobs", exc_info=True)
    for job in jobs:
        if job.get("ticket_number") or job.get("state") not in ("queued", "investigating"):
            continue
        fingerprint = job.get("fingerprint")
        if fingerprint:
            ticket = TicketStore().find_by_fingerprint(fingerprint)
            if ticket:
                job["ticket_number"] = ticket.get("number")
                job["ticket_detected_at"] = _utc_now()
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
        _persist(job)
        report = compose_incident_report(job["alert"])
        job["report"] = report
        response = invoke_agent(
            report,
            sly_data={
                "alert_fingerprint": job.get("fingerprint"),
                "incident_session_id": job.get("id"),
            },
        )
        sly_data = response.get("sly_data") or {}
        job["response"] = response.get("text", "")
        job["verified_facts"] = sly_data.get("sentinel_verified") or {}
        job["verified_markdown"] = format_verified_facts(sly_data)
        job["ticket_number"] = sly_data.get("sentinel_ticket_number")
        job["state"] = "completed"
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        LOGGER.exception("Alertmanager job %s failed", job["id"])
        job["error"] = redact_text(str(exc))
        job["state"] = "failed"
    except Exception as exc:  # pylint: disable=broad-exception-caught
        LOGGER.exception("Unexpected Alertmanager job failure: %s", job["id"])
        job["error"] = redact_text(f"Unexpected investigation error: {exc}")
        job["state"] = "failed"
    finally:
        job["updated_at"] = _utc_now()
        _persist(job)
        with _lock:
            # The in-memory guard only covers retries while a run is in flight; afterwards the open-ticket check decides.
            _fingerprints.pop(str(job.get("fingerprint") or ""), None)
            _worker_threads.discard(threading.current_thread())
        _job_slots.release()


def _queue_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    """Queues a normalized alert and suppresses Alertmanager retry duplicates."""
    fingerprint = str(alert.get("fingerprint") or "")
    if fingerprint:
        existing_ticket = TicketStore().find_by_fingerprint(fingerprint)
        if existing_ticket:
            return {"status": "duplicate", "fingerprint": fingerprint, "ticket_number": existing_ticket.get("number")}
    ticket_count_before = len(TicketStore().load())
    now = time.monotonic()
    with _lock:
        expired = [key for key, seen_at in _fingerprints.items() if now - seen_at > FINGERPRINT_TTL_SECONDS]
        for key in expired:
            _fingerprints.pop(key, None)
        if fingerprint and fingerprint in _fingerprints:
            return {"status": "duplicate", "fingerprint": fingerprint}
        if not _job_slots.acquire(blocking=False):
            return {"status": "busy", "fingerprint": fingerprint}

        job = {
            "id": uuid.uuid4().hex[:12],
            "received_at": _utc_now(),
            "updated_at": _utc_now(),
            "state": "queued",
            "ticket_count_before": ticket_count_before,
            "alert_name": alert["title"],
            "service": alert["tags"][0].split(":", 1)[1],
            "fingerprint": fingerprint,
            "severity": (alert.get("body") or "").split(".", 1)[0].replace("Severity: ", ""),
            "alert": alert,
        }
        _jobs.append(job)
        del _jobs[:-50]
        _persist(job)
        if fingerprint:
            _fingerprints[fingerprint] = now

        worker = threading.Thread(target=_process_job, args=(job,), daemon=True, name=f"sentinel-alert-{job['id']}")
        _worker_threads.add(worker)
        try:
            worker.start()
        except RuntimeError:
            _worker_threads.discard(worker)
            if fingerprint:
                _fingerprints.pop(fingerprint, None)
            _jobs.remove(job)
            _job_slots.release()
            LOGGER.exception("Could not start alert investigation worker")
            return {"status": "busy", "fingerprint": fingerprint}
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

        expected_token = _webhook_token()
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
            is_busy = any(item.get("status") == "busy" for item in accepted)
            if is_busy:
                self.send_response(503)
                self.send_header("Retry-After", "5")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                body = _json_bytes({"accepted": accepted, "ignored": ignored, "receiver": "sentinel", "error": "investigation capacity reached; retry later"})
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send(202, {"accepted": accepted, "ignored": ignored, "receiver": "sentinel"})
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError, ValueError) as exc:
            self._send(400, {"error": str(exc)})

    def log_message(self, format_string: str, *args: Any) -> None:
        """Logs concise request metadata without logging headers or alert payloads."""
        LOGGER.info("Alertmanager webhook: %s", format_string % args)


def start_receiver() -> None:
    """Starts the webhook server once per Streamlit process; requires a configured shared token."""
    global _server
    global _server_thread
    if _server is not None:
        return
    token = _webhook_token()
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
    _server_thread = threading.Thread(target=_server.serve_forever, daemon=True, name="sentinel-alertmanager-http")
    _server_thread.start()
    atexit.register(stop_receiver)
    LOGGER.info("Alertmanager webhook listening on %s:%s", host, port)


def _webhook_token() -> str:
    """Read the bearer token from a mounted secret file, with env support for local development."""
    secret_file = os.environ.get("SENTINEL_WEBHOOK_TOKEN_FILE", "").strip()
    if secret_file:
        try:
            return Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError:
            LOGGER.error("Webhook secret file is unavailable; receiver remains disabled.")
            return ""
    return os.environ.get("SENTINEL_WEBHOOK_TOKEN", "")


def stop_receiver() -> None:
    """Stop accepting requests, close the socket, and give active investigations time to finish."""
    global _server
    global _server_thread
    server = _server
    receiver_thread = _server_thread
    if server is None:
        return
    server.shutdown()
    server.server_close()
    if receiver_thread and receiver_thread is not threading.current_thread():
        receiver_thread.join(timeout=5)
    with _lock:
        workers = list(_worker_threads)
    for worker in workers:
        if worker is not threading.current_thread():
            worker.join(timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS)
    remaining = sum(worker.is_alive() for worker in workers)
    if remaining:
        LOGGER.warning("Receiver stopped with %d investigation(s) still running after the shutdown grace period.", remaining)
    _server = None
    _server_thread = None


def receiver_status() -> Dict[str, Any]:
    """Returns safe listener health for the UI; never returns the bearer token or payload."""
    if _server is None or _server_thread is None or not _server_thread.is_alive():
        return {"status": "disabled", "host": None, "port": None, "active_jobs": 0, "capacity": MAX_ACTIVE_JOBS}
    host, port = _server.server_address[:2]
    with _lock:
        active_jobs = len(_worker_threads)
    return {"status": "listening", "host": host, "port": port,
            "active_jobs": active_jobs, "capacity": MAX_ACTIVE_JOBS}
