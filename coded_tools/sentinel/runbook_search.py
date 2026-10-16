"""Coded tool that retrieves the best-matching past runbook/postmortem for an incident."""

import json
import logging
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Union

import requests

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel.ticket_notes import add_note
from coded_tools.sentinel.service_names import known_services
from coded_tools.sentinel.schemas import EvidenceRecord
from coded_tools.sentinel.schemas import SourceStatus
from coded_tools.sentinel.schemas import dump_model
from coded_tools.sentinel.schemas import sanitize_record
from coded_tools.sentinel.schemas import unavailable_result
from coded_tools.sentinel.schemas import utc_now

from coded_tools.sentinel.verified_facts import publish
from coded_tools.sentinel.verified_facts import publish_evidence
from coded_tools.sentinel.confluence_runbooks import ConfluenceRunbooks
from coded_tools.sentinel.mode import demo_mode

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


# Alerts say "5xx"; runbooks are titled by symptom ("API Service Unavailable"). Expand the status class into the
# plain-language symptoms so a vague agent-written query still finds the right page.
SYMPTOM_SYNONYMS = {
    "5xx": "server error service unavailable api",
    "503": "service unavailable",
    "500": "internal server error",
}


def _expand_symptoms(query: str) -> str:
    extra = [words for key, words in SYMPTOM_SYNONYMS.items() if key in query]
    return f"{query} {' '.join(extra)}" if extra else query


class RunbookSearch(CodedTool):
    """
    Finds the most relevant past runbook/postmortem for a free-text incident description,
    ranked by keyword overlap against each runbook's trigger_keywords and title.
    Uses configured, space-scoped Confluence pages; falls back to the local fixture only when
    Confluence is not configured.
    """

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        :param args: a dictionary with the following keys:
            - query: free-text description of the incident (service, symptoms, error codes, etc.).

        :param sly_data: the best match is published to sly_data["sentinel_verified"]["runbook"].

        :return: a dict with the ranked runbook matches (best first), or an error string.
        """
        query: str = (args.get("query") or "").strip().lower()
        symptoms = str((sly_data.get("sentinel_incident") or {}).get("symptoms") or "").strip().lower()
        if symptoms:
            query = f"{query} {symptoms}".strip()
        if not query:
            return unavailable_result("fixture:runbooks.json", "Please provide an incident query to search runbooks for.")

        confluence = ConfluenceRunbooks()
        if confluence.requested and not demo_mode():
            if not confluence.configured:
                return unavailable_result("confluence", "Confluence is partially configured; set the account email and explicit space key(s) in .env.")
            try:
                runbooks = confluence.search(query)
                source = "confluence"
                source_status = SourceStatus.LIVE
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                logger.warning("Confluence runbook lookup failed (%s).", type(exc).__name__)
                return unavailable_result("confluence", "Confluence runbook lookup failed; check connector health and permissions.")
        else:
            try:
                runbooks = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            except OSError as exc:
                return unavailable_result("fixture:runbooks.json", f"Could not read runbooks dataset: {exc}")
            source = "fixture:runbooks.json"
            source_status = SourceStatus.FIXTURE

        query = _expand_symptoms(query)
        query_tokens = set(query.replace("-", " ").split()) - TITLE_STOPWORDS

        scored = []
        for rb in runbooks:
            keyword_hits = sum(1 for kw in rb.get("trigger_keywords", []) if kw.lower() in query)
            title_tokens = set(rb["title"].lower().replace("-", " ").split()) - TITLE_STOPWORDS
            title_hits = len(query_tokens.intersection(title_tokens))
            content_hits = sum(1 for token in query_tokens if token in rb.get("search_text", ""))
            score = keyword_hits * 2 + title_hits + content_hits
            if score > 0:
                scored.append({**rb, "match_score": score, "_source_content_hits": content_hits})

        scored.sort(key=lambda r: r["match_score"], reverse=True)
        logger.debug("RunbookSearch found %d candidate(s) from %d query terms.", len(scored), len(query_tokens))

        if not scored:
            return {"matches": [], "evidence": [], "source": source,
                    "source_status": source_status.value,
                    "summary": "No matching runbook found for this incident description."}

        best = scored[0]
        if source_status == SourceStatus.LIVE:
            try:
                best.update(confluence.get_page(best["page_id"]))
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                logger.warning("Confluence selected-page lookup failed (%s).", type(exc).__name__)
                return unavailable_result("confluence", "Could not read the selected Confluence runbook page; check connector health and permissions.")
        publish(sly_data, "runbook", {key: best.get(key) for key in ("id", "title", "webui_link", "recommended_action")})
        add_note(
            sly_data,
            f"[Runbook] Matched {('Confluence' if source_status == SourceStatus.LIVE else 'fixture')} runbook {best['id']} - {best['title']} "
            f"(link: {best.get('webui_link')}). Recommended action: {best.get('recommended_action')}",
            once_key=f"runbook:{best['id']}",
        )
        retrieved_at = utc_now()
        evidence = []
        services = known_services()
        for item in scored:
            service = next((name for name in services if name in item["title"].lower()), "unspecified")
            evidence.append(dump_model(EvidenceRecord(
                evidence_type="runbook", service_name=service, source=source,
                source_status=source_status, query=query, record_id=item["id"],
                event_time=(item.get("version") or {}).get("when"), retrieved_at=retrieved_at,
                summary=f"{item['title']}: {item.get('recommended_action', '')}",
                safe_fields={key: item[key] for key in ("id", "title", "recommended_action", "related_incident", "match_score") if key in item},
                source_url=item.get("webui_link"),
            )))
        publish_evidence(sly_data, evidence)
        public_matches = [{key: value for key, value in item.items() if key not in ("search_text", "_source_content_hits")} for item in scored]
        return {"matches": sanitize_record(public_matches), "evidence": evidence, "source": source,
                "source_status": source_status.value,
                "summary": f"Best match: {scored[0]['id']} - {scored[0]['title']}"}

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick, non-blocking."""
        return self.invoke(args, sly_data)
