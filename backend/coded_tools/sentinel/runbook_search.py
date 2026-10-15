"""Coded tool that retrieves the best-matching past runbook/postmortem for an incident."""

import json
import logging
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel.ticket_notes import add_note

from coded_tools.sentinel.verified_facts import publish

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "data" / "runbooks.json"

# Words too generic to count as title-overlap evidence. Without this, "service" alone
# (present in every runbook title via "checkout-service", "inventory-service", etc.) made
# every runbook score >0 for every query -- found via the standalone sanity test when a
# search-service query falsely matched all 4 unrelated runbooks.
TITLE_STOPWORDS = frozenset(
    {
        "a", "an", "the", "on", "after", "for", "from", "with", "to", "of", "in", "at",
        "and", "or", "service", "services", "error", "errors",
    }
)


class RunbookSearch(CodedTool):
    """
    Finds the most relevant past runbook/postmortem for a free-text incident description,
    ranked by keyword overlap against each runbook's trigger_keywords and title.
    Backed entirely by a small synthetic dataset (backend/coded_tools/sentinel/data/runbooks.json).
    """

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        :param args: a dictionary with the following keys:
            - query: free-text description of the incident (service, symptoms, error codes, etc.).

        :param sly_data: the best match is published to sly_data["sentinel_verified"]["runbook"].

        :return: a dict with the ranked runbook matches (best first), or an error string.
        """
        query: str = (args.get("query") or "").strip().lower()
        if not query:
            return "Error: Please provide a query describing the incident to search runbooks for."

        try:
            runbooks: List[Dict[str, Any]] = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except OSError as exc:
            return f"Error: Could not read runbooks dataset: {exc}"

        query_tokens = set(query.replace("-", " ").split()) - TITLE_STOPWORDS

        scored = []
        for rb in runbooks:
            keyword_hits = sum(1 for kw in rb["trigger_keywords"] if kw.lower() in query)
            title_tokens = set(rb["title"].lower().replace("-", " ").split()) - TITLE_STOPWORDS
            title_hits = len(query_tokens.intersection(title_tokens))
            score = keyword_hits * 2 + title_hits
            if score > 0:
                scored.append({**rb, "match_score": score})

        scored.sort(key=lambda r: r["match_score"], reverse=True)
        logger.debug("RunbookSearch found %d candidate(s) for query=%r", len(scored), query)

        if not scored:
            return {"matches": [], "summary": "No matching runbook found for this incident description."}

        best = scored[0]
        publish(sly_data, "runbook", {key: best.get(key) for key in ("id", "title", "webui_link", "recommended_action")})
        add_note(
            sly_data,
            f"[Runbook] Matched Confluence runbook {best['id']} - {best['title']} "
            f"(link: {best.get('webui_link')}). Recommended action: {best.get('recommended_action')}",
            once_key=f"runbook:{best['id']}",
        )
        return {"matches": scored, "summary": f"Best match: {scored[0]['id']} - {scored[0]['title']}"}

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick, non-blocking."""
        return self.invoke(args, sly_data)
