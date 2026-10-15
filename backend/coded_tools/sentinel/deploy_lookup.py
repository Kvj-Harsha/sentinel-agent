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

from coded_tools.sentinel.ticket_notes import add_note

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "data" / "deploys.json"


class DeployLookup(CodedTool):
    """
    Looks up recent deployment/change history for a given service.
    Backed entirely by a small synthetic dataset (backend/coded_tools/sentinel/data/deploys.json) --
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
        if not service_name:
            return "Error: Please provide a service_name to look up deploy history for."

        try:
            entries: List[Dict[str, Any]] = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except OSError as exc:
            return f"Error: Could not read deploy history dataset: {exc}"

        matches = [e for e in entries if service_name in e["service"].lower()]
        matches.sort(key=lambda e: e["deployed_at"], reverse=True)
        logger.debug("DeployLookup matched %d deploys for service=%r", len(matches), service_name)

        if not matches:
            return {"matches": [], "summary": f"No recent deploys found for service '{service_name}'."}

        latest = matches[0]
        add_note(
            sly_data,
            f"[Deploys] Most recent deploy of {latest['service']}: {latest['version']} at {latest['deployed_at']} "
            f"by {latest['author']} - {latest['change_summary']}",
            once_key=f"deploy:{latest['service']}",
        )
        return {"matches": matches, "summary": f"Found {len(matches)} recent deploy(s), most recent first."}

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick, non-blocking."""
        return self.invoke(args, sly_data)
