"""Tests for Sentinel's issue-first and human-reviewed GitHub repair flow."""

from types import SimpleNamespace
from typing import Any
from typing import Dict
from typing import List
from typing import Tuple

from coded_tools.sentinel import github_pr_client


def test_repair_pr_creates_issue_before_branch_and_links_issue_for_merge(
    monkeypatch,
) -> None:
    """The GitHub issue is created before PR work and closes only through its merge keyword."""
    monkeypatch.setenv("SENTINEL_GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("SENTINEL_DEMO_GITHUB_OWNER", "demo-owner")
    monkeypatch.setenv("SENTINEL_DEMO_GITHUB_REPO", "demo-repo")
    calls: List[Tuple[str, str, Dict[str, Any]]] = []

    issue = {
        "number": 2,
        "state": "open",
        "html_url": "https://github.com/demo-owner/demo-repo/issues/2",
    }
    pull_request = {
        "number": 1,
        "state": "open",
        "html_url": "https://github.com/demo-owner/demo-repo/pull/1",
        "title": "Repair checkout fault",
        "body": "Sentinel repair.\n\nCloses #2",
    }

    def fake_request(_session: object, method: str, path: str, **kwargs: Any) -> SimpleNamespace:
        calls.append((method, path, kwargs))
        if method == "GET" and path == "":
            value: Any = {"default_branch": "main"}
        elif method == "GET" and path == "/pulls":
            value = []
        elif method == "GET" and path == "/issues":
            value = []
        elif method == "POST" and path == "/issues":
            value = issue
        elif method == "GET" and path == "/git/ref/heads/main":
            value = {"object": {"sha": "abc123"}}
        elif method == "GET" and path == "/contents/app/settings.json":
            value = {"sha": "settings-sha"}
        elif method == "POST" and path == "/pulls":
            value = pull_request
        else:
            value = {}
        return SimpleNamespace(json=lambda: value)

    monkeypatch.setattr(github_pr_client, "_request", fake_request)
    context = {
        "eligible": True,
        "all_passed": True,
        "ticket_number": "INC0010015",
        "session_id": "test-session",
        "service_name": "checkout-service",
        "verdict": "SUPPORTED",
        "runbook_id": "RB-042",
        "evidence_refs": [
            {"evidence_type": "log", "source": "fixture:logs.json", "record_id": "log-1", "source_status": "fixture"},
            {"evidence_type": "deployment", "source": "github:deployments", "record_id": "deployment-1", "source_status": "live"},
        ],
    }
    result = github_pr_client.create_repair_pr("INC0010015", context, "test-session")

    methods_and_paths = [(method, path) for method, path, _kwargs in calls]
    issue_creation = methods_and_paths.index(("POST", "/issues"))
    branch_creation = methods_and_paths.index(("POST", "/git/refs"))
    pr_creation = methods_and_paths.index(("POST", "/pulls"))
    assert issue_creation < branch_creation < pr_creation
    pr_payload = calls[pr_creation][2]["json"]
    assert "Closes #2" in pr_payload["body"]
    assert result["sentinel_issue_number"] == 2
    assert result["sentinel_issue_url"] == issue["html_url"]
    assert github_pr_client.linked_issue_number(result) == 2


def test_linked_issue_parser_ignores_unlinked_pr() -> None:
    """Only GitHub's issue-closing keywords count as a closing link."""
    assert github_pr_client.linked_issue_number({"body": "See issue #12 for context."}) is None
    assert github_pr_client.linked_issue_number({"body": "Fixes #12"}) == 12
