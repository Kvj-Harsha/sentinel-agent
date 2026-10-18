# Sentinel — Project Summary

## The problem

When a production alert fires, the useful work is mostly rote: pull the error logs, check the error-rate metric,
look up what deployed recently, search the runbook wiki, and connect the dots into a hypothesis. It is also exactly
the kind of work an LLM is tempted to shortcut — producing a confident, plausible-sounding root cause that nobody
actually checked against the evidence. A wrong ticket note that *looks* verified is more dangerous than an empty one,
because an on-call engineer will trust it and chase the wrong fix.

## What Sentinel does

Sentinel is an incident-triage **agent network**, built on Cognizant's [neuro-san](https://github.com/cognizant-ai-lab/neuro-san)
framework, that automates the legwork of triage while refusing to automate the guessing. Given an alert (service,
symptom, timestamp), it:

1. Opens exactly one ticket and recalls whether this service has had a similar incident before.
2. Gathers four kinds of evidence in parallel — error logs, error-rate metrics, deployment history and the best-matching
   runbook — each labelled with where it actually came from (`fixture`, `live`, or `unavailable`).
3. Drafts a root-cause hypothesis and runs it through a **deterministic correlation check** that independently
   re-derives the incident and deploy timestamps from the evidence itself and verifies the timing actually supports
   the hypothesis (same service, deploy before the incident, within a 30-minute window).
4. Only if that check returns `SUPPORTED` does it attempt a fix: it reads the one allow-listed source file, writes
   the smallest correction, and runs it through a sandbox that checks syntax, safe imports, and that previously-working
   behavior is preserved — before opening it as a pull request.
5. Finalizes the incident (records it to memory for next time) and hands a human a ticket, a verdict with its
   reasoning, and — if applicable — a reviewable diff. The ticket stays **In Progress** until a person reviews and
   merges the PR; Sentinel never auto-merges, never auto-resolves, and never claims a fix is deployed.

If the evidence does not support a cause, Sentinel says `NOT_SUPPORTED` and explains what is missing, rather than
inventing one. That is the behavior the whole system is built around.

## How the agentic system achieves this

Sentinel splits the work across **eight cooperating agents** declared in a single HOCON registry
(`registries/sentinel.hocon`), using neuro-san's AAOSA (Agent-As-Orchestrator/Agent-As-Specialist) pattern — the
network's shape *is* configuration, not Python control flow:

- **IncidentCommander** is the PII-guardrailed front-man that drives a fixed five-step protocol and is required to
  copy tool-produced facts into its final answer verbatim, never paraphrased.
- **Responder** owns the ticket and incident memory.
- **LogsAgent, MetricsAgent, DeployHistoryAgent, RunbookAgent** are narrow specialists, each bound to exactly one
  evidence tool, instructed to retrieve and report — never to conclude a cause.
- **Scribe** is the evaluation loop: it cannot state a verdict without calling the deterministic `correlation_check`
  tool, and it is never allowed to pass that tool its own timestamps — the tool re-derives them from the evidence
  the specialists actually published, so a hallucinated detail from any upstream agent cannot leak into the verdict.
- **FixAgent** only runs after a verified `SUPPORTED` outcome, is scoped to one file, and its output must pass
  sandboxed checks before it can become a PR.

This separation — **LLM agents route and explain; coded Python tools decide** — is the project's central idea. The
one judgment that must be reliable (is this hypothesis actually true?) is implemented as an ordinary, testable
Python function instead of being left to an LLM's self-report. Every other step benefits from LLM flexibility
(summarizing logs, drafting prose, deciding which specialist to call) without being trusted with the one decision
that has to be right.

## Two operating modes

By default Sentinel runs in **demo mode**: all evidence tools replay bundled JSON fixtures, so the complete agent
network — routing, tool calls, the correlation check, ticket creation — runs reproducibly offline and is safe to
demo repeatedly (only the LLM calls themselves hit a real API and may incur cost). Setting `SENTINEL_MODE=live`
switches the same tools to real Prometheus, Grafana Loki, GitHub Deployments and Confluence sources, with zero
change to the agents or their instructions — only the coded tool implementations differ.

## Safety model

- Fixed, allow-listed repair scope (one file); the PR's head SHA and CI checks are re-verified before any merge
  path proceeds; there is no auto-merge.
- PII (emails, credit-card numbers) is redacted on the front-man before any agent or LLM sees it.
- Secrets live only in a git-ignored `.env`; fixtures are synthetic.
- Ticket mutations require the private incident session to own the ticket, so a fingerprint retry or a stray request
  cannot hijack another investigation's ticket.

## What's included in this repo

- `registries/` — the declarative agent network (HOCON).
- `coded_tools/sentinel/` — every deterministic tool: evidence fetchers, the correlation check, ticket store,
  incident memory, sandboxed fix verification, and the demo/live fixture switch.
- `apps/sentinel/` — the native web UI and the neuro-san bootstrap (`run.py`) that starts the network; no
  orchestration logic lives here, only wiring.
- `extras/` — the optional Docker/Prometheus/Alertmanager/Loki stack for a full live-mode local demo.
- `tests/` — an offline pytest suite (no LLM, no network) covering the deterministic tools.
- `eval/` — opt-in live-LLM evaluation scenarios.
- `docs/` — detailed architecture notes and the diagram sources; see [`architecture.md`](architecture.md) for the
  full write-up and [`README.md`](README.md) for setup and run instructions.
