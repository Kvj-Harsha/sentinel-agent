"""Coded tool that looks up the real, live commit history of this project's own GitHub repo."""

import json
import logging
import os
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Union

import requests
from neuro_san.interfaces.coded_tool import CodedTool

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent / "data" / "github_deploys_cache.json"
DEFAULT_OWNER = "YOUR_GITHUB_USERNAME"
DEFAULT_REPO = "sentinel-agent"
DEMO_SERVICE_NAME = "sentinel-agent"
REQUEST_TIMEOUT_SECONDS = 5
PER_PAGE = 5


class GithubDeployLookup(CodedTool):
    """
    Looks up the real, live commit history of this project's own public GitHub repo via the
    unauthenticated GitHub REST API, and reports it in the same shape deploy_lookup.py's
    synthetic matches use (deployed_at, version, author, change_summary, url) -- treating
    each commit as a stand-in "deploy" for this one designated demo service
    (DEMO_SERVICE_NAME). Unlike every other tool in this project, this one calls a real,
    independently-verifiable external source instead of a local synthetic fixture, which is
    the point: a judge can click the returned url and see the actual commit.

    Falls back to a pre-captured local snapshot (backend/coded_tools/sentinel/data/github_deploys_cache.json)
    on any network error, timeout, or non-200 response, so a live demo never breaks on
    connectivity or GitHub's unauthenticated rate limit (60 requests/hour).
    """

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        :param args: a dictionary with the following keys:
            - service_name: the service to look up. Only DEMO_SERVICE_NAME
              ("sentinel-agent") is backed by this tool; use deploy_lookup for every
              other service.

        :param sly_data: unused.

        :return: a dict with matching "deploy" records sorted most-recent-first
            (sourced live from GitHub, or from the offline cache on failure), or an
            error string.
        """
        service_name: str = (args.get("service_name") or "").strip().lower()
        if not service_name:
            return "Error: Please provide a service_name to look up commit/deploy history for."
        if service_name != DEMO_SERVICE_NAME:
            return (
                f"Error: github_deploy_lookup only has data for '{DEMO_SERVICE_NAME}'. "
                "Use deploy_lookup for every other service."
            )

        matches = self._fetch_live()
        if matches is None:
            matches = self._load_cache()

        logger.debug("GithubDeployLookup returning %d commit(s) for %r", len(matches), service_name)
        if not matches:
            return {"matches": [], "summary": f"No commit history available for '{service_name}'."}

        return {
            "matches": matches,
            "summary": f"Found {len(matches)} recent commit(s) on GitHub, most recent first.",
        }

    @staticmethod
    def _fetch_live() -> Union[List[Dict[str, Any]], None]:
        """:return: live commit data mapped to the deploy shape, or None on any failure."""
        owner = os.environ.get("GITHUB_REPO_OWNER", DEFAULT_OWNER)
        repo = os.environ.get("GITHUB_REPO_NAME", DEFAULT_REPO)
        url = f"https://api.github.com/repos/{owner}/{repo}/commits"
        try:
            response = requests.get(url, params={"per_page": PER_PAGE}, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.warning("GithubDeployLookup live call failed, falling back to cache: %s", exc)
            return None

        commits = response.json()
        return [
            {
                "service": DEMO_SERVICE_NAME,
                "deployed_at": commit["commit"]["author"]["date"],
                "version": commit["sha"][:7],
                "author": commit["commit"]["author"]["name"],
                "change_summary": commit["commit"]["message"].splitlines()[0],
                "url": commit["html_url"],
            }
            for commit in commits
        ]

    @staticmethod
    def _load_cache() -> List[Dict[str, Any]]:
        """:return: the offline cached snapshot, or an empty list if it's unreadable."""
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning("GithubDeployLookup could not read fallback cache: %s", exc)
            return []

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method; the live call has its own short timeout."""
        return self.invoke(args, sly_data)
