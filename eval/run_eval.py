"""
Automated evaluation harness for the Sentinel agent network.

Drives each scenario in eval/scenarios.json through the real agent network using neuro-san's own
CLI client (`python -m neuro_san.client.agent_cli --direct --one_shot`, in-process, no server), then
asserts correctness by reading the *deterministic* coded-tool outputs from the run's thinking-dir
trace (correlation_check's verdict, runbook_search's top match, open_ticket's number,
recall_incidents' suggested_hotfix) rather than regexing the LLM's prose -- the tools are reliable,
the LLM's retelling of them is not, so grade on the former. (Whether the prose also repeats the
hotfix is reported for information only.)

Runtime state the network writes (tickets, incident history) is snapshotted before the run and
restored afterwards, so running the battery leaves the working tree clean.

Run with: python eval/run_eval.py   (needs OPENAI_API_KEY in .env or the environment)
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
DATA_DIR = BACKEND_DIR / "coded_tools" / "sentinel" / "data"
RUNTIME_STATE_FILES = [DATA_DIR / "tickets.json", DATA_DIR / "incident_history.json"]
NETWORK_NAME = "sentinel"
SCENARIO_TIMEOUT_SECONDS = 240

# Matches a "Got result:\n```json\n{...}\n```" block in a thinking-dir trace file, as
# written by neuro_san's AgentCli for each coded-tool invocation.
GOT_RESULT_PATTERN = re.compile(r"Got result:\s*```json\s*(\{.*?\})\s*```", re.DOTALL)
# The CLI prints the answer after a "Response from <front-man agent>:" line.
FINAL_RESPONSE_PATTERN = re.compile(r"Response from [^\n:]+:\s*\n(.*)", re.DOTALL)


@dataclass
class ScenarioResult:  # pylint: disable=too-many-instance-attributes
    """Outcome of running one eval scenario against the live agent network."""

    scenario_id: str
    prompt: str
    expected_verdict: str
    actual_verdict: Optional[str]
    expected_runbook_id: Optional[str]
    actual_runbook_id: Optional[str]
    runbook_checked: bool
    ticket_checked: bool
    actual_ticket_number: Optional[str]
    hotfix_checked: bool
    actual_suggested_hotfix: Optional[str]
    passed: bool
    duration_seconds: float
    final_response: str = ""
    error: str = ""
    leaked_substrings: List[str] = field(default_factory=list)
    hotfix_surfaced: bool = False


class EvalRunner:
    """Runs every scenario in a scenarios file against a live neuro-san agent network."""

    def __init__(self, scenarios_path: Path, thinking_root: Path) -> None:
        """
        :param scenarios_path: path to a JSON file of scenario definitions.
        :param thinking_root: directory under which each scenario's thinking-dir trace
            is written (each scenario gets its own subdirectory, auto-cleared by neuro-san).
        """
        self.scenarios_path = scenarios_path
        self.thinking_root = thinking_root
        self.thinking_root.mkdir(parents=True, exist_ok=True)

    def load_scenarios(self) -> List[Dict[str, Any]]:
        """Reads and parses the scenarios JSON file."""
        return json.loads(self.scenarios_path.read_text(encoding="utf-8"))

    def run_all(self) -> List[ScenarioResult]:
        """Runs every scenario and returns its result, in order."""
        snapshot = {path: path.read_bytes() for path in RUNTIME_STATE_FILES if path.exists()}
        results: List[ScenarioResult] = []
        try:
            for scenario in self.load_scenarios():
                logger.info("Running scenario: %s", scenario["id"])
                results.append(self._run_scenario(scenario))
        finally:
            for path, content in snapshot.items():
                path.write_bytes(content)
        return results

    def _run_scenario(self, scenario: Dict[str, Any]) -> ScenarioResult:  # pylint: disable=too-many-locals
        """Runs a single scenario end-to-end and grades it."""
        scenario_id: str = scenario["id"]
        prompt_file = self.thinking_root / f"{scenario_id}_prompt.txt"
        prompt_file.write_text(scenario["prompt"], encoding="utf-8")
        thinking_file = self.thinking_root / f"{scenario_id}.txt"
        thinking_dir = self.thinking_root / scenario_id

        start = time.monotonic()
        # Force UTF-8 stdio in the subprocess: on Windows, Python's default console
        # encoding (cp1252) can't encode some punctuation an LLM occasionally emits (e.g. a
        # non-breaking hyphen), which otherwise crashes the CLI with a UnicodeEncodeError
        # before any tool output is produced -- a real failure caught by running this
        # battery live, not a hypothetical one.
        child_env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            # The eval must not create ~13 real incidents on a live ServiceNow instance.
            "SERVICENOW_INSTANCE": "",
            "AGENT_MANIFEST_FILE": str(BACKEND_DIR / "registries" / "manifest.hocon"),
            "AGENT_TOOL_PATH": str(BACKEND_DIR / "coded_tools"),
            "PYTHONPATH": os.pathsep.join(filter(None, [str(BACKEND_DIR), os.environ.get("PYTHONPATH", "")])),
        }
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "neuro_san.client.agent_cli",
                    "--agent",
                    NETWORK_NAME,
                    "--direct",
                    "--one_shot",
                    "--first_prompt_file",
                    str(prompt_file),
                    "--thinking_file",
                    str(thinking_file),
                    "--maximal",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                timeout=SCENARIO_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._build_result(scenario, actual_verdict=None, actual_runbook_id=None,
                                       actual_ticket_number=None, actual_suggested_hotfix=None,
                                       duration=time.monotonic() - start,
                                       error=f"Timed out after {SCENARIO_TIMEOUT_SECONDS}s")

        duration = time.monotonic() - start
        if completed.returncode != 0:
            error = f"CLI exited {completed.returncode}: {completed.stderr[-500:]}"
            return self._build_result(scenario, actual_verdict=None, actual_runbook_id=None,
                                       actual_ticket_number=None, actual_suggested_hotfix=None,
                                       duration=duration, error=error)

        actual_verdict = self._read_correlation_verdict(thinking_dir)
        actual_runbook_id = self._read_top_runbook_id(thinking_dir)
        actual_ticket_number = self._read_ticket_number(thinking_dir)
        actual_suggested_hotfix = self._read_suggested_hotfix(thinking_dir)
        final_response = self._extract_final_response(completed.stdout)
        leaked = self._find_leaked_substrings(scenario, thinking_dir, final_response)
        return self._build_result(scenario, actual_verdict, actual_runbook_id,
                                   actual_ticket_number, actual_suggested_hotfix, duration,
                                   final_response=final_response, leaked_substrings=leaked)

    def _find_leaked_substrings(self, scenario: Dict[str, Any], thinking_dir: Path,
                                 final_response: str) -> List[str]:
        """
        Checks that none of a scenario's `forbidden_substrings` (e.g. a fake email/credit
        card planted to test the PII guardrail) leaked into any *downstream* agent's trace
        or the final response. The front-man's own trace file (named exactly after the
        network's entry agent, no dots) legitimately logs the raw human turn verbatim --
        that's expected transcript behavior, not a guardrail failure -- so it's excluded.
        """
        forbidden: List[str] = scenario.get("forbidden_substrings", [])
        if not forbidden:
            return []

        haystacks = [final_response]
        if thinking_dir.exists():
            for trace_file in thinking_dir.iterdir():
                if trace_file.is_file() and "." in trace_file.name:  # downstream agents only
                    try:
                        haystacks.append(trace_file.read_text(encoding="utf-8"))
                    except OSError as exc:
                        logger.warning("Could not read trace file %s: %s", trace_file, exc)

        combined = "\n".join(haystacks)
        return [s for s in forbidden if s in combined]

    def _build_result(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
            self, scenario: Dict[str, Any], actual_verdict: Optional[str],
            actual_runbook_id: Optional[str], actual_ticket_number: Optional[str],
            actual_suggested_hotfix: Optional[str], duration: float,
                       final_response: str = "", error: str = "",
                       leaked_substrings: Optional[List[str]] = None) -> ScenarioResult:
        """Grades a scenario's actual outcome against its expectation and builds the result."""
        expected_verdict: str = scenario["expected_verdict"]
        check_runbook: bool = scenario.get("check_runbook", False)
        expected_runbook_id: Optional[str] = scenario.get("expected_runbook_id")
        check_ticket: bool = scenario.get("check_ticket", True)
        check_hotfix: bool = scenario.get("check_hotfix", False)
        leaked_substrings = leaked_substrings or []

        # A NOT_SUPPORTED expectation is satisfied even if correlation_check was never
        # reached at all (e.g. an unknown service with no data) -- either way, the system
        # did not falsely assert a confident SUPPORTED verdict, which is what matters.
        if expected_verdict == "NOT_SUPPORTED":
            verdict_ok = actual_verdict in (None, "NOT_SUPPORTED")
        else:
            verdict_ok = actual_verdict == expected_verdict

        runbook_ok = (not check_runbook) or (actual_runbook_id == expected_runbook_id)
        # Every scenario should open a ServiceNow-style ticket -- this is the product-flow
        # check, independent of whether a root cause was actually confirmed.
        ticket_ok = (not check_ticket) or bool(actual_ticket_number and actual_ticket_number.startswith("INC"))
        # Only checked for services seeded with >=2 prior recorded incidents, where
        # recall_incidents must return an actionable suggestion. Whether the model's prose then
        # repeats it is recorded (hotfix_surfaced) but not graded: the UI shows the tool-verified
        # hotfix regardless, and the LLM's wording is exactly what this harness refuses to trust.
        hotfix_surfaced = "suggested hotfix" in final_response.lower()
        hotfix_ok = (not check_hotfix) or bool(actual_suggested_hotfix)
        passed = verdict_ok and runbook_ok and ticket_ok and hotfix_ok and not error and not leaked_substrings

        return ScenarioResult(
            scenario_id=scenario["id"],
            prompt=scenario["prompt"],
            expected_verdict=expected_verdict,
            actual_verdict=actual_verdict,
            expected_runbook_id=expected_runbook_id if check_runbook else None,
            actual_runbook_id=actual_runbook_id,
            runbook_checked=check_runbook,
            ticket_checked=check_ticket,
            actual_ticket_number=actual_ticket_number,
            hotfix_checked=check_hotfix,
            actual_suggested_hotfix=actual_suggested_hotfix,
            passed=passed,
            duration_seconds=round(duration, 1),
            final_response=final_response,
            error=error,
            leaked_substrings=leaked_substrings,
            hotfix_surfaced=hotfix_surfaced,
        )

    def _read_correlation_verdict(self, thinking_dir: Path) -> Optional[str]:
        """Reads the verdict correlation_check actually returned, if it was ever called."""
        trace_file = thinking_dir / "IncidentCommander.Scribe.correlation_check"
        tool_output = self._last_tool_output(trace_file)
        if isinstance(tool_output, dict):
            return tool_output.get("verdict")
        return None

    def _read_top_runbook_id(self, thinking_dir: Path) -> Optional[str]:
        """Reads the top runbook match id runbook_search actually returned, if any."""
        trace_file = thinking_dir / "IncidentCommander.RunbookAgent.runbook_search"
        tool_output = self._last_tool_output(trace_file)
        if isinstance(tool_output, dict):
            matches = tool_output.get("matches") or []
            if matches:
                return matches[0].get("id")
        return None

    def _read_ticket_number(self, thinking_dir: Path) -> Optional[str]:
        """Reads the ticket number open_ticket actually returned, if it was ever called."""
        trace_file = thinking_dir / "IncidentCommander.TicketAgent.open_ticket"
        tool_output = self._last_tool_output(trace_file)
        if isinstance(tool_output, dict):
            return tool_output.get("number")
        return None

    def _read_suggested_hotfix(self, thinking_dir: Path) -> Optional[str]:
        """Reads the suggested_hotfix recall_incidents actually returned, if any."""
        trace_file = thinking_dir / "IncidentCommander.HistorianAgent.recall_incidents"
        tool_output = self._last_tool_output(trace_file)
        if isinstance(tool_output, dict):
            return tool_output.get("suggested_hotfix")
        return None

    @staticmethod
    def _last_tool_output(trace_file: Path) -> Optional[Any]:
        """Parses a thinking-dir trace file and returns the last tool_output value, if any."""
        if not trace_file.exists():
            return None
        try:
            text = trace_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read trace file %s: %s", trace_file, exc)
            return None

        matches = GOT_RESULT_PATTERN.findall(text)
        if not matches:
            return None
        try:
            parsed = json.loads(matches[-1])
        except json.JSONDecodeError as exc:
            logger.warning("Could not parse JSON in %s: %s", trace_file, exc)
            return None
        return parsed.get("tool_output")

    @staticmethod
    def _extract_final_response(stdout: str) -> str:
        """Pulls the human-readable final answer out of the CLI's stdout, for the report."""
        found = FINAL_RESPONSE_PATTERN.search(stdout)
        return found.group(1).strip() if found else ""


def write_report(results: List[ScenarioResult], report_dir: Path) -> None:
    """Writes both a machine-readable JSON file and a human-readable Markdown report."""
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "results.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
    )

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    pass_rate = (passed / total * 100) if total else 0.0

    lines = [
        "# Sentinel eval report",
        "",
        f"**{passed}/{total} scenarios passed ({pass_rate:.0f}%)**",
        "",
        "| Scenario | Expected | Actual | Runbook OK | Ticket OK | Hotfix OK | Result | Time (s) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        runbook_cell = "n/a" if not r.runbook_checked else (
            "yes" if r.actual_runbook_id == r.expected_runbook_id else
            f"no (got {r.actual_runbook_id!r})"
        )
        ticket_cell = "n/a" if not r.ticket_checked else (
            "yes" if r.actual_ticket_number and r.actual_ticket_number.startswith("INC") else "no"
        )
        hotfix_cell = "n/a" if not r.hotfix_checked else (
            ("yes (also in answer)" if r.hotfix_surfaced else "yes (tool only)") if r.actual_suggested_hotfix else "no"
        )
        reason = r.error or (f"leaked: {r.leaked_substrings}" if r.leaked_substrings else "")
        status = "PASS" if r.passed else f"FAIL{(' - ' + reason) if reason else ''}"
        lines.append(
            f"| {r.scenario_id} | {r.expected_verdict} | {r.actual_verdict} | "
            f"{runbook_cell} | {ticket_cell} | {hotfix_cell} | {status} | {r.duration_seconds} |"
        )
    (report_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    """CLI entry point: runs the eval harness and prints/saves a summary report."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv(REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(description="Run the Sentinel eval harness.")
    parser.add_argument("--scenarios", type=Path, default=Path(__file__).parent / "scenarios.json")
    parser.add_argument("--report-dir", type=Path, default=Path(__file__).parent / "_reports")
    parser.add_argument("--thinking-root", type=Path, default=Path(__file__).parent / "_thinking")
    args = parser.parse_args()

    runner = EvalRunner(scenarios_path=args.scenarios, thinking_root=args.thinking_root)
    results = runner.run_all()
    write_report(results, args.report_dir)

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"\n{passed}/{total} scenarios passed ({passed / total * 100:.0f}%)\n")
    for r in results:
        marker = "PASS" if r.passed else "FAIL"
        print(f"  [{marker}] {r.scenario_id}: expected={r.expected_verdict} actual={r.actual_verdict}"
              + (f" error={r.error}" if r.error else ""))
    print(f"\nFull report: {args.report_dir / 'report.md'}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
