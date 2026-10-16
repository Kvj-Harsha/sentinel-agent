# Project Summary

## What We Built

When a production alert fires, the first critical minutes of an on-call engineer's time go into manual correlation:
pull the error logs, check what deployed recently, search old runbooks, remember whether this has happened before -
all while under pressure. **Sentinel** automates the investigation, then keeps a human in control of the repair.
A team of cooperating AI agents, built on Cognizant's open-source **neuro-san** framework and declared in HOCON,
takes a monitoring alert and gathers evidence. The current canonical investigation uses synthetic JSON fixtures for
logs, deploys, runbooks, incident memory and local tickets; its root-cause hypothesis is checked deterministically.
When configured, Sentinel can mirror the ticket to ServiceNow and create/review a GitHub issue and pull request for
the isolated checkout demo. A human approves and merges; Sentinel applies only an allow-listed setting to the local
demo app, verifies recovery, and then resolves the ticket.

---

## The Problem

1. **Slow, error-prone correlation.** Logs say *when* something broke, deploy history says *what* changed, runbooks say
   *what it looked like before*; only combining all three gives a trustworthy conclusion, and humans do it under stress.
2. **Confident-sounding AI guesses.** A single LLM asked "why is checkout failing?" will happily invent a plausible
   cause. An on-call tool that cannot tell a supported cause from a coincidence is worse than none.
3. **Demos that are not products.** Real incidents arrive as *structured alerts* and live in *tickets*, not in a chat box.

---

## How It Works

Incidents arrive through a Streamlit sample/free-text report or a structured webhook payload. The local Prometheus /
Alertmanager stack can send a firing alert to Sentinel. `backend/alerts.py` composes the incident report
**deterministically, with no LLM**, and hands it to a network of eight agents declared in one HOCON file:

- **IncidentCommander** (front man) orchestrates the others at runtime via neuro-san's AAOSA protocol; PII middleware
  redacts accidentally pasted emails and card numbers before any agent or the LLM sees them.
- **HistorianAgent** is persistent memory: it recalls prior incidents and returns the **known hotfix to reapply**
  (not just "this happened before"), and records the new incident.
- **TicketAgent** opens one local JSON ticket per incident and can mirror it to a **ServiceNow incident** when credentials are set, with caller,
  assignment group and assignee). The ticket stays open while repair awaits human review. Work notes are written from tool-verified facts, never from the
  model's free text (in testing the model invented a "database connectivity" cause).
- **RemediationAgent** prepares a proposed config patch and local preflight results. On **Prepare repair PR**, Sentinel
  creates a GitHub issue first, then opens a PR linked with `Closes #N` for the mock checkout app. It shows the issue,
  real diff and GitHub Actions checks, and waits for a human to approve and merge. GitHub closes the issue on merge.
  The merged settings are applied only to the localhost demo app; its checkout endpoint is then verified before the
  ServiceNow incident is resolved.
- **LogsAgent / DeployHistoryAgent / RunbookAgent** are independent specialists. Logs come from bounded local Loki,
  and checkout-service deployment evidence comes from explicit GitHub Deployment records and statuses. Other service
  deploy history and Confluence-shaped runbooks remain clearly labeled fixtures. Commit history remains context only;
  a commit or build run is not proof of deployment.
- **Scribe** writes the summary - but only after `correlation_check`, a plain-Python tool that **re-derives the
  incident and deploy timestamps from source data itself** and checks that the deploy came shortly *before* the anomaly on
  the *same* service. If it does not line up, the verdict is `NOT_SUPPORTED` and the answer must say so.

The key idea: **the LLMs route and explain; deterministic tools decide.** Tools publish what they found into
`sly_data`, and the UI shows a **"Verified by tools"** card (verdict, runbook, hotfix) next to the ticket. The ticket
state and the memory write are enforced in code too, so a model that forgets or repeats a tool call cannot leave the
system wrong.

![Architecture](docs/diagrams/architecture.png)

---

## Engineering Rigor

- **Recorded evaluation baseline:** the checked-in `eval/_reports/report.md` shows **12/13 scenarios passed** in the
  last recorded live run, with a miss on `payments-gateway-paraphrase` (expected `SUPPORTED`, actual
  `NOT_SUPPORTED`). The report grades deterministic tool outputs, not prose. This is a historical report, not a run
  performed as part of Step 0. The repository also records 27 offline tool tests; they were not rerun in this baseline review.
- **Bugs the harness and live runs caught (all fixed):** the Scribe garbling timestamps relayed across agent hops
  (so `correlation_check` re-derives them itself); service-name drift (`payment-gateway` vs `payments-gateway`) silently
  breaking an exact-match lookup (now canonicalized in the tool layer, not just warned about in the prompt); a free-text log query that required the whole phrase to match; four duplicate tickets
  for one incident (now idempotent per conversation via `sly_data`); the model's prose citing a **wrong runbook id**
  while the tools were right (hence the verified-facts card); a weaker second `correlation_check` call overwriting a SUPPORTED verdict (now sticky); parallel agents
  overwriting each other's ticket notes (now atomic under a lock); the model writing invented causes into ticket
  notes (now written from verified facts); and a Windows console crash on a Unicode hyphen that failed
  every scenario until the harness forced UTF-8.
- **Resilience and guardrails:** two-model LLM fallback chain, bounded `max_steps`/`max_execution_seconds`, PII redaction,
  offline fallback for the one live external call.

---

## Technology Choices

| Concern | Choice | Reason |
|---------|--------|--------|
| Agent orchestration | neuro-san 0.7.7 | Declarative HOCON networks, AAOSA routing, `CodedTool` extensions, middleware, LLM fallbacks, `sly_data` |
| LLM | OpenAI `gpt-4o-mini` (+ `gpt-4.1-mini` fallback) | Cheap, fast, good tool-calling; any OpenAI-compatible provider works via env vars |
| Frontend | Streamlit | Chat + alert buttons + ticket panel in ~130 lines |
| Verification | Deterministic Python tools | Facts that must be right (timing, ticket state, memory) never depend on an LLM's retelling |
| Data | Synthetic checkout app and evidence fixtures; optional GitHub/ServiceNow APIs | Local demo stays reproducible; external ticket/PR paths can be verified when configured |

---

## Illustrative fixture-backed interaction

The following is a representative local-fixture scenario. A ServiceNow record, GitHub PR, and real Actions checks exist
only when their respective external integrations are configured; the logs, deploy, and runbook values below are
synthetic demo evidence.

```
Alert fired:  [Triggered] High 5xx rate on checkout-service ... NullPointerException on PaymentValidator.charge()
              since 14:02 UTC on 2026-10-12

ServiceNow:   INC0010008 opened immediately (link shown in the UI) - caller admin, group Software, assigned Beth Anglin
Work notes:   [Deploys]       v2.14.0 deployed 13:58 by a.kumar (new tax-calc-lib v3)
              [Logs]          841 errors from 14:02; NullPointerException at PaymentValidator.java:88 (trace 4bf92f35...)
              [Runbook]       RB-042 (Confluence) - tax library deploy without config migration
              [Root cause]    Correlation SUPPORTED - deploy 4.0 min before the incident, same service
              [Remediation]   config diff: tax.fallback_to_legacy_table -> true, tax.null_rate_guard -> true
              [GitHub]        PR #12 opened; actual Actions checks passed
              [Human review]  diff reviewed and merged from the Streamlit approval panel
              [Recovery]      localhost checkout returned HTTP 200 after merged settings synced
Ticket:       INC0010008 - Resolved only after human approval and recovery verification

Follow-up:    "Which deploy caused this and who authored it?"  ->  v2.14.0 by a.kumar (same ticket, no duplicate)
```

---

## Limitations and Future Work

- **ServiceNow is live when configured** (Table API: real incident, caller, assignment group, assignee, work notes,
  resolution); without credentials it falls back to a local store. **Confluence is shape-only** (synthetic pages).
- The storefront and its traffic are synthetic and local. Prometheus scraping and GitHub PR/Actions calls are real
  when configured. Sentinel is restricted to the demo app's allow-listed settings file; its merge control requires
  successful checks and explicit human approval.
- An alert that names no service at all can make the model guess a service name that has no data; the tools then
  decline to confirm a cause (NOT_SUPPORTED) rather than guess, but the incident is not triaged. Fuzzy name matching only
  repairs near-misses (`payment-gateway`), not invented names.
- Runbook retrieval is keyword-based, not semantic; vague descriptions can miss a real match.
- The LLM's final prose can still paraphrase imperfectly (see the wrong-runbook-id bug above); the verified card, not the
  prose, is the source of truth, and the harness grades tool outputs for that reason.
- GitHub issue/PR creation, diff retrieval, check status and merge require a fine-grained token scoped to the demo app
  repository with Issues and Pull requests read/write, Contents read/write, and Checks read. The deployment lookup is
  read-only; it uses explicit GitHub Deployment records, while commit history remains context only.
- Next: live Confluence retrieval, semantic runbook search, multi-tenant memory, tracing dashboards.
