"""GitHub REST client for the Sentinel demo repair PR and human approval flow."""

import base64
import hashlib
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
from coded_tools.sentinel.schemas import redact_text

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
    except requests.RequestException:
        raise GithubPullRequestError("GitHub is unreachable or the request failed.") from None
    if response.status_code >= 400:
        raise GithubPullRequestError(f"GitHub returned HTTP {response.status_code}; check the repository and token permissions.")
    return response


def validate_repair_context(ticket_number: str, context: Dict[str, Any], ticket_session_id: str) -> None:
    """Reject PR operations unless the stored, tool-verified incident binding is complete."""
    if not re.fullmatch(r"INC\d+", str(ticket_number)):
        raise GithubPullRequestError("Repair PR is disabled: the local incident number is invalid.")
    refs = context.get("evidence_refs") or []
    evidence_types = {ref.get("evidence_type") for ref in refs if ref.get("source_status") in {"live", "fixture"}}
    if (
        context.get("eligible") is not True or context.get("all_passed") is not True
        or context.get("ticket_number") != ticket_number
        or context.get("session_id") != ticket_session_id
        or context.get("service_name") != "checkout-service"
        or context.get("verdict") != "SUPPORTED" or not context.get("session_id")
        or "log" not in evidence_types or not evidence_types.intersection({"deployment", "deployment_change"})
    ):
        raise GithubPullRequestError(
            "Repair PR is disabled: this ticket has no matching SUPPORTED correlation, evidence, and passing preflight binding."
        )


def _binding_marker(ticket_number: str, context: Dict[str, Any]) -> str:
    """Stable marker binds the PR description to the persisted incident/evidence context."""
    payload = {
        "ticket_number": ticket_number,
        "session_id": context.get("session_id"),
        "service_name": context.get("service_name"),
        "verdict": context.get("verdict"),
        "runbook_id": context.get("runbook_id"),
        "evidence_refs": context.get("evidence_refs") or [],
        "all_passed": bool(context.get("all_passed")),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
    return f"<!-- sentinel-binding:{digest} -->"


def create_repair_pr(ticket_number: str, repair_context: Dict[str, Any], ticket_session_id: str) -> Dict[str, Any]:
    """Create an incident issue, then a linked PR with the allow-listed safe settings change."""
    validate_repair_context(ticket_number, repair_context, ticket_session_id)
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
        pr = _link_issue_to_pr(session, existing_prs[0], issue, ticket_number, repair_context)
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
    evidence_lines = "\n".join(
        f"- `{ref['evidence_type']}` {ref['source']} · {ref['record_id']} ({ref['source_status']})"
        for ref in repair_context["evidence_refs"]
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
                f"in Sentinel before approving and merging.\n\nIncident binding: ticket {ticket_number}; "
                f"service {repair_context['service_name']}; correlation {repair_context['verdict']}; "
                f"runbook {repair_context.get('runbook_id') or 'unavailable'}.\n\nEvidence references:\n{evidence_lines}\n\n"
                f"{_binding_marker(ticket_number, repair_context)}\n\nCloses #{issue['number']}"
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
    session: requests.Session, pull_request: Dict[str, Any], issue: Dict[str, Any],
    ticket_number: str, repair_context: Dict[str, Any],
) -> Dict[str, Any]:
    """Add the incident link and immutable-context marker to a reused PR."""
    issue_number = int(issue["number"])
    body = pull_request.get("body") or ""
    linked_numbers = {int(match) for match in CLOSING_ISSUE_PATTERN.findall(body)}
    binding = _binding_marker(ticket_number, repair_context)
    if issue_number in linked_numbers and binding in body:
        return pull_request
    body_without_old_binding = re.sub(r"(?m)^<!-- sentinel-binding:[0-9a-f]+ -->\s*$", "", body)
    evidence_lines = "\n".join(
        f"- `{ref['evidence_type']}` {ref['source']} · {ref['record_id']} ({ref['source_status']})"
        for ref in repair_context["evidence_refs"]
    )
    issue_link = "" if issue_number in linked_numbers else f"\n\nCloses #{issue_number}"
    updated_body = (
        f"{body_without_old_binding.rstrip()}\n\nIncident binding: ticket {ticket_number}; "
        f"service {repair_context['service_name']}; correlation {repair_context['verdict']}; "
        f"runbook {repair_context.get('runbook_id') or 'unavailable'}.\n\nEvidence references:\n{evidence_lines}\n\n"
        f"{binding}{issue_link}"
    ).strip()
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
    return redact_text(response.text)


def get_pull_request_files(number: int) -> List[Dict[str, Any]]:
    """Return changed-file metadata for the requested pull request."""
    session = _session()
    return _request(session, "GET", f"/pulls/{number}/files", params={"per_page": 100}).json()


def _settings_at_ref(ref: str) -> Dict[str, Any]:
    session = _session()
    encoded = _request(session, "GET", f"/contents/{CONFIG_PATH}", params={"ref": ref}).json()
    try:
        return json.loads(base64.b64decode(encoded["content"]).decode("utf-8"))
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise GithubPullRequestError("Repair settings are not valid JSON at the reviewed commit.") from exc


def verify_repair_pr(
    pr: Dict[str, Any], files: List[Dict[str, Any]], ticket_number: str,
    repair_context: Dict[str, Any], ticket_session_id: str, *, require_open: bool = False,
) -> None:
    """Verify repo, incident binding, exact changed path, and exact safe settings at the PR head."""
    validate_repair_context(ticket_number, repair_context, ticket_session_id)
    expected_repo = repository_name().casefold()
    base_repo = (pr.get("base", {}).get("repo", {}).get("full_name") or "").casefold()
    head_repo = (pr.get("head", {}).get("repo", {}).get("full_name") or "").casefold()
    expected_branch = f"sentinel/disable-demo-fault-{ticket_number.lower()}"
    if base_repo != expected_repo or head_repo != expected_repo:
        raise GithubPullRequestError("The PR base and head must both belong to the configured demo repository.")
    if pr.get("head", {}).get("ref") != expected_branch or f"[{ticket_number}]" not in pr.get("title", ""):
        raise GithubPullRequestError("The PR branch or title does not match this incident ticket.")
    if require_open and pr.get("state") != "open":
        raise GithubPullRequestError("The PR is no longer open; refresh before approval.")
    repo_data = _request(_session(), "GET", "").json()
    if pr.get("base", {}).get("ref") != repo_data.get("default_branch"):
        raise GithubPullRequestError("The PR base must be the configured demo repository's default branch.")
    if len(files) != 1 or files[0].get("filename") != CONFIG_PATH:
        raise GithubPullRequestError(f"The PR must change only {CONFIG_PATH}.")
    if _settings_at_ref(pr["head"]["sha"]) != SAFE_SETTINGS:
        raise GithubPullRequestError("The PR settings do not exactly match Sentinel's allow-listed repair.")
    issue_number = linked_issue_number(pr)
    if not issue_number:
        raise GithubPullRequestError("The PR is not linked to this incident's GitHub issue.")
    issue = get_issue(issue_number)
    if ISSUE_MARKER.format(ticket_number=ticket_number) not in (issue.get("body") or ""):
        raise GithubPullRequestError("The linked GitHub issue does not identify this incident ticket.")
    if _binding_marker(ticket_number, repair_context) not in (pr.get("body") or ""):
        raise GithubPullRequestError("The PR does not carry this ticket's current evidence and preflight binding.")


def get_check_runs(commit_sha: str) -> List[Dict[str, Any]]:
    """:return: GitHub Actions/check-run results for the PR's current commit."""
    session = _session()
    return _request(session, "GET", f"/commits/{commit_sha}/check-runs").json().get("check_runs", [])


def checks_passed(checks: List[Dict[str, Any]]) -> bool:
    """:return: True only when at least one check exists and every check has succeeded."""
    return bool(checks) and all(check.get("status") == "completed" and check.get("conclusion") == "success" for check in checks)


def merge_pull_request(
    number: int, expected_sha: str, ticket_number: str, repair_context: Dict[str, Any], ticket_session_id: str
) -> Dict[str, Any]:
    """Merge only after a human approves in Sentinel and all current checks pass."""
    session = _session()
    pr = get_pull_request(number)
    if pr.get("head", {}).get("sha") != expected_sha:
        raise GithubPullRequestError("The PR changed after review. Refresh the diff and checks before approving again.")
    verify_repair_pr(pr, get_pull_request_files(number), ticket_number, repair_context, ticket_session_id, require_open=True)
    checks = get_check_runs(pr["head"]["sha"])
    if not checks_passed(checks):
        raise GithubPullRequestError("The PR cannot be merged until all GitHub checks have passed.")
    return _request(
        session,
        "PUT",
        f"/pulls/{number}/merge",
        json={"sha": expected_sha, "merge_method": "squash"},
    ).json()


def sync_merged_settings(
    number: int, ticket_number: str, repair_context: Dict[str, Any], ticket_session_id: str, expected_sha: str
) -> Path:
    """Fetch the merged allow-listed config and apply it to the local demo app."""
    pr = get_pull_request(number)
    if not pr.get("merged") or not pr.get("merge_commit_sha") or pr.get("head", {}).get("sha") != expected_sha:
        raise GithubPullRequestError("The merged PR no longer matches the commit that received human review.")
    verify_repair_pr(pr, get_pull_request_files(number), ticket_number, repair_context, ticket_session_id)
    checks = get_check_runs(expected_sha)
    if not checks_passed(checks):
        raise GithubPullRequestError("The merged PR's reviewed commit does not have all successful checks.")
    settings = _settings_at_ref(pr["merge_commit_sha"])
    if settings != SAFE_SETTINGS:
        raise GithubPullRequestError("Merged settings do not match Sentinel's reviewed safe repair.")
    local_path = Path(os.environ.get("SENTINEL_DEMO_APP_CONFIG_PATH", str(DEFAULT_LOCAL_SETTINGS)))
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return local_path
