"""
Backend entry point for Sentinel.

Runs the neuro-san agent network in-process (no separate server needed) and exposes one function,
invoke_agent(). Used by the Streamlit frontend (frontend/run.py), the alert CLI (backend/alerts.py)
and, directly, by `python backend/run.py` for a headless smoke test.
"""

import os
import sys
from pathlib import Path
from typing import Any
from typing import Dict
from typing import Optional

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent
AGENT_NAME = "sentinel"

# Secrets (OPENAI_API_KEY, ...) come from <repo>/.env; neuro-san itself does not load it.
load_dotenv(REPO_ROOT / ".env")

# OpenRouter accepts the same token as OPENAI_API_KEY, but some neuro-san policy versions
# look for the provider-specific name when the OpenAI-compatible base URL points to OpenRouter.
if "openrouter.ai" in os.environ.get("OPENAI_API_BASE", "").lower():
    os.environ.setdefault("OPENROUTER_API_KEY", os.environ.get("OPENAI_API_KEY", ""))

# HOCON `include "backend/registries/..."` paths are resolved against the working directory, so
# always run from the repository root no matter where the process was started.
os.chdir(REPO_ROOT)

os.environ["AGENT_MANIFEST_FILE"] = str(BACKEND_DIR / "registries" / "manifest.hocon")
os.environ["AGENT_TOOL_PATH"] = str(BACKEND_DIR / "coded_tools")

# neuro-san maps AGENT_TOOL_PATH to a dotted module path using PYTHONPATH, and imports
# `coded_tools.sentinel.*` through sys.path, so the backend directory must be on both.
_existing_pythonpath = os.environ.get("PYTHONPATH", "")
if str(BACKEND_DIR) not in _existing_pythonpath.split(os.pathsep):
    os.environ["PYTHONPATH"] = (
        f"{BACKEND_DIR}{os.pathsep}{_existing_pythonpath}" if _existing_pythonpath else str(BACKEND_DIR)
    )
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# pylint: disable=wrong-import-position
from neuro_san.client.agent_session_factory import DirectAgentSessionFactory  # noqa: E402

from coded_tools.sentinel.record_incident import RecordIncident  # noqa: E402
from coded_tools.sentinel.remediation_pipeline import RemediationPipeline  # noqa: E402
from coded_tools.sentinel.remediation_pipeline import plan_exists  # noqa: E402
from coded_tools.sentinel.ticket_store import TicketStore  # noqa: E402
from coded_tools.sentinel.update_ticket import UpdateTicket  # noqa: E402

# pylint: enable=wrong-import-position


def invoke_agent(
    user_text: str,
    sly_data: Optional[Dict[str, Any]] = None,
    chat_context: Optional[Dict[str, Any]] = None,
    agent_name: str = AGENT_NAME,
) -> Dict[str, Any]:
    """
    Sends one message to the agent network and returns its final response.

    :param user_text: the message (e.g. a composed incident report) for the front-man agent.
    :param sly_data: private control data from the previous turn (never shown to the LLM); pass the
        value returned by the previous call so a follow-up reuses the same ticket.
    :param chat_context: chat context returned by the previous call, to continue the conversation.
    :param agent_name: network name from manifest.hocon, without the .hocon suffix.
    :return: the final response body with keys "text" (the answer) and, when available,
        "chat_context" and "sly_data" to pass into the next call. Empty dict if nothing came back.
    """
    factory = DirectAgentSessionFactory()
    session = factory.create_session(agent_name=agent_name, use_direct=True, metadata={})

    request_payload: Dict[str, Any] = {"user_message": {"text": user_text}, "sly_data": sly_data or {}}
    if chat_context:
        request_payload["chat_context"] = chat_context

    last_message: Dict[str, Any] = {}
    for chat_msg in session.streaming_chat(request_payload):
        if chat_msg.get("response"):
            last_message = chat_msg
        if chat_msg.get("done") is True:
            break
    response = last_message.get("response", {})
    _ensure_remediated_and_resolved(response)
    _ensure_incident_recorded(response)
    return response


def _ensure_remediated_and_resolved(response: Dict[str, Any]) -> None:
    """
    Guarantees a local remediation proposal exists for a verified root cause. A human-reviewed GitHub PR and
    recovery verification are required before the UI can resolve the ticket.

    :param response: the final response body from the network (its sly_data is updated in place).
    """
    sly_data = response.get("sly_data")
    if not isinstance(sly_data, dict):
        return
    facts = sly_data.get("sentinel_verified") or {}
    correlation = facts.get("correlation") or {}
    service_name = correlation.get("service_name", "")
    number = sly_data.get("sentinel_ticket_number")
    if correlation.get("verdict") != "SUPPORTED" or not number or not plan_exists(service_name):
        return
    if not facts.get("remediation"):
        RemediationPipeline().invoke({"service_name": service_name}, sly_data)
    remediation = (sly_data.get("sentinel_verified") or {}).get("remediation") or {}
    ticket = TicketStore().get(number) or {}
    if remediation.get("all_passed") and ticket.get("state") != "Resolved":
        UpdateTicket().invoke(
            {
                "number": number,
                "work_note": (
                    f"Local preflight passed for the proposed fix from runbook {remediation.get('runbook_id')}. "
                    "GitHub PR checks, human approval, merge and recovery verification are still required."
                ),
            },
            sly_data,
        )


def _ensure_incident_recorded(response: Dict[str, Any]) -> None:
    """
    Guarantees the incident reaches persistent memory once a verdict exists. The HistorianAgent is asked to
    record it, but an LLM may skip or repeat that tool call; RecordIncident is idempotent per incident session,
    so calling it here again is a no-op when the agent already did, and a safety net when it didn't.

    :param response: the final response body from the network (its sly_data is updated in place).
    """
    sly_data = response.get("sly_data")
    if not isinstance(sly_data, dict):
        return
    facts = sly_data.get("sentinel_verified") or {}
    correlation = facts.get("correlation")
    if not correlation:
        return
    runbook_id = (facts.get("runbook") or {}).get("id")
    summary = (
        f"{correlation['service_name']}: {correlation['verdict']} -- deploy and incident "
        f"{correlation['delta_minutes']} min apart" + (f"; matched runbook {runbook_id}." if runbook_id else ".")
    )
    RecordIncident().invoke(
        {
            "service_name": correlation["service_name"],
            "hypothesis": correlation["hypothesis"],
            "verdict": correlation["verdict"],
            "runbook_id": runbook_id,
            "summary": summary,
        },
        sly_data,
    )


def format_verified_facts(sly_data: Optional[Dict[str, Any]]) -> str:
    """
    Renders what the deterministic coded tools actually found (not the LLM's paraphrase of it).

    :param sly_data: the sly_data returned by invoke_agent().
    :return: markdown bullet lines, or an empty string if no tool published anything.
    """
    facts = (sly_data or {}).get("sentinel_verified") or {}
    lines = []
    correlation = facts.get("correlation")
    if correlation:
        lines.append(
            f"- **Correlation check:** {correlation['verdict']} -- deploy at {correlation['deploy_time']}, "
            f"incident at {correlation['incident_time']} ({correlation['delta_minutes']} min apart)"
        )
    runbook = facts.get("runbook")
    if runbook:
        lines.append(f"- **Runbook:** {runbook['id']} -- {runbook['title']} (`{runbook['webui_link']}`)")
    remediation = facts.get("remediation")
    if remediation:
        lines.append(f"- **Fix:** {remediation['fix_summary']} (`{remediation['config_file']}`)")
        for check in remediation["checks"]:
            mark = "PASS" if check["status"] == "passed" else "FAIL"
            lines.append(f"- **Local preflight · {check['name']}:** {mark} -- {check['detail']}")
    memory = facts.get("memory") or {}
    if memory.get("suggested_hotfix"):
        lines.append(f"- **Suggested hotfix:** {memory['suggested_hotfix']}")
    return "\n".join(lines)


def make_stdout_safe() -> None:
    """Windows consoles default to cp1252 and crash on punctuation an LLM may emit (e.g. U+2011)."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    """Headless smoke test: triage one built-in incident and print the answer."""
    make_stdout_safe()
    incident = (
        "checkout-service is throwing 500 errors with NullPointerException on PaymentValidator.charge() "
        "since 14:02 UTC on 2026-10-12, what's going on?"
    )
    response = invoke_agent(incident)
    print(response.get("text", "(no response)"))
    verified = format_verified_facts(response.get("sly_data"))
    if verified:
        print("\nVerified by tools (deterministic, not LLM-generated):\n" + verified)


if __name__ == "__main__":
    main()
