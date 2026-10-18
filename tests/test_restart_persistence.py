"""Real process boundaries over isolated SQLite, with no model or remote writes."""

import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from coded_tools.sentinel.ticket_store import TicketStore


ROOT = Path(__file__).resolve().parents[1]
SEED = '''
import json,sys
from coded_tools.sentinel.open_ticket import OpenTicket
from coded_tools.sentinel.ticket_store import TicketStore
from coded_tools.sentinel.sqlite_store import SQLiteDatabase
import conversation_store
mode=sys.argv[1]
state={'alert_fingerprint':'restart-fixture-fingerprint','incident_session_id':'restart-fixture-session'}
ticket=OpenTicket().invoke({'service_name':'checkout-service','short_description':'Fixture restart check'},state)
assert isinstance(ticket,dict)
if mode=='resolved': TicketStore().update(ticket['number'],{'state':'Resolved'})
conversation_store.persist('restart-fixture-conversation',{'messages':[{'role':'user','content':'Fixture incident'}],
    'chat_context':{},'sly_data':state,'ticket_number':ticket['number'], 'running':mode=='interrupted'})
SQLiteDatabase().save_alert_job({'id':'restart-fixture-job','state':'completed',
    'fingerprint':state['alert_fingerprint'],'ticket_number':ticket['number']})
print(json.dumps({'ticket':ticket['number']}))
'''
RESTORE = '''
import json,sys
import alertmanager_receiver as receiver
import conversation_store
from coded_tools.sentinel.ticket_store import TicketStore
from coded_tools.sentinel.sqlite_store import SQLiteDatabase
assert not receiver._jobs and not receiver._fingerprints
def forbidden(*args,**kwargs): raise AssertionError('Duplicate started a worker/model')
receiver.invoke_agent=forbidden
receiver.threading.Thread=forbidden
reply=receiver._queue_alert({'fingerprint':'restart-fixture-fingerprint'})
assert reply['status']=='duplicate'
assert len(TicketStore().load())==1
jobs=receiver.snapshot_jobs()
assert len(jobs)==1 and jobs[0]['state']=='completed' and jobs[0]['ticket_number']==reply['ticket_number']
restored=conversation_store.restore_latest(interrupt_running=True)
assert restored['id']=='restart-fixture-conversation' and not restored['running']
assert restored['ticket_number']==reply['ticket_number']
if sys.argv[1]=='interrupted':
    assert 'interrupted' in restored['messages'][-1]['content']
    assert len(conversation_store.restore_latest(interrupt_running=True)['messages'])==2
else: assert len(restored['messages'])==1
with SQLiteDatabase().connection() as c:
    assert c.execute('select count(*) from alert_jobs').fetchone()[0]==1
print(json.dumps({'ticket':reply['ticket_number'],'duplicate':True,'conversation_restored':True}))
'''


def child(program, database, mode):
    env = dict(os.environ)
    env.update({"PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "apps/sentinel"))),
                "SENTINEL_STORAGE_BACKEND": "sqlite", "SENTINEL_DB_PATH": str(database),
                "SENTINEL_SEED_LEGACY_DATA": "false", "SENTINEL_DEDUP_WINDOW_SECONDS": "300",
                "SERVICENOW_ENABLED": "false", "SERVICENOW_INSTANCE": "", "SERVICENOW_USER": "",
                "SERVICENOW_PASSWORD": "", "SENTINEL_GITHUB_TOKEN": "", "SENTINEL_AUTO_PR": "false",
                "OPENAI_API_KEY": "fixture-key", "OPENROUTER_API_KEY": "fixture-key",
                "OPENAI_API_BASE": "https://example.invalid/v1", "SENTINEL_RECEIVER_ENABLED": "false"})
    result = subprocess.run([sys.executable, "-c", program, mode], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("mode", ["idle", "interrupted"])
def test_fresh_process_restores_state_and_suppresses_duplicate(tmp_path, mode):
    database = tmp_path / "restart.db"
    before = child(SEED, database, mode)
    after = child(RESTORE, database, mode)
    assert before["ticket"] == after["ticket"]
    assert after["duplicate"] and after["conversation_restored"]


def test_resolved_fingerprint_allows_new_incident_in_fresh_process(tmp_path):
    database = tmp_path / "restart.db"
    before = child(SEED, database, "resolved")
    program = '''
import json
from coded_tools.sentinel.open_ticket import OpenTicket
from coded_tools.sentinel.ticket_store import TicketStore
state={'alert_fingerprint':'restart-fixture-fingerprint','incident_session_id':'new-fixture-session'}
ticket=OpenTicket().invoke({'service_name':'checkout-service','short_description':'New fixture episode'},state)
assert len(TicketStore().load())==2
print(json.dumps({'ticket':ticket['number']}))
'''
    after = child(program, database, "resolved")
    assert after["ticket"] != before["ticket"]


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_append_deduplicates_open_episode_but_not_resolved_ticket(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("SENTINEL_SEED_LEGACY_DATA", "false")
    store = (TicketStore(data_file=tmp_path / "tickets.json") if backend == "json"
             else TicketStore(backend="sqlite", db_path=tmp_path / "tickets.db"))
    base = {"service_name": "checkout-service", "alert_fingerprint": "fixture-append-fingerprint",
            "sys_created_on": datetime.now(timezone.utc).isoformat(), "state": "In Progress", "work_notes": []}
    first = store.append({**base, "number": store.next_number()})
    store.update(first["number"], {"state": "Resolved"})
    second = store.append({**base, "number": store.next_number()})
    assert second["number"] != first["number"]
    assert len(store.load()) == 2
    retry = store.append({**base, "number": store.next_number()})
    assert retry["number"] == second["number"]
    assert len(store.load()) == 2
