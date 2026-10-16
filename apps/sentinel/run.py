"""
Backend entry point for Sentinel.

Runs the neuro-san agent network in-process (no separate server needed) and exposes one function,
invoke_agent(). Used by the Streamlit app (apps/sentinel/ui.py), the alert CLI (apps/sentinel/alerts.py)
and, directly, by `python apps/sentinel/run.py` for a headless smoke test.
"""

import os
import sys
from pathlib import Path
from typing import Any
from typing import Dict
from typing import Optional

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent.parent
AGENT_NAME = "sentinel"

# Secrets (OPENAI_API_KEY, ...) come from <repo>/.env; neuro-san itself does not load it.
load_dotenv(REPO_ROOT / ".env")

# OpenRouter accepts the same token as OPENAI_API_KEY, but some neuro-san policy versions
# look for the provider-specific name when the OpenAI-compatible base URL points to OpenRouter.
if "openrouter.ai" in os.environ.get("OPENAI_API_BASE", "").lower():
    os.environ.setdefault("OPENROUTER_API_KEY", os.environ.get("OPENAI_API_KEY", ""))

# HOCON `include "registries/..."` paths are resolved against the working directory, so
# always run from the repository root no matter where the process was started.
os.chdir(REPO_ROOT)

os.environ["AGENT_MANIFEST_FILE"] = str(REPO_ROOT / "registries" / "manifest.hocon")
os.environ["AGENT_TOOL_PATH"] = str(REPO_ROOT / "coded_tools")

# neuro-san maps AGENT_TOOL_PATH to a dotted module path using PYTHONPATH, and imports
# `coded_tools.sentinel.*` through sys.path, so the repository root must be on both.
_existing_pythonpath = os.environ.get("PYTHONPATH", "")
if str(REPO_ROOT) not in _existing_pythonpath.split(os.pathsep):
    os.environ["PYTHONPATH"] = (
        f"{REPO_ROOT}{os.pathsep}{_existing_pythonpath}" if _existing_pythonpath else str(REPO_ROOT)
    )
for _path in (str(REPO_ROOT), str(APP_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# pylint: disable=wrong-import-position
from neuro_san.client.agent_session_factory import DirectAgentSessionFactory  # noqa: E402

from coded_tools.sentinel.finalize_incident import build_report  # noqa: E402
from coded_tools.sentinel.incident_context import seed_incident_context  # noqa: E402
from coded_tools.sentinel.schemas import sanitize_record  # noqa: E402

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

    sly_data = dict(sly_data or {})
    seed_incident_context(user_text, sly_data)
    request_payload: Dict[str, Any] = {"user_message": {"text": user_text}, "sly_data": sly_data}
    if chat_context:
        request_payload["chat_context"] = chat_context

    last_message: Dict[str, Any] = {}
    for chat_msg in session.streaming_chat(request_payload):
        if chat_msg.get("response"):
            last_message = chat_msg
        if chat_msg.get("done") is True:
            break
    response = last_message.get("response", {})
    _prepend_verified_report(response)
    return response


def _prepend_verified_report(response: Dict[str, Any]) -> None:
    """
    Put the tool-built report ahead of the model's narrative. A small model may reword or contradict the verdict,
    runbook or source labels; the report is rendered by code from verified facts, so it is authoritative.
    """
    sly_data = response.get("sly_data")
    if not isinstance(sly_data, dict) or not (sly_data.get("sentinel_verified") or {}).get("correlation"):
        return
    report = "\n".join(f"- {line}" for line in build_report(sly_data).splitlines())
    response["text"] = (
        f"**Verified by tools**\n{report}\n\n---\n*Agent narrative (the lines above are authoritative):*\n\n"
        f"{response.get('text', '')}"
    )


def format_verified_facts(sly_data: Optional[Dict[str, Any]]) -> str:
    """
    Renders what the deterministic coded tools actually found (not the LLM's paraphrase of it).

    :param sly_data: the sly_data returned by invoke_agent().
    :return: markdown bullet lines, or an empty string if no tool published anything.
    """
    facts = sanitize_record((sly_data or {}).get("sentinel_verified") or {})
    lines = []
    correlation = facts.get("correlation")
    if correlation:
        if correlation.get("deploy_time") and correlation.get("incident_time"):
            lines.append(
                f"- **Correlation check:** {correlation['verdict']} -- deploy at {correlation['deploy_time']}, "
                f"incident at {correlation['incident_time']} ({correlation.get('delta_minutes', 'unknown')} min apart)"
            )
        else:
            lines.append(
                f"- **Correlation check:** {'INSUFFICIENT EVIDENCE' if correlation.get('insufficient_evidence') else correlation.get('verdict', 'unknown')}"
                f" -- {correlation.get('reasoning', 'No deterministic explanation was recorded.')}"
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
    evidence = facts.get("evidence") or []
    for record in evidence:
        status = record.get("source_status", "unknown")
        event_time = record.get("event_time") or "time unavailable"
        record_id = record.get("record_id") or "no record ID"
        source_url = record.get("source_url")
        link = f" · [open source query]({source_url})" if source_url else ""
        lines.append(
            f"- **Evidence · {record.get('evidence_type', 'record')}:** {record.get('summary', '')} "
            f"(source: {record.get('source', 'unknown')}; status: {status}; ID: {record_id}; event: {event_time}){link}"
        )
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
