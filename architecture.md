# Sentinel — Architecture

## What we are trying to do

Production incidents get triaged by an engineer who opens a monitoring alert, greps logs, checks a metrics dashboard,
scrolls the deploy history, searches a runbook wiki, and only then writes down a root cause. An LLM asked to do that
same job end-to-end will happily skip steps and *confidently state a cause it never actually checked* — logs it
paraphrased from memory, a deploy time it rounded, a runbook it half-remembers. That failure mode is worse than no
automation at all, because a wrong but confident ticket note gets trusted.

Sentinel is an incident-triage agent network built on Cognizant's [neuro-san](https://github.com/cognizant-ai-lab/neuro-san)
framework that refuses to make that mistake. It automates the *legwork* of triage — gathering logs, metrics, deploy
history and runbook evidence, drafting a hypothesis, opening a ticket, and proposing a bounded code fix — while keeping
the one step that must never be guessed, **"is this hypothesis actually supported by the evidence?"**, behind a piece of
deterministic Python that the LLM cannot talk its way around.

## How it achieves that: LLMs route and explain, coded tools decide

Sentinel's core design principle is a hard separation of responsibility:

| What LLM agents do | What coded tools do |
|---|---|
| Decide **who** to ask and **in what order** | Fetch evidence from its actual source (fixture or live) |
| Turn tool output into **readable summaries** | Label every record `fixture`, `live`, or `unavailable` |
| Draft a root-cause **hypothesis** in English | **Re-derive timestamps from source data** and verify the hypothesis is supported — the LLM is never trusted to relay its own math |
| Explain the verdict to the human | Open/update the ticket, record incident memory, run sandboxed fix verification, decide `SUPPORTED` / `NOT_SUPPORTED` |

No agent is ever allowed to state a verified root cause without first calling `correlation_check`, a plain Python
function that pulls the incident and deploy timestamps straight from the evidence tools published and checks the
timing itself (same service, deploy before the incident, within a 30-minute window). If evidence is missing, the
verdict is `NOT_SUPPORTED` with `insufficient_evidence` — never a plausible-sounding guess.

## The agentic system

Sentinel is a **network of eight cooperating agents** (one front-man, six specialists, and a verification/evaluation
loop), declared declaratively in [`registries/sentinel.hocon`](registries/sentinel.hocon) using neuro-san's AAOSA
(Agent-As-Orchestrator/Agent-As-Specialist) convention — no orchestration logic is written in Python; the HOCON file
*is* the org chart.

![Sentinel agent network architecture](docs/diagrams/architecture.png)

- **IncidentCommander** — the front-man. It receives the incident report, is PII-guardrailed (two `PIIMiddleware`
  passes redact emails and credit-card numbers from everything going in or out), and drives a fixed five-step
  protocol: open the ticket and recall history → fan out to all four evidence agents → hand the draft hypothesis to
  the Scribe → if (and only if) `SUPPORTED`, hand the failing code to FixAgent → finalize and answer. It is
  instructed to never move to the next step until the current one is complete, and to copy the final report block
  from tools **verbatim** rather than paraphrase it — a paraphrased ticket number or verdict is exactly the kind of
  drift this system exists to prevent.
- **Responder** — owns the operational record. Opens exactly one ticket per incident (idempotent per conversation
  and alert fingerprint), recalls prior incidents and any previously suggested hotfix, and later finalizes the
  incident and updates the ticket. It can never resolve a ticket itself — only a human merging a PR does that.
- **LogsAgent / MetricsAgent / DeployHistoryAgent / RunbookAgent** — four narrow specialists, each wired to exactly
  one evidence-gathering tool (`loki_logs`, `prometheus_metrics`, `github_deployments`/`deploy_lookup`,
  `runbook_search`). Each is instructed to report retrieval only, never a root cause, and to copy the exact
  `service_name` and alert timestamp character-for-character — a slightly-wrong value silently returns no evidence.
- **Scribe** — the evaluation loop. Takes the Commander's draft hypothesis and all gathered evidence, calls
  `correlation_check` (passing only `service_name` and the hypothesis sentence — **never** timestamps, which the
  tool re-derives itself from source data), and writes the verdict-labelled summary that the rest of the system is
  required to trust.
- **FixAgent** — only invoked after a `SUPPORTED` verdict. Reads the one allow-listed source file, writes the
  smallest fix for the logged failing markets, and submits it through `submit_code_fix`, which runs it through a
  sandbox (size/syntax/safe-import checks, public-API preservation, that previously-working markets stay working)
  before it can become a pull request.
- **`finalize_incident`** (deterministic close-out, not an LLM step) — records incident memory and, only for
  `SUPPORTED`, prepares the bounded fix and runs local preflight. The ticket stays **In Progress**; nothing in the
  agent network can resolve it.

The sequence below shows one investigation end-to-end, including the human approval gate that sits outside the agent
network entirely:

![Sentinel investigation sequence](docs/diagrams/sequence.png)

### Why a network instead of one prompt

A single large prompt asked to "triage this and tell me the cause" collapses retrieval and judgment into the same
non-deterministic step. Splitting it into narrow specialist agents means:

1. **Each tool call is auditable** — every piece of evidence keeps its source status (`fixture`/`live`/`unavailable`)
   and record IDs all the way to the final ticket note, instead of being flattened into prose.
2. **The one step that must be correct is not an LLM step.** `correlation_check` is ordinary Python; it is as
   deterministic and testable as any other function in the codebase (see `tests/`), unlike the agents around it.
3. **The blast radius of a bad LLM turn is small.** If LogsAgent hallucinates a log line, the Scribe's
   `correlation_check` call still only sees timestamps the tool itself re-derived from the *actual* published
   evidence — a hallucinated log excerpt cannot change the verdict.
4. **The repair step is gated, sandboxed and reversible.** FixAgent can only act after a verified `SUPPORTED`
   verdict, only on one allow-listed file, only through a tool that runs the diff through safety checks before it
   becomes a pull request — and even then a human must review and merge it. No agent, and no human-provided prompt,
   can make Sentinel merge code on its own.

## Modes: demo vs. live

`SENTINEL_MODE=demo` (default) makes every evidence tool replay bundled JSON fixtures from
`coded_tools/sentinel/data/`, so the full agent network runs reproducibly offline (only the LLM calls hit a real
API). `SENTINEL_MODE=live` switches the same tools to real sources — Prometheus, Grafana Loki, GitHub Deployments,
and Confluence — with no change to the agent network or its instructions; only the coded tool implementations differ.
See `coded_tools/sentinel/mode.py` and the mode table in [README.md](README.md).

## Diagram sources

Both diagrams are generated from LaTeX/TikZ sources checked into [`docs/diagrams/`](docs/diagrams/)
(`architecture.tex`, `sequence.tex`) and rendered to PDF/PNG with `pdflatex` + `pdftoppm`. They are regenerated by
hand whenever the agent network in `registries/sentinel.hocon` changes shape.

## Further reading

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — a shorter internal reference with the step-by-step flow and
  neuro-san conventions used.
- [`registries/sentinel.hocon`](registries/sentinel.hocon) — the single source of truth for every agent, its
  instructions, and its tool wiring.
- [`coded_tools/sentinel/`](coded_tools/sentinel/) — the deterministic Python behind every tool call.
- [`summary.md`](summary.md) — a project-level summary of what Sentinel does and why.
