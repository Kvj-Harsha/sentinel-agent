"""GitHub REST client for the Sentinel demo repair PR and human approval flow."""

import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

import requests

LOGGER = logging.getLogger(__name__)
API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
REQUEST_TIMEOUT_SECONDS = 15
CONFIG_PATH = "app/settings.json"
SAFE_SETTINGS = {"demo_faults_enabled": False, "checkout_error_rate": 0.0}
DEFAULT_LOCAL_SETTINGS = Path(__file__).resolve().parents[3].parent / "checkout-demo-app" / CONFIG_PATH
ISSUE_MARKER = "<!-- sentinel-ticket:{ticket_number} -->"
CLOSING_ISSUE_PATTERN = re.compile(r"(?im)^\s*(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+#(\d+)\b")


class GithubPullRequestError(RuntimeError):
    """Raised when GitHub rejects a demo pull-request operation."""


def is_configured() -> bool:
    """:return: True when the demo repo, owner and token are configured."""
    return all(os.environ.get(key) for key in ("SENTINEL_GITHUB_TOKEN", "SENTINEL_DEMO_GITHUB_OWNER", "SENTINEL_DEMO_GITHUB_REPO"))


def repository_name() -> str:
    """:return: the configured demo repository in owner/name form, if available."""
    owner = os.environ.get("SENTINEL_DEMO_GITHUB_OWNER", "").strip()
    repo = os.environ.get("SENTINEL_DEMO_GITHUB_REPO", "").strip()
    return f"{owner}/{repo}" if owner and repo else "Not configured"


def _session() -> requests.Session:
    """:return: an authenticated GitHub session without exposing its token in logs."""
    if not is_configured():
        raise GithubPullRequestError(
            "Set SENTINEL_GITHUB_TOKEN, SENTINEL_DEMO_GITHUB_OWNER and SENTINEL_DEMO_GITHUB_REPO in .env."
        )
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['SENTINEL_GITHUB_TOKEN']}",
            "X-GitHub-Api-Version": API_VERSION,
        }
    )
    return session


def _request(session: requests.Session, method: str, path: str, **kwargs: Any) -> requests.Response:
    owner = os.environ["SENTINEL_DEMO_GITHUB_OWNER"]
    repo = os.environ["SENTINEL_DEMO_GITHUB_REPO"]
    try:
        response = session.request(
            method, f"{API_ROOT}/repos/{owner}/{repo}{path}", timeout=REQUEST_TIMEOUT_SECONDS, **kwargs
        )
    except requests.RequestException as exc:
        raise GithubPullRequestError(f"GitHub is unreachable: {exc}") from exc
    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("message", "")
        except ValueError:
            detail = response.text[:160]
        raise GithubPullRequestError(f"GitHub returned HTTP {response.status_code}: {detail}")
    return response


def create_repair_pr(ticket_number: str) -> Dict[str, Any]:
    """Create an incident issue, then a linked PR with the allow-listed safe settings change."""
    session = _session()
    repo_data = _request(session, "GET", "").json()
    base = repo_data["default_branch"]
    branch = f"sentinel/disable-demo-fault-{ticket_number.lower()}"
    existing_prs = _request(
        session,
        "GET",
        "/pulls",
        params={"state": "all", "head": f"{os.environ['SENTINEL_DEMO_GITHUB_OWNER']}:{branch}"},
    ).json()

    issue = _find_or_create_incident_issue(session, ticket_number)
    if existing_prs:
        pr = _link_issue_to_pr(session, existing_prs[0], issue)
        return _attach_issue_details(pr, issue)

    base_ref = _request(session, "GET", f"/git/ref/heads/{base}").json()
    _request(session, "POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_ref["object"]["sha"]})
    original = _request(session, "GET", f"/contents/{CONFIG_PATH}", params={"ref": base}).json()
    safe_content = json.dumps(SAFE_SETTINGS, indent=2) + "\n"
    _request(
        session,
        "PUT",
        f"/contents/{CONFIG_PATH}",
        json={
            "message": f"fix: disable demo checkout fault for {ticket_number}",
            "content": base64.b64encode(safe_content.encode("utf-8")).decode("ascii"),
            "sha": original["sha"],
            "branch": branch,
        },
    )
    pr = _request(
        session,
        "POST",
        "/pulls",
        json={
            "title": f"[{ticket_number}] Disable checkout fault injection",
            "head": branch,
            "base": base,
            "body": (
                "## Sentinel proposed repair\n\n"
                "Disable the local demo fault switch and set checkout error rate to zero.\n\n"
                "This PR is for the isolated localhost demo app only. Review the diff and GitHub Actions checks "
                f"in Sentinel before approving and merging.\n\nCloses #{issue['number']}"
            ),
        },
    ).json()
    return _attach_issue_details(pr, issue)


def _find_or_create_incident_issue(session: requests.Session, ticket_number: str) -> Dict[str, Any]:
    """Find the issue for this incident or create one before opening its repair PR."""
    marker = ISSUE_MARKER.format(ticket_number=ticket_number)
    issues = _request(session, "GET", "/issues", params={"state": "all", "per_page": 100}).json()
    for issue in issues:
        if not issue.get("pull_request") and marker in (issue.get("body") or ""):
            return issue
    return _request(
        session,
        "POST",
        "/issues",
        json={
            "title": f"[{ticket_number}] Checkout service incident",
            "body": (
                f"Sentinel recorded a checkout-service incident as ServiceNow ticket {ticket_number}.\n\n"
                "Proposed remediation: disable the localhost demo fault switch and set the checkout error rate "
                "to zero. The linked repair pull request will close this issue only after a human reviews and "
                f"merges it.\n\n{marker}"
            ),
        },
    ).json()


def _link_issue_to_pr(
    session: requests.Session, pull_request: Dict[str, Any], issue: Dict[str, Any]
) -> Dict[str, Any]:
    """Add GitHub's closing keyword to an existing PR unless it already links this issue."""
    issue_number = int(issue["number"])
    body = pull_request.get("body") or ""
    linked_numbers = {int(match) for match in CLOSING_ISSUE_PATTERN.findall(body)}
    if issue_number in linked_numbers:
        return pull_request
    updated_body = f"{body.rstrip()}\n\nCloses #{issue_number}".strip()
    return _request(
        session,
        "PATCH",
        f"/pulls/{pull_request['number']}",
        json={"body": updated_body},
    ).json()


def _attach_issue_details(pull_request: Dict[str, Any], issue: Dict[str, Any]) -> Dict[str, Any]:
    """Include issue metadata for the Streamlit review panel without altering GitHub's response."""
    result = dict(pull_request)
    result["sentinel_issue_number"] = issue.get("number")
    result["sentinel_issue_url"] = issue.get("html_url")
    result["sentinel_issue_state"] = issue.get("state")
    return result


def linked_issue_number(pull_request: Dict[str, Any]) -> Optional[int]:
    """Extract the issue GitHub will close when this PR is merged, if one is linked."""
    body = pull_request.get("body") or ""
    matches = CLOSING_ISSUE_PATTERN.findall(body)
    return int(matches[-1]) if matches else None


def get_issue(number: int) -> Dict[str, Any]:
    """Fetch a linked GitHub issue so Sentinel can show its current open/closed state."""
    session = _session()
    return _request(session, "GET", f"/issues/{number}").json()


def get_pull_request(number: int) -> Dict[str, Any]:
    """:return: current PR metadata."""
    session = _session()
    return _request(session, "GET", f"/pulls/{number}").json()


def get_pull_request_diff(number: int) -> str:
    """:return: the real GitHub unified diff for display in Streamlit."""
    session = _session()
    response = _request(
        session,
        "GET",
        f"/pulls/{number}",
        headers={"Accept": "application/vnd.github.v3.diff"},
    )
    return response.text


def get_check_runs(commit_sha: str) -> List[Dict[str, Any]]:
    """:return: GitHub Actions/check-run results for the PR's current commit."""
    session = _session()
    return _request(session, "GET", f"/commits/{commit_sha}/check-runs").json().get("check_runs", [])


def checks_passed(checks: List[Dict[str, Any]]) -> bool:
    """:return: True only when at least one check exists and every check has succeeded."""
    return bool(checks) and all(check.get("status") == "completed" and check.get("conclusion") == "success" for check in checks)


def merge_pull_request(number: int, expected_sha: str) -> Dict[str, Any]:
    """Merge only after a human approves in Sentinel and all current checks pass."""
    session = _session()
    pr = get_pull_request(number)
    checks = get_check_runs(pr["head"]["sha"])
    if not checks_passed(checks):
        raise GithubPullRequestError("The PR cannot be merged until all GitHub checks have passed.")
    return _request(
        session,
        "PUT",
        f"/pulls/{number}/merge",
        json={"sha": expected_sha, "merge_method": "squash"},
    ).json()


def sync_merged_settings() -> Path:
    """Fetch the merged allow-listed config and apply it to the local demo app."""
    session = _session()
    repo_data = _request(session, "GET", "").json()
    encoded = _request(
        session,
        "GET",
        f"/contents/{CONFIG_PATH}",
        params={"ref": repo_data["default_branch"]},
    ).json()
    try:
        content = base64.b64decode(encoded["content"]).decode("utf-8")
        settings = json.loads(content)
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise GithubPullRequestError("Merged app settings are not valid JSON.") from exc
    if settings != SAFE_SETTINGS:
        raise GithubPullRequestError("Merged settings do not match Sentinel's reviewed safe repair.")
    local_path = Path(os.environ.get("SENTINEL_DEMO_APP_CONFIG_PATH", str(DEFAULT_LOCAL_SETTINGS)))
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return local_path
