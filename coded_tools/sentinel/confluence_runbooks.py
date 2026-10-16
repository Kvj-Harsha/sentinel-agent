"""Read-only, space-scoped Confluence Cloud runbook search."""

from __future__ import annotations

from html.parser import HTMLParser
import os
import re
from typing import Any
from typing import Dict
from typing import List
from urllib.parse import quote

import requests


class _TextExtractor(HTMLParser):
    """Convert Confluence storage-format HTML into searchable plain text."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "tr"}:
            self.parts.append("\n")


class ConfluenceRunbooks:
    """Read pages only from explicitly configured Confluence spaces."""

    def __init__(self) -> None:
        self.base_url = os.getenv("CONFLUENCE_BASE_URL", "").rstrip("/").removesuffix("/wiki")
        self.cloud_id = os.getenv("CONFLUENCE_CLOUD_ID", "").strip()
        self.email = os.getenv("CONFLUENCE_EMAIL", "").strip()
        self.token = os.getenv("CONFLUENCE_API_TOKEN", "").strip()
        self.space_keys = [key.strip() for key in os.getenv("CONFLUENCE_SPACE_KEYS", "").split(",") if key.strip()]
        self.label = os.getenv("CONFLUENCE_RUNBOOK_LABEL", "").strip()
        self.timeout = max(1, min(int(os.getenv("CONFLUENCE_TIMEOUT_SECONDS", "8")), 30))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.email and self.token and self.space_keys)

    @property
    def requested(self) -> bool:
        return bool(self.base_url or self.email or self.token or self.space_keys)

    def search(self, query: str) -> List[Dict[str, Any]]:
        if not self.configured:
            raise RuntimeError("Confluence needs a site URL, account email, API token, and explicit space key(s).")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::\d+)?(?:/wiki)?", self.base_url):
            raise RuntimeError("CONFLUENCE_BASE_URL must be an HTTPS Confluence site URL.")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", key) for key in self.space_keys):
            raise RuntimeError("CONFLUENCE_SPACE_KEYS may contain only letters, numbers, underscores, and hyphens.")

        api_root = (
            f"https://api.atlassian.com/ex/confluence/{quote(self.cloud_id, safe='')}/wiki/rest/api"
            if self.cloud_id
            else f"{self.base_url}/wiki/rest/api"
        )

        terms = list(dict.fromkeys(re.findall(r"[A-Za-z0-9]+", query.lower())))[:12]
        if not terms:
            return []
        cql = "type = page AND space in (" + ",".join(f'"{key}"' for key in self.space_keys) + ")"
        if self.label:
            safe_label = re.sub(r"[^A-Za-z0-9_-]", "", self.label)
            if not safe_label:
                raise RuntimeError("CONFLUENCE_RUNBOOK_LABEL is invalid.")
            cql += f' AND label = "{safe_label}"'
        cql += " AND (" + " OR ".join(f'text ~ "{term}"' for term in terms) + ")"

        pages: List[Dict[str, Any]] = []
        start = 0
        session = requests.Session()
        session.auth = (self.email, self.token)
        session.headers.update({"Accept": "application/json"})
        while start < 100:
            response = session.get(
                f"{api_root}/content/search",
                params={
                    "cql": cql,
                    "expand": "space,version",
                    "limit": min(50, 100 - start),
                    "start": start,
                },
                timeout=self.timeout,
            )
            if response.status_code in (401, 403):
                raise RuntimeError(f"Confluence denied access (HTTP {response.status_code}); check the account and space permissions.")
            response.raise_for_status()
            payload = response.json()
            batch = payload.get("results", [])
            pages.extend(batch)
            if not batch or len(batch) < 50:
                break
            start += len(batch)
        return [self._normalize(page) for page in pages]

    def get_page(self, page_id: str) -> Dict[str, Any]:
        """Fetch the full body only for the selected candidate, with a strict size cap."""
        api_root = (
            f"https://api.atlassian.com/ex/confluence/{quote(self.cloud_id, safe='')}/wiki/rest/api"
            if self.cloud_id
            else f"{self.base_url}/wiki/rest/api"
        )
        response = requests.get(
            f"{api_root}/content/{quote(page_id)}",
            params={"expand": "body.storage,space,version"},
            auth=(self.email, self.token),
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if response.status_code in (401, 403):
            raise RuntimeError(f"Confluence denied page access (HTTP {response.status_code}).")
        response.raise_for_status()
        return self._normalize(response.json(), max_body_chars=20000)

    def _normalize(self, page: Dict[str, Any], max_body_chars: int = 2000) -> Dict[str, Any]:
        body_html = (page.get("body") or {}).get("storage", {}).get("value", "") or page.get("excerpt", "")
        parser = _TextExtractor()
        parser.feed(body_html)
        body_text = " ".join(parser.parts)
        body_text = re.sub(r" *\n *", "\n", body_text)
        body_text = re.sub(r"\n{2,}", "\n", body_text).strip()[:max_body_chars]
        title = page.get("title", "Untitled runbook")
        page_id = str(page.get("id", ""))
        space_key = ((page.get("space") or {}).get("key") or "")
        page_url = page.get("_links", {}).get("webui")
        if page_url:
            if page_url.startswith("/spaces/"):
                page_url = "/wiki" + page_url
            source_url = f"{self.base_url}{page_url}"
        else:
            source_url = f"{self.base_url}/wiki/spaces/{quote(space_key)}/pages/{quote(page_id)}"
        combined = f"{title}. {body_text}"
        action_match = re.search(
            r"(?:recommended action|recommended response|mitigation|remediation)\s*[:\-]?\s*(.{1,2000}?)(?=\n\s*(?:\d+[.)]\s*)?(?:root cause|verification|validation|rollback|recovery|checks|diagnosis|escalation|references|notes|troubleshooting|monitoring)\b|$)",
            body_text,
            re.IGNORECASE | re.DOTALL,
        )
        runbook_id = re.search(r"\b(?:RB-\d+|RUNBOOK-[A-Z0-9]+-\d+)\b", title + " " + body_text, re.IGNORECASE)
        version = (page.get("version") or {}).get("when")
        return {
            "id": runbook_id.group(0).upper() if runbook_id else page_id,
            "title": title,
            "trigger_keywords": [],
            "root_cause": "",
            "recommended_action": action_match.group(1).strip(" .:-") if action_match else "See the linked Confluence runbook for the recommended response.",
            "page_id": page_id,
            "space": {"key": space_key},
            "version": {"when": version},
            "webui_link": source_url,
            "search_text": combined.lower(),
        }
