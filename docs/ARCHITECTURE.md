# Architecture

## Idea

Triage agents fail when an LLM confidently retells a cause nobody verified. Sentinel splits the work:

| LLM agents (route + explain) | Coded tools (decide) |
|---|---|
| Choose who to ask, summarize, write the answer | Fetch evidence, label its source, re-derive timestamps, grade the hypothesis, open and update tickets, prepare the fix |

## Flow

1. **Alert in.** `apps/sentinel/alerts.py` turns a Datadog-shaped or Alertmanager alert into an incident report (no LLM).
2. **Responder** opens exactly one ticket and recalls prior incidents (idempotent per conversation and alert fingerprint).
3. **Evidence agents** (Logs, Metrics, DeployHistory, Runbook) each call one tool. Tools publish normalized
   `EvidenceRecord`s into `sly_data["sentinel_verified"]` (private channel, never shown to an LLM).
4. **Scribe** calls `correlation_check`: same service, deploy before incident, within 30 minutes, built only from published
   evidence. Missing evidence gives `NOT_SUPPORTED` with `insufficient_evidence`, never a guess.
5. **Responder finalizes** via `finalize_incident`: records memory; if `SUPPORTED`, prepares the allow-listed fix and runs
   local preflight. A hotfix suggestion only comes from a past `SUPPORTED` incident.
6. **Human gate** (Streamlit): create issue + PR, show diff and CI checks, approve and merge, verify recovery, resolve.

## neuro-san conventions used

- Network, tool routing, middleware (`PIIMiddleware`) and the `allow.to_upstream.sly_data` list live in
  `registries/sentinel.hocon`; `manifest.hocon` registers it.
- Coded tools extend `CodedTool` in `coded_tools/sentinel/` and share state only through `sly_data`.
- AAOSA instructions are included, not forked. `apps/sentinel/run.py` is a thin bootstrap over `DirectAgentSessionFactory`;
  it holds no orchestration logic.
- Fallback LLM chain in `registries/llm_config.hocon`; `max_execution_seconds` bounds every run.

## Modes

`SENTINEL_MODE=demo` (default) replays `coded_tools/sentinel/data/*.json` (`fixture`); `live` queries Prometheus, Loki,
GitHub Deployments and Confluence. See `coded_tools/sentinel/mode.py`.

## Known limits

- Runbook search is keyword-based. Fixture scenarios cover `checkout-service` end to end; other services replay deploys/logs only.
- Live mode needs a real GitHub Deployment record and a fresh fault to reach `SUPPORTED`.
