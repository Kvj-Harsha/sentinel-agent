"""Durable UI conversations: a page reload (or a new browser tab) restores the ongoing incident instead of losing it."""

import logging
import os
import uuid
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import Optional

from coded_tools.sentinel.sqlite_store import SQLiteDatabase
from coded_tools.sentinel.ticket_store import TicketStore

LOGGER = logging.getLogger(__name__)
STALE_RUN_SECONDS = 600  # a worker that has not finished after this long died with its container
STATE_KEYS = ("messages", "chat_context", "sly_data", "ticket_number", "alert_context", "running")


def _enabled() -> bool:
    return os.environ.get("SENTINEL_STORAGE_BACKEND", "sqlite").lower() == "sqlite"


def new_id() -> str:
    return uuid.uuid4().hex


def persist(conversation_id: str, state: Dict[str, Any]) -> None:
    """Save the conversation. Safe to call from a worker thread; never raises into the UI."""
    if not _enabled() or not conversation_id:
        return
    try:
        number = state.get("ticket_number") or (state.get("sly_data") or {}).get("sentinel_ticket_number")
        SQLiteDatabase().save_conversation(conversation_id, {key: state.get(key) for key in STATE_KEYS}, number)
    except Exception:  # pylint: disable=broad-exception-caught
        LOGGER.warning("Could not persist conversation %s", conversation_id, exc_info=True)


def archive(conversation_id: Optional[str]) -> None:
    """Mark a conversation as finished so it is not restored again (the user pressed New incident)."""
    if not _enabled() or not conversation_id:
        return
    try:
        SQLiteDatabase().archive_conversation(conversation_id)
    except Exception:  # pylint: disable=broad-exception-caught
        LOGGER.warning("Could not archive conversation %s", conversation_id, exc_info=True)


def _settle_if_stale(conversation: Dict[str, Any], *, interrupted: bool = False) -> Dict[str, Any]:
    """A run flagged as running whose worker can no longer exist is closed with an honest message."""
    if not conversation.get("running"):
        return conversation
    try:
        updated = datetime.fromisoformat(str(conversation.get("updated_at")).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - updated).total_seconds()
    except ValueError:
        age = STALE_RUN_SECONDS + 1
    if interrupted or age > STALE_RUN_SECONDS:
        conversation["running"] = False
        conversation["messages"] = [*(conversation.get("messages") or []), {
            "role": "assistant",
            "content": "The previous investigation was interrupted before it finished (the service restarted). "
                       "Send the alert again to re-run it; the ticket, if one opened, is unchanged.",
        }]
        persist(conversation["id"], conversation)
    return conversation


def restore_latest(*, interrupt_running: bool = False) -> Optional[Dict[str, Any]]:
    """
    Return the newest ongoing conversation: not archived, and its ticket (if it has one) is not Resolved.
    A resolved incident is finished work, so the page starts clean instead.
    """
    if not _enabled():
        return None
    try:
        for conversation in SQLiteDatabase().active_conversations():
            number = conversation.get("ticket_number")
            ticket = TicketStore().get(number) if number else None
            if ticket and ticket.get("state") == "Resolved":
                continue
            return _settle_if_stale(conversation, interrupted=interrupt_running)
    except Exception:  # pylint: disable=broad-exception-caught
        LOGGER.warning("Could not restore the previous conversation", exc_info=True)
    return None


def load(conversation_id: str) -> Optional[Dict[str, Any]]:
    """Re-read one conversation (used to pick up the result of an investigation that finished after a reload)."""
    if not _enabled() or not conversation_id:
        return None
    try:
        found = next((c for c in SQLiteDatabase().active_conversations(limit=20) if c["id"] == conversation_id), None)
        return _settle_if_stale(found) if found else None
    except Exception:  # pylint: disable=broad-exception-caught
        LOGGER.warning("Could not reload conversation %s", conversation_id, exc_info=True)
        return None
