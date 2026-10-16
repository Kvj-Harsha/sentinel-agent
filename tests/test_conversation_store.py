"""A reload must bring back the ongoing incident; finished or archived ones must not come back."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "sentinel"))

import conversation_store  # noqa: E402
from coded_tools.sentinel.sqlite_store import SQLiteDatabase  # noqa: E402


@pytest.fixture(autouse=True)
def sqlite_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTINEL_STORAGE_BACKEND", "sqlite")
    database = SQLiteDatabase(tmp_path / "ui.db", seed_legacy_data=False)
    monkeypatch.setattr(conversation_store, "SQLiteDatabase", lambda: database)
    monkeypatch.setattr(conversation_store, "TicketStore", lambda: _Tickets())


class _Tickets:
    states = {"INC1": "In Progress", "INC2": "Resolved"}

    def get(self, number):
        return {"number": number, "state": self.states[number]} if number in self.states else None


def _state(ticket, running=False):
    return {"messages": [{"role": "user", "content": "alert"}, {"role": "assistant", "content": "done"}],
            "chat_context": {"k": 1}, "sly_data": {"sentinel_ticket_number": ticket}, "ticket_number": ticket,
            "alert_context": {"service": "checkout-service"}, "running": running}


def test_ongoing_conversation_is_restored_with_everything():
    conversation_store.persist("c1", _state("INC1"))
    restored = conversation_store.restore_latest()
    assert restored["id"] == "c1" and restored["ticket_number"] == "INC1"
    assert restored["messages"][1]["content"] == "done" and restored["chat_context"] == {"k": 1}


def test_resolved_and_archived_conversations_are_not_restored():
    conversation_store.persist("done", _state("INC2"))
    assert conversation_store.restore_latest() is None
    conversation_store.persist("open", _state("INC1"))
    conversation_store.archive("open")
    assert conversation_store.restore_latest() is None


def test_run_that_outlived_its_worker_is_closed_honestly(monkeypatch):
    monkeypatch.setattr(conversation_store, "STALE_RUN_SECONDS", -1)
    conversation_store.persist("c1", _state("INC1", running=True))
    restored = conversation_store.restore_latest()
    assert restored["running"] is False
    assert "interrupted" in restored["messages"][-1]["content"]
