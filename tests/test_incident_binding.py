"""Ticket ownership regressions using isolated stores and mocked agent/provider calls."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from http.server import ThreadingHTTPServer

import web
import alertmanager_receiver as receiver
from coded_tools.sentinel import ticket_notes
from coded_tools.sentinel.open_ticket import OpenTicket
from coded_tools.sentinel.ticket_store import TicketStore
from coded_tools.sentinel.update_ticket import UpdateTicket


@pytest.fixture(params=["json", "sqlite"])
def store(request, tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_SEED_LEGACY_DATA", "false")
    return TicketStore(tmp_path / "tickets.json") if request.param == "json" else TicketStore(backend="sqlite", db_path=tmp_path / "tickets.db")


def open_ticket(store, session, fingerprint=None):
    state = {"incident_session_id": session}
    if fingerprint:
        state["alert_fingerprint"] = fingerprint
    result = OpenTicket(store).invoke({"service_name": "checkout-service", "short_description": session}, state)
    assert isinstance(result, dict)
    return state


@pytest.mark.parametrize("operation", ["update", "note", "open"])
def test_cross_session_update_and_note_are_rejected(store, operation):
    owner = open_ticket(store, "owner")
    other = open_ticket(store, "other")
    before = store.load()
    forged = {**owner, "sentinel_ticket_number": other["sentinel_ticket_number"]}
    if operation == "update":
        result = UpdateTicket(store).invoke({"number": other["sentinel_ticket_number"], "work_note": "wrong"}, owner)
        assert isinstance(result, str) and result.startswith("Error:")
    elif operation == "note":
        assert ticket_notes.add_note(forged, "wrong", store, once_key="proof") is False
        assert "proof" not in forged.get("sentinel_notes_posted", [])
    else:
        assert isinstance(OpenTicket(store).invoke({}, forged), str)
    assert store.load() == before


def test_fingerprint_reuse_adopts_existing_owner(store):
    owner = open_ticket(store, "original", "same-alert")
    retry = open_ticket(store, "retry-session", "same-alert")
    assert retry["sentinel_ticket_number"] == owner["sentinel_ticket_number"]
    assert retry["incident_session_id"] == "original"
    assert ticket_notes.add_note(retry, "valid retry", store)


def test_concurrent_independent_sessions_keep_their_notes(store):
    barrier = threading.Barrier(4)
    def investigate(index):
        state = open_ticket(store, f"session-{index}")
        barrier.wait(timeout=5)
        assert ticket_notes.add_note(state, f"owned-note-{index}", store)
        return index, state
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(investigate, range(4)))
    assert len({state["sentinel_ticket_number"] for _, state in results}) == 4
    for index, state in results:
        ticket = store.get(state["sentinel_ticket_number"])
        assert ticket["session_id"] == f"session-{index}"
        assert sum("owned-note-" in note for note in ticket["work_notes"]) == 1
        assert ticket["work_notes"][-1].endswith(f"owned-note-{index}")


def test_webhook_progress_does_not_bind_same_service_manual_ticket(store, monkeypatch):
    open_ticket(store, "manual")
    monkeypatch.setattr(receiver, "TicketStore", lambda: store)
    monkeypatch.setenv("SENTINEL_STORAGE_BACKEND", "json")
    monkeypatch.setattr(receiver, "_jobs", [{"id": "webhook", "state": "investigating", "service": "checkout-service", "ticket_count_before": 0}])
    jobs = receiver.snapshot_jobs()
    assert not jobs[0].get("ticket_number")


@pytest.fixture
def manual(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTINEL_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "manual.db"))
    monkeypatch.setenv("SENTINEL_SEED_LEGACY_DATA", "false")
    monkeypatch.setattr(web, "_conversation", {"id": None, "messages": [], "chat_context": {}, "sly_data": {}, "ticket_number": None, "alert_context": None, "running": False, "updated": 0})
    yield
    for thread in threading.enumerate():
        if thread.name in {"sentinel-turn", "sentinel-ticket-bind"}:
            thread.join(timeout=3)


def test_manual_worker_rejects_foreign_result(manual, monkeypatch):
    foreign = open_ticket(TicketStore(), "webhook")
    monkeypatch.setattr(web, "invoke_agent", lambda *a, **k: {"sly_data": foreign, "text": "wrong result"})
    web.start_turn("manual incident", {}, True)
    deadline = time.monotonic() + 3
    while web._conversation["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not web._conversation["running"]
    assert not web._conversation["ticket_number"]
    assert "Error:" in web._conversation["messages"][-1]["content"]


def test_repair_action_requires_reviewed_ticket(manual, monkeypatch):
    called = []
    monkeypatch.setattr(web, "_current_ticket", lambda *a: called.append(a) or {})
    with pytest.raises(ValueError, match="ticket"):
        web.api_create_pr()
    assert called == []


def test_concurrent_fingerprint_append_uses_canonical_session(store, monkeypatch):
    barrier = threading.Barrier(2)
    monkeypatch.setattr(OpenTicket, "_mirror_to_servicenow", staticmethod(lambda *a: barrier.wait(timeout=5)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda session: open_ticket(store, session, "racing-alert"), ["first", "second"]))
    assert len(store.load()) == 1
    assert states[0] == states[1]
    assert ticket_notes.add_note(states[1], "canonical retry", store)


@pytest.mark.parametrize("foreign", [False, True])
def test_webhook_retains_owned_ticket_on_partial_or_foreign_result(manual, monkeypatch, foreign):
    wrong = open_ticket(TicketStore(), "manual")
    monkeypatch.setattr(receiver, "compose_incident_report", lambda alert: "fixture")
    monkeypatch.setattr(receiver, "invoke_agent", lambda *a, **k: {"text": "fixture", "sly_data": wrong if foreign else {}})
    monkeypatch.setattr(receiver, "_persist", lambda job: None)
    monkeypatch.setattr(receiver, "_job_slots", threading.BoundedSemaphore(1))
    receiver._job_slots.acquire()
    job = {"id": "webhook", "fingerprint": "alert", "alert_name": "test", "service": "checkout-service", "alert": {}}
    receiver._process_job(job)
    ticket = TicketStore().get(job["ticket_number"])
    assert ticket["session_id"] == "webhook"
    assert job["state"] == ("failed" if foreign else "completed")
    assert receiver._job_slots.acquire(blocking=False)


def test_merge_finalizes_reviewed_ticket_after_dashboard_changes(manual, monkeypatch):
    ticket = {"number": "INC1200001", "session_id": "first", "repair_pr_number": "7"}
    other = {"number": "INC1200002", "session_id": "second", "repair_pr_number": "8"}
    merged = []
    finalized = []
    monkeypatch.setattr(web, "_current_ticket", lambda *a: other if merged else ticket)
    monkeypatch.setattr(web, "_audit", lambda *a: None)
    monkeypatch.setattr(web, "_snapshot", lambda number: ({"number": number, "state": "open", "merged": bool(merged), "head": {"sha": "reviewed-full-sha"}}, [], "", None, []))
    monkeypatch.setattr(web.github_pr_client, "checks_passed", lambda checks: True)
    monkeypatch.setattr(web.github_pr_client, "merge_pull_request", lambda *a: merged.append(a) or {"merged": True})
    monkeypatch.setattr(web, "_finalize_merged_pr", lambda *a: finalized.append(a))
    assert web.api_merge(True, ticket["number"], 7, "reviewed-full-sha") == {"resolved": True}
    assert finalized == [(ticket, 7, "reviewed-full-sha")]


@pytest.mark.parametrize("pr,sha", [(8, "reviewed"), (7, "stale"), (None, None)])
def test_stale_review_never_merges(manual, monkeypatch, pr, sha):
    monkeypatch.setattr(web, "_current_ticket", lambda *a: {"number": "INC1200001", "repair_pr_number": "7"})
    monkeypatch.setattr(web, "_snapshot", lambda number: ({"state": "open", "head": {"sha": "reviewed"}}, [], "", None, []))
    monkeypatch.setattr(web.github_pr_client, "checks_passed", lambda checks: True)
    monkeypatch.setattr(web.github_pr_client, "merge_pull_request", lambda *a: pytest.fail("Stale review performed a remote write"))
    with pytest.raises(ValueError, match="review"):
        web.api_merge(True, "INC1200001", pr, sha)


def test_stale_ticket_selection_is_rejected(manual, monkeypatch):
    state = open_ticket(TicketStore(), "actual")
    monkeypatch.setattr(web, "build_state", lambda: {"ticket": {"number": state["sentinel_ticket_number"]}})
    with pytest.raises(ValueError, match="changed"):
        web._current_ticket("INC9999999")


def test_manual_and_webhook_workers_overlap_without_cross_binding(manual, monkeypatch):
    barrier = threading.Barrier(2)
    manual_opened = threading.Event()
    def invoke(text, **kwargs):
        state = dict(kwargs["sly_data"])
        OpenTicket().invoke({"service_name": "checkout-service", "short_description": text}, state)
        if text == "manual":
            manual_opened.set()
        barrier.wait(timeout=5)
        assert ticket_notes.add_note(state, f"worker-note:{text}")
        return {"text": text, "sly_data": state}
    monkeypatch.setattr(web, "invoke_agent", invoke)
    monkeypatch.setattr(receiver, "invoke_agent", invoke)
    monkeypatch.setattr(receiver, "compose_incident_report", lambda alert: "webhook")
    monkeypatch.setattr(receiver, "_persist", lambda job: None)
    monkeypatch.setattr(receiver, "_job_slots", threading.BoundedSemaphore(1))
    receiver._job_slots.acquire()
    web.start_turn("manual", {}, True)
    assert manual_opened.wait(timeout=3)
    job = {"id": "webhook", "fingerprint": "concurrent-alert", "alert_name": "test", "service": "checkout-service", "alert": {}}
    worker = threading.Thread(target=receiver._process_job, args=(job,))
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    for thread in threading.enumerate():
        if thread.name == "sentinel-turn":
            thread.join(timeout=3)
    assert not web._conversation["running"]
    assert job["state"] == "completed"
    assert web._conversation["ticket_number"] != job["ticket_number"]
    for label, number in [("manual", web._conversation["ticket_number"]), ("webhook", job["ticket_number"])]:
        ticket = TicketStore().get(number)
        notes = [note for note in ticket["work_notes"] if "worker-note:" in note]
        assert len(notes) == 1 and notes[0].endswith(f"worker-note:{label}")


@pytest.mark.parametrize("route,method,expected", [
    ("create", "api_create_pr", ("INC1200001",)),
    ("merge", "api_merge", (True, "INC1200001", 7, "full-sha")),
    ("finalize", "api_finalize", ("INC1200001", 7, "full-sha")),
])
def test_http_forwards_review_binding(manual, monkeypatch, route, method, expected):
    calls = []
    monkeypatch.setattr(web, method, lambda *args: calls.append(args) or {"ok": True})
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = requests.post(f"http://127.0.0.1:{server.server_port}/api/pr/{route}",
            headers={"X-Sentinel": "1"}, json={"ticket_number": "INC1200001", "pr_number": 7, "sha": "full-sha", "approved": True}, timeout=3)
        assert response.status_code == 200
        assert calls == [expected]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("source", ["manual", "webhook"])
def test_stale_persisted_pointer_does_not_enable_ticket_actions(manual, monkeypatch, source):
    foreign = open_ticket(TicketStore(), "foreign")
    monkeypatch.setattr(web, "_webhook_health", lambda: {"status": "disabled"})
    monkeypatch.setattr(web, "snapshot_jobs", lambda: [] if source == "manual" else [
        {"id": "other-job", "service": "checkout-service", "ticket_number": foreign["sentinel_ticket_number"], "state": "completed"}])
    if source == "manual":
        web._conversation.update(ticket_number=foreign["sentinel_ticket_number"], sly_data={"incident_session_id": "actual"}, messages=[{"role": "user", "content": "test"}])
    state = web.build_state()
    assert state["ticket"] is None
    assert state["repair"]["state"] == "none"
