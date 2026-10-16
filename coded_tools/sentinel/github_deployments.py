"""Read-only GitHub Deployments API adapter for checkout-service."""

from datetime import datetime
from datetime import timezone
import logging
import os
import re
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Union
from urllib.parse import quote

import requests
from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel.schemas import EvidenceRecord
from coded_tools.sentinel.schemas import SourceStatus
from coded_tools.sentinel.schemas import dump_model
from coded_tools.sentinel.schemas import sanitize_record
from coded_tools.sentinel.schemas import unavailable_result
from coded_tools.sentinel.schemas import utc_now
from coded_tools.sentinel.verified_facts import publish
from coded_tools.sentinel.verified_facts import publish_evidence

logger = logging.getLogger(__name__)

SOURCE = "github:deployments"
CHECKOUT_SERVICE = "checkout-service"
DEFAULT_ENVIRONMENT = "demo"
MAX_DEPLOYMENTS = 5
MAX_STATUSES_PER_DEPLOYMENT = 10
MAX_RESPONSE_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 5
API_VERSION = "2022-11-28"


class GithubDeploymentsError(Exception):
    """A bounded GitHub API request failed or returned an invalid payload."""


class GithubDeployments(CodedTool):
    """Find explicit deployment records and statuses; never infer deployment from commits or builds."""

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        service_name = str(args.get("service_name") or "").strip().lower()
        if service_name != CHECKOUT_SERVICE:
            return unavailable_result(SOURCE, f"No GitHub repository mapping is configured for service '{service_name or 'unknown'}'.")

        environment = str(args.get("environment") or os.environ.get(
            "SENTINEL_GITHUB_DEPLOYMENT_ENVIRONMENT", DEFAULT_ENVIRONMENT
        )).strip()
        if not environment or len(environment) > 100 or re.search(r"[\x00-\x1f\x7f]", environment):
            return unavailable_result(SOURCE, "A valid GitHub deployment environment is required.")

        owner = os.environ.get("SENTINEL_DEMO_GITHUB_OWNER", "").strip()
        repo = os.environ.get("SENTINEL_DEMO_GITHUB_REPO", "").strip()
        if not owner or not repo:
            return unavailable_result(SOURCE, "Configure SENTINEL_DEMO_GITHUB_OWNER and SENTINEL_DEMO_GITHUB_REPO for checkout-service.")

        try:
            client = _GithubDeploymentsClient(owner, repo)
            deployments = client.list_deployments(environment)
            matches = []
            evidence = []
            retrieved_at = utc_now()
            for deployment in deployments[:MAX_DEPLOYMENTS]:
                deployment_id = deployment.get("id")
                if not isinstance(deployment_id, int):
                    raise GithubDeploymentsError("GitHub returned a deployment without a valid ID.")
                returned_environment = str(deployment.get("environment") or "")
                if returned_environment and returned_environment.casefold() != environment.casefold():
                    continue
                statuses = client.list_statuses(deployment_id)
                latest_status = _latest_status(statuses)
                record = _normalize_deployment(
                    deployment, latest_status, service_name, owner, repo,
                    returned_environment or environment,
                )
                matches.append(record)
                evidence.append(dump_model(EvidenceRecord(
                    evidence_type="deployment",
                    service_name=service_name,
                    source=SOURCE,
                    source_status=SourceStatus.LIVE,
                    query=f"repo:{owner}/{repo} environment:{environment}",
                    record_id=str(deployment_id),
                    event_time=record.get("deployment_event_time"),
                    retrieved_at=retrieved_at,
                    summary=record["summary"],
                    safe_fields={key: record.get(key) for key in (
                        "repository", "environment", "state", "commit_sha", "actor",
                        "deployment_id", "deployment_created_at", "deployment_updated_at",
                        "deployment_event_time", "deployment_url", "status_url",
                    )},
                    source_url=record["deployment_url"],
                )))
        except GithubDeploymentsError as exc:
            logger.info("GitHub deployments unavailable for %s/%s: %s", owner, repo, exc)
            publish(sly_data, "deployment_lookup", {
                "service_name": service_name, "environment": environment,
                "source": SOURCE, "source_status": SourceStatus.UNAVAILABLE.value,
            })
            return unavailable_result(SOURCE, str(exc))

        publish(sly_data, "deployment_lookup", {
            "service_name": service_name, "environment": environment,
            "source": SOURCE, "source_status": SourceStatus.LIVE.value,
            "record_count": len(matches),
        })
        if evidence:
            publish_evidence(sly_data, evidence)

        summary = (
            f"GitHub returned no deployment records for {service_name} in environment '{environment}'."
            if not matches else
            f"Found {len(matches)} explicit GitHub deployment record(s) for {service_name} in '{environment}'. "
            "Only records whose latest deployment status is success count as completed deployments."
        )
        return {
            "matches": sanitize_record(matches),
            "evidence": evidence,
            "source": SOURCE,
            "source_status": SourceStatus.LIVE.value,
            "repository": f"{owner}/{repo}",
            "environment": environment,
            "summary": summary,
        }

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous adapter; every request has a short timeout and fixed result cap."""
        return self.invoke(args, sly_data)


class _GithubDeploymentsClient:
    """Small read-only client for the two GitHub Deployments endpoints used here."""

    def __init__(self, owner: str, repo: str) -> None:
        self.owner = owner
        self.repo = repo
        self.timeout = _bounded_timeout()
        self.base_url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/deployments"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "sentinel-agent-readonly",
        }
        token = os.environ.get("SENTINEL_GITHUB_DEPLOYMENTS_TOKEN", "").strip()
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def list_deployments(self, environment: str) -> List[Dict[str, Any]]:
        rows = self._get_json(self.base_url, {"environment": environment, "per_page": MAX_DEPLOYMENTS})
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise GithubDeploymentsError("GitHub returned malformed deployment records.")
        return rows[:MAX_DEPLOYMENTS]

    def list_statuses(self, deployment_id: int) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/{deployment_id}/statuses"
        rows = self._get_json(url, {"per_page": MAX_STATUSES_PER_DEPLOYMENT})
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise GithubDeploymentsError("GitHub returned malformed deployment statuses.")
        return rows[:MAX_STATUSES_PER_DEPLOYMENT]

    def _get_json(self, url: str, params: Dict[str, Any]) -> Any:
        try:
            response = requests.get(url, params=params, headers=self.headers, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise GithubDeploymentsError("Could not reach the GitHub Deployments API.") from exc
        if response.status_code >= 400:
            status = response.status_code
            if status in (401, 403):
                raise GithubDeploymentsError(
                    f"GitHub returned HTTP {status}; check repository visibility, rate limit, or the read-only deployments token."
                )
            if status == 404:
                raise GithubDeploymentsError("GitHub repository or deployment endpoint was not found or is not accessible.")
            raise GithubDeploymentsError(f"GitHub Deployments API returned HTTP {status}.")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise GithubDeploymentsError("GitHub returned a response larger than the configured limit.")
        try:
            return response.json()
        except ValueError as exc:
            raise GithubDeploymentsError("GitHub returned invalid JSON.") from exc


def _latest_status(statuses: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not statuses:
        return None
    return max(
        statuses,
        key=lambda status: _parse_time(status.get("updated_at") or status.get("created_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
    )


def _normalize_deployment(
    deployment: Dict[str, Any], latest_status: Optional[Dict[str, Any]],
    service_name: str, owner: str, repo: str, environment: str,
) -> Dict[str, Any]:
    state = str((latest_status or {}).get("state") or "unknown").strip().lower()
    status_time = _parse_time((latest_status or {}).get("updated_at") or (latest_status or {}).get("created_at"))
    deployment_id = deployment["id"]
    sha = str(deployment.get("sha") or "")
    actor = ((latest_status or {}).get("creator") or deployment.get("creator") or {}).get("login")
    deployment_url = f"{_GithubDeploymentsClient(owner, repo).base_url}/{deployment_id}"
    status_url = f"{deployment_url}/statuses"
    success_time = status_time if state == "success" else None
    timestamp = success_time.isoformat().replace("+00:00", "Z") if success_time else None
    if state == "success":
        summary = f"Successful deployment {sha[:12] or deployment_id} to {environment} at {timestamp}."
    else:
        summary = f"GitHub deployment {deployment_id} to {environment} has latest state '{state}'; it is not treated as a completed deployment."
    return {
        "service": service_name,
        "repository": f"{owner}/{repo}",
        "environment": environment,
        "deployment_id": deployment_id,
        "state": state,
        "commit_sha": sha,
        "version": sha[:12] if sha else "unknown",
        "actor": actor or "unknown",
        "deployment_created_at": deployment.get("created_at"),
        "deployment_updated_at": deployment.get("updated_at"),
        "deployment_event_time": timestamp,
        "deployed_at": timestamp,
        "deployment_url": deployment_url,
        "status_url": status_url,
        "summary": summary,
    }


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _bounded_timeout() -> float:
    try:
        return max(1.0, min(float(os.environ.get("SENTINEL_GITHUB_DEPLOYMENTS_TIMEOUT_SECONDS", "5")), 10.0))
    except ValueError:
        return 5.0
