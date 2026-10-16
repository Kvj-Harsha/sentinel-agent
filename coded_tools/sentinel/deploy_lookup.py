"""Coded tool that looks up synthetic recent-deployment history for a service."""

import json
import logging
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel.service_names import canonical_service_name
from coded_tools.sentinel.schemas import EvidenceRecord
from coded_tools.sentinel.schemas import SourceStatus
from coded_tools.sentinel.schemas import dump_model
from coded_tools.sentinel.schemas import sanitize_record
from coded_tools.sentinel.schemas import unavailable_result
from coded_tools.sentinel.schemas import utc_now
from coded_tools.sentinel.verified_facts import publish

from coded_tools.sentinel.ticket_notes import add_note
from coded_tools.sentinel.verified_facts import publish_evidence

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "data" / "deploys.json"


class DeployLookup(CodedTool):
    """
    Looks up recent deployment/change history for a given service.
    Backed entirely by a small synthetic dataset (coded_tools/sentinel/data/deploys.json) --
    no real deployment records or credentials are involved.
    """

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        :param args: a dictionary with the following keys:
            - service_name: the name of the service to inspect (e.g. "checkout-service").

        :param sly_data: unused.

        :return: a dict with matching deploy records sorted most-recent-first, or an error string.
        """
        service_name: str = canonical_service_name(args.get("service_name") or "")
        environment: str = str(args.get("environment") or "").strip()
        if not service_name:
            return unavailable_result("fixture:deploys.json", "Please provide a service_name to look up deploy history for.")
        if service_name == "checkout-service":
            return unavailable_result(
                "fixture:deploys.json",
                "Fixture deployment fallback is disabled for checkout-service; use github_deployments.",
            )

        try:
            entries: List[Dict[str, Any]] = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except OSError as exc:
            return unavailable_result("fixture:deploys.json", f"Could not read deploy history dataset: {exc}")

        matches = [
            e for e in entries
            if service_name in e["service"].lower()
            and (not environment or str(e.get("environment") or "").casefold() == environment.casefold())
        ]
        matches.sort(key=lambda e: e["deployed_at"], reverse=True)
        logger.debug("DeployLookup matched %d deploys for service=%r", len(matches), service_name)

        if not matches:
            publish(sly_data, "deployment_lookup", {
                "service_name": service_name, "environment": environment or None,
                "source": "fixture:deploys.json", "source_status": SourceStatus.FIXTURE.value,
                "record_count": 0,
            })
            return {"matches": [], "evidence": [], "source": "fixture:deploys.json",
                    "source_status": SourceStatus.FIXTURE.value,
                    "summary": f"No recent deploys found for service '{service_name}'"
                    + (f" in environment '{environment}'." if environment else ".")}

        latest = matches[0]
        add_note(
            sly_data,
            f"[Deploys] Most recent deploy of {latest['service']}: {latest['version']} at {latest['deployed_at']} "
            f"by {latest['author']} - {latest['change_summary']}",
            once_key=f"deploy:{latest['service']}",
        )
        retrieved_at = utc_now()
        evidence = [dump_model(EvidenceRecord(
            evidence_type="deployment_change", service_name=item["service"], source="fixture:deploys.json",
            source_status=SourceStatus.FIXTURE,
            query=f"service={service_name} environment={environment}" if environment else service_name,
            record_id=f"{item['service']}:{item['version']}", event_time=item.get("deployed_at"),
            retrieved_at=retrieved_at,
            summary=f"{item['version']} by {item['author']}: {item['change_summary']}",
            safe_fields={key: item[key] for key in ("service", "environment", "deployed_at", "version", "author", "change_summary") if key in item},
            source_url=item.get("url"),
        )) for item in matches]
        publish(sly_data, "deployment_lookup", {
            "service_name": service_name, "environment": environment or None,
            "source": "fixture:deploys.json", "source_status": SourceStatus.FIXTURE.value,
            "record_count": len(evidence),
        })
        publish_evidence(sly_data, evidence)
        return {"matches": sanitize_record(matches), "evidence": evidence, "source": "fixture:deploys.json",
                "source_status": SourceStatus.FIXTURE.value,
                "summary": f"Found {len(matches)} recent deploy(s), most recent first."}

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick, non-blocking."""
        return self.invoke(args, sly_data)
