"""Read-only, bounded client for Grafana Loki's HTTP query API."""

import base64
import json
import os
import ssl
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.request import Request
from urllib.request import urlopen


class LokiError(RuntimeError):
    """A safe Loki connection, HTTP, or response error."""


class LokiClient:
    """Query one Loki endpoint with optional auth and strict response limits."""

    MAX_RESPONSE_BYTES = 1_000_000

    def __init__(self) -> None:
        self.base_url = os.environ.get("LOKI_URL", "http://127.0.0.1:3100").rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise LokiError("LOKI_URL must be an HTTP(S) URL without embedded credentials, query, or fragment.")
        self.timeout = _bounded_int("LOKI_TIMEOUT_SECONDS", 5, 1, 20)
        self.verify_tls = os.environ.get("LOKI_VERIFY_TLS", "true").strip().lower() not in {"0", "false", "no"}
        self.bearer_token = os.environ.get("LOKI_BEARER_TOKEN", "").strip()
        self.username = os.environ.get("LOKI_USERNAME", "")
        self.password = os.environ.get("LOKI_PASSWORD", "")
        self.tenant_id = os.environ.get("LOKI_TENANT_ID", "").strip()

    def query_range(self, logql: str, start_ns: int, end_ns: int, limit: int = 50) -> list[dict[str, Any]]:
        """Return bounded log streams from the Loki query_range endpoint."""
        params = {
            "query": logql,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(min(50, max(1, limit))),
            "direction": "forward",
        }
        request = Request(
            f"{self.base_url}/loki/api/v1/query_range?{urlencode(params)}",
            headers=self._headers(),
            method="GET",
        )
        context = None if self.verify_tls else ssl._create_unverified_context()
        try:
            with urlopen(request, timeout=self.timeout, context=context) as response:
                length = response.headers.get("Content-Length")
                try:
                    if length and int(length) > self.MAX_RESPONSE_BYTES:
                        raise LokiError("Loki response exceeded the 1 MB limit.")
                except ValueError:
                    raise LokiError("Loki returned an invalid response length.") from None
                body = response.read(self.MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise LokiError(f"Loki returned HTTP {exc.code}.") from None
        except (URLError, TimeoutError, OSError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise LokiError("Loki request timed out.") from None
            raise LokiError("Loki is unreachable or the connection failed.") from None
        if len(body) > self.MAX_RESPONSE_BYTES:
            raise LokiError("Loki response exceeded the 1 MB limit.")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LokiError("Loki returned malformed JSON.") from None
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise LokiError("Loki returned an unsuccessful or malformed response.")
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("resultType") != "streams" or not isinstance(data.get("result"), list):
            raise LokiError("Loki returned an unexpected query response shape.")
        return data["result"]

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.tenant_id:
            headers["X-Scope-OrgID"] = self.tenant_id
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        elif self.username or self.password:
            encoded = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        return headers


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))
