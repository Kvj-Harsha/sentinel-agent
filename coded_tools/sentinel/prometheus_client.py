"""Small read-only Prometheus HTTP API client with bounded responses."""

import base64
import json
import os
import ssl
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen


class PrometheusError(RuntimeError):
    """A safe, user-displayable Prometheus request or response error."""


class PrometheusClient:
    """Read-only range-query client. Credentials and TLS policy come from environment."""

    MAX_RESPONSE_BYTES = 1_000_000

    def __init__(self) -> None:
        self.base_url = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/")
        parsed_url = urlsplit(self.base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise PrometheusError("PROMETHEUS_URL must be an HTTP(S) URL without embedded credentials, query, or fragment.")
        if parsed_url.path not in {"", "/"}:
            self.base_url = self.base_url.rstrip("/")
        self.timeout = _bounded_int("PROMETHEUS_TIMEOUT_SECONDS", 5, minimum=1, maximum=20)
        self.verify_tls = os.environ.get("PROMETHEUS_VERIFY_TLS", "true").strip().lower() not in {"0", "false", "no"}
        self.bearer_token = os.environ.get("PROMETHEUS_BEARER_TOKEN", "").strip()
        self.username = os.environ.get("PROMETHEUS_USERNAME", "")
        self.password = os.environ.get("PROMETHEUS_PASSWORD", "")

    def query_range(self, promql: str, start: str, end: str, step: str = "15s") -> dict[str, Any]:
        """Run one bounded range query and return the decoded Prometheus response."""
        url = f"{self.base_url}/api/v1/query_range?{urlencode({'query': promql, 'start': start, 'end': end, 'step': step})}"
        headers = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        elif self.username or self.password:
            token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        request = Request(url, headers=headers, method="GET")
        context = None if self.verify_tls else ssl._create_unverified_context()
        try:
            with urlopen(request, timeout=self.timeout, context=context) as response:
                content_length = response.headers.get("Content-Length")
                try:
                    if content_length and int(content_length) > self.MAX_RESPONSE_BYTES:
                        raise PrometheusError("Prometheus response exceeded the 1 MB limit.")
                except ValueError:
                    raise PrometheusError("Prometheus returned an invalid response length.") from None
                body = response.read(self.MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise PrometheusError(f"Prometheus returned HTTP {exc.code}.") from None
        except (URLError, TimeoutError, OSError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise PrometheusError("Prometheus request timed out.") from None
            raise PrometheusError("Prometheus is unreachable or the connection failed.") from None
        if len(body) > self.MAX_RESPONSE_BYTES:
            raise PrometheusError("Prometheus response exceeded the 1 MB limit.")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PrometheusError("Prometheus returned malformed JSON.") from None
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise PrometheusError("Prometheus returned an unsuccessful or malformed response.")
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("resultType") != "matrix" or not isinstance(data.get("result"), list):
            raise PrometheusError("Prometheus returned an unexpected range-query shape.")
        return data


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))
