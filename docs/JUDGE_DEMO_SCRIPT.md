# Live judge demo script

Rehearsed end-to-end on 2026-10-18 (PR #8 merged, ticket INC0010053 resolved). The stack is currently re-armed
and ready to go again. Two screens/tabs recommended: Sentinel UI (localhost:8501) and the checkout app
(localhost:8000). Keep github.com/Kvj-Harsha/checkout-demo-app and the ServiceNow incident list open in
background tabs to flip to after the merge.

## 0. Before judges arrive (2 min)

```powershell
.\.venv\Scripts\python.exe scripts\live_demo.py preflight   # everything must say [ok]
.\.venv\Scripts\python.exe scripts\live_demo.py status       # error rate: 0.00 errors/s
```

If it's been more than ~25 minutes since the last `prepare`/`deploy`, re-run so the deploy timestamp is fresh
(the SUPPORTED-verdict window is 30 minutes):

```powershell
.\.venv\Scripts\python.exe scripts\live_demo.py prepare --yes
```

## 1. Set up the story (30 sec)

Say: *"checkout-service just shipped a release. Watch what happens when it breaks — nothing here is scripted
or faked, it's a real Python service, a real Prometheus alert, and a real GitHub PR."*

Show the checkout app at localhost:8000 — healthy, quiet.

## 2. Break it, live (10 sec)

Click **"Inject checkout fault"** in the checkout app UI (or run `python scripts/live_demo.py fault --yes` in
a visible terminal). Say: *"That's it — I didn't touch Sentinel. The app is now generating its own shopper
traffic and some of it is failing."*

## 3. Let it find itself (~45–60 sec, narrate while it runs)

Switch to Sentinel (localhost:8501). Nothing to click — say: *"No one told Sentinel about this. Prometheus
will notice the error rate, Alertmanager will page it, and the ticket will just appear."*

Within about a minute a ticket opens by itself. Walk through the timeline live as it fills in:
- **Evidence** — point out the source badges flipping to `LIVE`: real Prometheus error rate, real Loki log
  lines (the exact `TypeError` and the failing country codes), the real GitHub Deployment it found, the real
  Confluence runbook it matched.
- **Verdict: SUPPORTED** — read the one sentence: deploy happened ~0.2 minutes before the incident, same
  service. Say: *"That's a deterministic check re-deriving the timestamps itself — not the model's opinion."*
- **Fix: validated** — the FixAgent's proposed patch to `app/pricing.py` and the 7/7 sandbox checks list.
  Say: *"It just wrote a code fix, and before I even see it, a sandbox already proved it reproduces the bug on
  the original code and fixes it, without touching the markets that already worked."*

## 4. Open the real PR (10 sec + wait for CI)

Click **Prepare repair PR**. Say: *"That's a real GitHub issue and PR going up right now."* Switch to the
github.com tab, show the real diff and the real Actions check running.

## 5. The human gate (this is the point of the whole system)

Back in Sentinel: read the diff and the checkbox copy out loud — *"I reviewed this diff and the GitHub Actions
results, and I approve merging this change."* Check it, click **Approve and merge repair**. Say: *"Nothing
merges without that click — there's no auto-merge path in this system."*

## 6. Prove it actually fixed something (15 sec)

Switch back to the checkout app / run `python scripts/live_demo.py status` — error rate back to 0.00, probe
200. Say: *"That's a live HTTP order succeeding against the real app, not a log message claiming success."*

## 7. Close the loop (10 sec)

Show the ticket flipping to Resolved, and click the **Open in ServiceNow** link — real incident, same close
notes citing the merge. *"And that's a second system of record, synced automatically, with no extra step from
me."*

## If something doesn't fire in time

- Alert hasn't fired after ~90s: check `python scripts/live_demo.py status` for the error rate; if it's low,
  run `python scripts/live_demo.py load --yes` as a traffic booster.
- Have the rehearsed PR #8 / ticket INC0010053 bookmarked as a labeled fallback — say explicitly "here's the
  run we did earlier today" rather than presenting a replay as live.

## After judging: reset for the next run

```powershell
.\.venv\Scripts\python.exe scripts\live_demo.py reset-repo --yes
.\.venv\Scripts\python.exe scripts\live_demo.py prepare --yes
```
