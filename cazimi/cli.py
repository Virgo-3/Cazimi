"""Interactive research terminal and foreground persistent polling worker."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import time

from dao.cli import HELP as DAO_HELP, Terminal, clean, format_result as dao_format
from dao.engine import new_plan
from . import __version__
from .runtime import ResearchRuntime, research_state


HELP = """CAZIMI - continuous, falsifiable equity research

  /source-add FILE               Register a JSON source definition
  /sources                       Inspect registered sources
  /watch-add FILE                Register a thesis and valuation assumptions
  /watches                       List watched theses
  /approve ID                    Approve this thesis's monitoring thresholds
  /run [--model-review]           Poll once and review the current branch
  /schedule SECONDS [--model-review]
                                 Persist a polling schedule for this branch
  /pause [BRANCH]                Pause a schedule; an in-flight review can finish
  /jobs                          Inspect schedules, failures and worker leases
  /reports [ID]                  Read latest recorded research, with as-of dates
  /evidence [TICKER]             Inspect latest 50 preserved source observations
  /document-review REVISION NOTE Acknowledge a document review; creates no metrics
  /alerts [--all]                Read material changes and error/recovery alerts
  /ack ID                        Acknowledge an alert
  /reviews                       Inspect optional model opposing-view commentary
  /outcomes                      Compare dated forecasts with later raw prices
  /verify                        Verify state, evidence, alerts and outcomes

Run the worker with: python -m cazimi watch
A schedule runs only while that worker is running. It survives worker restarts.
Research postures are review suggestions. Cazimi has no brokerage execution.
Model suggestions are commentary, with no authority to change evidence or rules.

The underlying Dao planning, branching, usage and conversation commands follow:
""" + DAO_HELP


def read_json(path_text: str) -> tuple[dict, Path]:
    parts = shlex.split(path_text)
    if len(parts) != 1:
        raise ValueError('Supply one quoted JSON file path.')
    path = Path(parts[0]).expanduser().resolve()
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Configuration files must be no larger than 2 MiB.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except RecursionError:
        raise ValueError("Configuration JSON is nested too deeply.") from None
    if not isinstance(value, dict):
        raise ValueError("Configuration must be a JSON object.")
    return value, path.parent


class CazimiTerminal(Terminal):
    def __init__(self, store: ResearchRuntime):
        super().__init__(store)
        self.store = store

    def model_context(self, state: dict) -> dict:
        research = state.get("research", {})
        # Preserve room for the conversation and cap context through the existing adapter.
        return {"manual_plan": state.get("plan"), "research_reports": research.get("reports", {}),
                "authority": "These are source-linked research records and assumptions, not trading instructions."}

    def run_line(self, line: str) -> dict:
        command, _, tail = line.strip().partition(" ")
        command = command.lower()
        if len(line) > 32000:
            raise ValueError("Command exceeds 32,000 characters.")
        if command == "/help":
            return {"type": "help", "text": HELP}
        if command == "/source-add":
            raw, base = read_json(tail)
            return {"type": "source", "source": self.store.add_source(raw, base)}
        if command == "/watch-add":
            raw, _ = read_json(tail)
            return {"type": "watch", "watch": self.store.add_watch(raw)}
        if command == "/approve":
            return {"type": "watch", "watch": self.store.approve(tail.strip())}
        if command in ("/sources", "/watches", "/reports", "/show"):
            state = self.store.load()
            research = research_state(state)
            if command == "/sources":
                return {"type": "sources", "items": list(research["sources"].values())}
            if command == "/watches":
                return {"type": "watches", "items": list(research["watches"].values())}
            if command == "/show" and state.get("plan"):
                return super().run_line(line)
            reports = research["reports"]
            if tail:
                if tail not in reports:
                    raise ValueError("No recorded report for this thesis; use /run first.")
                reports = {tail: reports[tail]}
            return {"type": "reports", "branch": self.store.branch, "items": list(reports.values())}
        if command == "/run":
            if tail not in ("", "--model-review"):
                raise ValueError("Use /run or /run --model-review.")
            return self.store.run(model_review=True if tail else None)
        if command == "/schedule":
            parts = shlex.split(tail)
            if not parts or len(parts) > 2 or (len(parts) == 2 and parts[1] != "--model-review"):
                raise ValueError("Use /schedule SECONDS [--model-review].")
            return {"type": "jobs", "items": [self.store.schedule(int(parts[0]), model_review=len(parts) == 2)]}
        if command == "/pause":
            self.store.pause(tail or None)
            return {"type": "message", "message": "Schedule paused. An in-flight research review can finish."}
        if command == "/jobs":
            return {"type": "jobs", "items": self.store.jobs()}
        if command == "/alerts":
            if tail not in ("", "--all"):
                raise ValueError("Use /alerts or /alerts --all.")
            return {"type": "alerts", "items": self.store.alerts(include_acknowledged=bool(tail))}
        if command == "/ack":
            self.store.acknowledge(int(tail))
            return {"type": "message", "message": "Alert acknowledged."}
        if command == "/evidence":
            items = self.store.observations()
            if tail:
                items = [o for o in items if o["ticker"] == tail.upper()]
            return {"type": "evidence", "items": items[-50:], "total": len(items)}
        if command == "/document-review":
            revision, _, note = tail.partition(" ")
            self.store.review_document(revision, note)
            return {"type": "message", "message": "Document review recorded. Numeric evidence and monitoring rules are unchanged; /run reassesses readiness."}
        if command == "/outcomes":
            return {"type": "outcomes", **self.store.outcomes()}
        if command == "/reviews":
            rows = self.store.db.execute("SELECT payload FROM events WHERE kind='research_commentary' AND branch=? ORDER BY seq DESC LIMIT 20", (self.store.branch,))
            return {"type": "reviews", "items": [json.loads(r[0]) for r in rows]}
        if command == "/verify":
            problems = self.store.verify() + self.store.verify_research()
            if problems:
                raise ValueError("Integrity check failed: " + "; ".join(problems))
            return {"type": "message", "message": "State snapshots, audit chain, evidence revisions, research alerts and outcomes verified."}
        if command == "/goal":
            # A manual commitment must not silently discard the research registry.
            state, head, self._branch = self.store.snapshot()
            state.update(plan=new_plan(tail), messages=[], draft=None)
            return self.save(state, "Propose manual commitment: " + tail[:100], head)
        return super().run_line(line)


def format_result(result: dict) -> str:
    kind = result["type"]
    if kind == "research_cycle":
        changed = ", ".join(result["changed_theses"]) or "none"
        errors = "; ".join(result["source_errors"])
        return (f"Research branch {result['branch']}: {result['new_evidence']} new evidence revisions; changed theses: {changed}."
                + ("\n" + errors if errors else "") + ("\n" + result["review_error"] if result["review_error"] else ""))
    if kind == "idle":
        return "No research schedule is due."
    if kind == "reports":
        lines = ["Latest recorded research for " + result["branch"] + "; /run refreshes evidence."]
        for report in result["items"]:
            lines.extend(["", f"{report['ticker']} / {report['watch_id']}  as of {report['as_of']}",
                f"Company: {report['company_thesis_status']} | Security: {report['security_readiness']} | Review posture: {report['position_action']}"])
            lines += ["  " + reason for reason in report["reasons"]]
            lines += ["  Gap: " + gap for gap in report["gaps"]]
            valuation = report.get("valuation") or {}
            if valuation.get("current_price") is not None:
                lines.append(f"  Price: {valuation['current_price']} {valuation['currency']} | Expected value: {valuation.get('expected_price')} | Expected return: {valuation.get('expected_return')}")
            for pillar in report["pillars"]:
                lines.append(f"  {pillar['id']}: {pillar['status']}")
            for case in valuation.get("scenario_views", []):
                lines.append(f"  {case['name']}: price {case['price']} {valuation['currency']}; probability {case['probability']}; return {case['return_fraction']}")
            for document in report.get("pending_documents", []):
                lines += [f"  Review document: {document['title']}", f"    {document['citation']}",
                          f"    /document-review {document['revision']} YOUR_REVIEW_NOTE"]
            lines.append("  Next review gate: " + str(report.get("next_review_at") or "resolve the listed evidence gaps"))
        return "\n".join(lines) if result["items"] else "No research reports yet. Register sources and a thesis, then /run."
    if kind == "alerts":
        return "\n".join(f"#{r['id']} {r['created']} {json.dumps(r['payload'], ensure_ascii=False)}" for r in result["items"]) or "No unacknowledged material-change alerts."
    if kind == "jobs":
        return "\n".join(f"{r['branch']}: {'enabled' if r['enabled'] else 'paused/manual'}; every {r['interval_seconds']}s; next {r['next_run']}; failures {r['failures']}" for r in result["items"]) or "No schedules. Use /schedule SECONDS."
    if kind == "watches":
        return "\n".join(f"{r['id']} / {r['ticker']}: {r['thesis']}" for r in result["items"]) or "No watched theses."
    if kind == "source":
        return "Registered source " + result["source"]["id"]
    if kind == "watch":
        return "Saved thesis " + result["watch"]["id"] + ". Monitoring rules and assumptions are versioned."
    if kind in ("sources", "evidence", "outcomes", "reviews"):
        return json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
    return dao_format(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cazimi automated equity research terminal")
    parser.add_argument("mode", nargs="?", choices=["terminal", "run", "watch", "reports"], default="terminal")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--workspace", type=Path, default=Path(".cazimi"))
    parser.add_argument("--branch", help="research branch for run; interactive checkout for terminal/reports")
    parser.add_argument("-c", "--command", action="append", default=[])
    parser.add_argument("--script", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--model-review", action="store_true", help="request suggestion-only model commentary on changed reports")
    parser.add_argument("--poll-seconds", type=float, default=5, help="worker idle polling interval (0.1-60 seconds)")
    parser.add_argument("--cycles", type=int, help="exit worker after this many checks (default: keep running)")
    args = parser.parse_args(argv)
    if not 0.1 <= args.poll_seconds <= 60 or (args.cycles is not None and args.cycles < 1):
        parser.error("Polling must be 0.1-60 seconds; cycles must be positive.")
    runtime = None
    try:
        runtime = ResearchRuntime(args.workspace)
        terminal = CazimiTerminal(runtime)
        color = sys.stdout.isatty() and not args.no_color and not args.json and "NO_COLOR" not in os.environ

        def emit(result: dict) -> None:
            if args.json:
                print(json.dumps(result, ensure_ascii=True, allow_nan=False), flush=True)
            else:
                content = clean(format_result(result))
                if content:
                    print(("\033[36m" + content + "\033[0m") if color else content, flush=True)

        if args.mode == "run":
            result = runtime.run(branch=args.branch, model_review=True if args.model_review else None)
            emit(result)
            return 1 if result["source_errors"] or result["review_error"] else 0
        if args.mode == "watch":
            if args.branch or args.model_review or args.command or args.script:
                raise ValueError("Configure branch schedules before starting watch; the worker follows each saved schedule.")
            count = 0
            failures = False
            while args.cycles is None or count < args.cycles:
                before_alert = runtime.db.execute("SELECT COALESCE(MAX(id),0) FROM research_alerts").fetchone()[0]
                try:
                    result = runtime.run(force=False)
                    failures = failures or bool(result.get("source_errors") or result.get("review_error"))
                    # Stay quiet for idle and unchanged cycles unless machine records were requested.
                    if args.json or result["type"] != "idle" and (result["changed_theses"] or result["source_errors"] or result["new_outcomes"]):
                        emit(result)
                except (ValueError, OSError, sqlite3.Error):
                    failures = True
                    after_alert = runtime.db.execute("SELECT COALESCE(MAX(id),0) FROM research_alerts").fetchone()[0]
                    if args.json or after_alert > before_alert:
                        emit({"type": "message", "message": "Research worker recorded a failure. Inspect /jobs and /alerts; it will retry with backoff."})
                count += 1
                if args.cycles is None or count < args.cycles:
                    time.sleep(args.poll_seconds)
            return 1 if failures else 0
        if args.branch:
            runtime.checkout(args.branch)
        if args.mode == "reports":
            emit(terminal.run_line("/reports"))
            return 0
        commands = list(args.command)
        if args.script:
            commands.extend(s.strip() for s in args.script.read_text(encoding="utf-8").splitlines() if s.strip() and not s.lstrip().startswith("#"))
        batch = bool(args.command) or args.script is not None
        if not batch:
            print(f"CAZIMI {__version__} | automated equity research\n/source-add and /watch-add to configure; /help for commands.\nState: {clean(runtime.root)}\n")
        iterator = iter(commands)
        while True:
            try:
                line = next(iterator) if batch else input(f"cazimi:{clean(runtime.branch)}> ")
            except (EOFError, StopIteration):
                break
            try:
                result = terminal.run_line(line)
                if result["type"] == "quit":
                    break
                emit(result)
            except (ValueError, OSError, sqlite3.Error) as error:
                emit({"type": "message", "message": "Error: " + clean(error)})
                if batch:
                    return 1
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        if args.json:
            print(json.dumps({"type": "error", "message": clean(error)}))
        else:
            print("Error: " + clean(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Schedules, evidence and spending are preserved.", file=sys.stderr)
        return 130
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
