"""Interactive and scriptable terminal for falsifiable commitments."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
from uuid import uuid4

from . import __version__
from .engine import add_evidence, evaluate, new_plan, validate_plan
from .store import Store, credit


HELP = """Start with a future. Establish what would make it justified.

  /goal TEXT                     Start a provisional commitment
  /show                          Inspect prerequisites and live decision
  /require ID TEXT [--after A,B]  Add a prerequisite with dependencies
  /amend ID TEXT                 Rewrite a prerequisite; clears all evidence
  /support ID SOURCE NOTE        Append attributed supporting evidence
  /refute ID SOURCE NOTE         Append attributed disconfirming evidence
  /rollback TEXT                 Record the recovery path
  /economics KEY=VALUE ...       Set explicit decision assumptions
  /decide                        Adjudicate current evidence; save audit ruling
  /observe positive|negative SOURCE NOTE
                                 Buy the configured signal and update probability
  /act                           Save an approved commitment as a JSON artifact
  /receipts                      List action receipts across all branches
  /undo RECEIPT                  Remove an unchanged exported artifact

  /ask TEXT  (or ordinary text)   Talk to the configured model; stage a proposal
  /draft                         Inspect the model's suggested prerequisites
  /adopt                         Adopt draft with empty evidence and economics
  /import FILE                   Load a validated plan from JSON

  /branch NAME                   Fork current snapshot without switching
  /checkout NAME                 Switch branch
  /branches                      List branch heads
  /log                           Show snapshot ancestry
  /diff REF                      Compare a branch or snapshot with current state
  /restore REF                   Append a snapshot restoring previous state
  /usage                         Show cumulative credits and reported tokens
  /budget TOTAL                  Set total workspace credit limit
  /audit                         Show recent hash-linked audit events
  /verify                        Check snapshot, audit and artifact integrity
  /help                          Show this guide
  /quit                          Exit

Economics keys: probability, gain, loss, action_cost, observation_cost,
delay_cost, signal_accuracy, observation_available=true|false.
All values use your chosen credit/utility unit. Delay cost is opportunity cost.
Evidence is an attributed assertion; Dao does not independently verify it.
Only /act writes an artifact. Restoring state does not undo actions or usage.
"""


def clean(text: object) -> str:
    # Model responses, evidence and goals must never emit terminal escape sequences.
    return "".join(c if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) <= 159) else "?"
                   for c in str(text))


class Terminal:
    def __init__(self, store: Store):
        self.store = store
        self._branch = store.branch

    def plan(self, state: dict) -> dict:
        if state.get("plan") is None:
            raise ValueError("Start with /goal TEXT or adopt a model draft.")
        return validate_plan(state["plan"])

    def save(self, state: dict, message: str, head: str) -> dict:
        if state.get("plan") is not None:
            state["plan"] = validate_plan(state["plan"])
        return {"type": "saved", "message": message, "snapshot": self.store.commit(state, message, head, self._branch)}

    def ask(self, text: str, state: dict, head: str) -> dict:
        from .provider import Config, ModelClient

        config = Config.from_env()
        request_cost = credit(float(os.environ.get("DAO_REQUEST_COST", "1")))
        request_id = uuid4().hex
        messages = [*state.get("messages", []), {"role": "user", "content": text}]
        # Charge before a request: transport failure may still have incurred remote work.
        self.store.charge(request_cost, "model", "Conversation request", request_id=request_id,
                          model=config.model, prompt_tokens=None, completion_tokens=None)
        try:
            completion = ModelClient(config).complete(messages, self.model_context(state))
        except (ValueError, OSError) as error:
            self.store.record("model_result", {"request_id": request_id, "success": False,
                                               "prompt_tokens": None, "completion_tokens": None})
            raise ValueError(str(error)) from None
        self.store.record("model_result", {"request_id": request_id, "success": True,
                                           "model": completion.model,
                                           "prompt_tokens": completion.prompt_tokens,
                                           "completion_tokens": completion.completion_tokens})
        state["messages"] = [*messages, {"role": "assistant", "content": completion.reply}][-40:]
        if completion.proposal is not None:
            state["draft"] = completion.proposal
        saved = self.save(state, "Conversation", head)
        return {"type": "conversation", "reply": completion.reply, "draft_available": state.get("draft") is not None,
                "snapshot": saved["snapshot"]}

    def model_context(self, state: dict) -> dict | None:
        return state.get("plan")

    def run_line(self, line: str) -> dict:
        line = line.strip()
        if not line:
            return {"type": "empty"}
        if len(line) > 32000:
            raise ValueError("Command exceeds 32,000 characters.")
        if not line.startswith("/"):
            line = "/ask " + line
        command, _, tail = line.partition(" ")
        command = command.lower()
        state, head, self._branch = self.store.snapshot()
        if command in ("/quit", "/exit"):
            return {"type": "quit"}
        if command == "/help":
            return {"type": "help", "text": HELP}
        if command == "/goal":
            state = {"plan": new_plan(tail), "messages": [], "draft": None}
            return self.save(state, "Propose commitment: " + tail[:100], head)
        if command == "/show":
            plan = self.plan(state)
            return {"type": "plan", "plan": plan, "ruling": evaluate(plan, self.store.usage()["remaining"]),
                    "branch": self.store.branch, "snapshot": head}
        if command == "/ask":
            if not tail:
                raise ValueError("Use /ask TEXT, or type a conversational message.")
            return self.ask(tail, state, head)
        if command == "/draft":
            if not state.get("draft"):
                raise ValueError("No staged model proposal. Use /ask to request one.")
            return {"type": "draft", "plan": state["draft"]}
        if command == "/adopt":
            if not state.get("draft"):
                raise ValueError("There is no staged model proposal.")
            proposal = validate_plan(state["draft"])
            # Even a provider bug must never turn generated content into evidence.
            for requirement in proposal["requirements"]:
                requirement["evidence"] = []
            proposal["status"] = "provisional"
            proposal["economics"] = new_plan(proposal["goal"])["economics"]
            state["plan"], state["draft"] = proposal, None
            return self.save(state, "Adopt proposed prerequisites; evidence and economics reset", head)
        if command == "/import":
            paths = shlex.split(tail)
            if len(paths) != 1:
                raise ValueError("Use /import \"path/to/plan.json\".")
            path = Path(paths[0]).expanduser()
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Plan file must be no larger than 2 MiB.")
            try:
                state["plan"] = validate_plan(json.loads(path.read_text(encoding="utf-8")))
            except RecursionError:
                raise ValueError("Plan JSON is nested too deeply.") from None
            state["draft"] = None
            return self.save(state, "Import user-supplied plan and assertions", head)
        if command in ("/require", "/amend"):
            identifier, _, text = tail.partition(" ")
            dependencies = []
            if " --after " in text:
                text, dep_text = text.rsplit(" --after ", 1)
                dependencies = dep_text.split(",")
            plan = self.plan(state)
            existing = next((r for r in plan["requirements"] if r["id"] == identifier), None)
            if command == "/require":
                if existing:
                    raise ValueError("That prerequisite exists. Use /amend to change it and clear stale evidence.")
                plan["requirements"].append({"id": identifier, "text": text, "depends_on": dependencies, "evidence": []})
                message = "Add prerequisite " + identifier
            else:
                if existing is None:
                    raise ValueError("Unknown prerequisite.")
                existing["text"] = text
                if " --after " in tail:
                    existing["depends_on"] = dependencies
                for requirement in plan["requirements"]:
                    requirement["evidence"] = []
                message = "Amend " + identifier + "; all prerequisite evidence cleared"
            state["plan"] = plan
            return self.save(state, message, head)
        if command in ("/support", "/refute"):
            parts = shlex.split(tail)
            if len(parts) < 3:
                raise ValueError("Use " + command + ' ID "source" "observation or finding".')
            state["plan"] = add_evidence(self.plan(state), parts[0], command[1:], " ".join(parts[2:]), parts[1])
            return self.save(state, command[1:].capitalize() + " prerequisite " + parts[0], head)
        if command == "/rollback":
            plan = self.plan(state)
            plan["rollback"] = tail
            # A different recovery procedure invalidates any claim that it was tested.
            if any(r["evidence"] for r in plan["requirements"]):
                for requirement in plan["requirements"]:
                    requirement["evidence"] = []
                message = "Change recovery path; all prerequisite evidence cleared"
            else:
                message = "Define recovery path"
            state["plan"] = plan
            return self.save(state, message, head)
        if command == "/economics":
            plan = self.plan(state)
            if not tail:
                return {"type": "economics", "values": plan["economics"]}
            for item in shlex.split(tail):
                key, sep, value = item.partition("=")
                if not sep:
                    raise ValueError("Economics uses KEY=VALUE pairs.")
                if key == "observation_available":
                    if value not in ("true", "false"):
                        raise ValueError("observation_available must be true or false.")
                    plan["economics"][key] = value == "true"
                else:
                    plan["economics"][key] = float(value)
            state["plan"] = plan
            return self.save(state, "Update explicit decision assumptions", head)
        if command == "/decide":
            ruling = evaluate(self.plan(state), self.store.usage()["remaining"])
            self.store.record("adjudication", {"snapshot": head, "ruling": ruling})
            return {"type": "ruling", **ruling}
        if command == "/observe":
            parts = shlex.split(tail)
            if len(parts) < 3 or parts[0] not in ("positive", "negative"):
                raise ValueError('Use /observe positive|negative "source" "finding".')
            plan = self.plan(state)
            if not plan["economics"]["observation_available"]:
                raise ValueError("No observation is configured as available.")
            ruling = evaluate(plan, self.store.usage()["remaining"])
            signal = next((s for s in ruling["metrics"]["signals"] if s["signal"] == parts[0]), None)
            if signal is None:
                raise ValueError("That signal is impossible under the configured model. Revise the assumptions first.")
            self.store.charge(plan["economics"]["observation_cost"], "observation", "Record observed signal",
                              source=parts[1], finding=" ".join(parts[2:]), signal=parts[0], snapshot=head)
            plan["economics"]["probability"] = signal["posterior"]
            plan["economics"]["observation_available"] = False
            state["plan"] = plan
            return self.save(state, "Observe " + parts[0] + "; probability updated; evidence still adjudicated separately", head)
        if command == "/act":
            plan = self.plan(state)
            ruling = evaluate(plan, self.store.usage()["remaining"])
            self.store.record("adjudication", {"snapshot": head, "ruling": ruling})
            if ruling["decision"] != "act":
                raise ValueError("Commitment cannot act: " + " ".join(ruling["reasons"]))
            receipt = self.store.execute_export({"schema_version": 1, "goal": plan["goal"], "plan": plan,
                                                 "ruling": ruling, "snapshot": head},
                                                plan["economics"]["action_cost"], head, self._branch)
            return {"type": "action", "receipt": receipt,
                    "message": "Approved commitment exported. This receipt covers the local export only."}
        if command == "/receipts":
            return {"type": "receipts", "items": self.store.receipts()}
        if command == "/undo":
            self.store.undo_export(tail)
            return {"type": "message", "message": "Artifact removed; action history and consumed credits are preserved."}
        if command == "/branch":
            self.store.create_branch(tail)
            return {"type": "message", "message": "Created branch " + tail + ". Use /checkout to switch."}
        if command == "/checkout":
            self.store.checkout(tail)
            return {"type": "message", "message": "Current branch: " + tail}
        if command == "/branches":
            return {"type": "branches", "current": self.store.branch, "items": self.store.branches()}
        if command == "/log":
            return {"type": "history", "items": self.store.history()}
        if command == "/diff":
            return {"type": "diff", "text": self.store.diff(tail)}
        if command == "/restore":
            return {"type": "saved", "message": "Restored state; action effects and usage preserved",
                    "snapshot": self.store.restore(tail)}
        if command == "/usage":
            return {"type": "usage", **self.store.usage()}
        if command == "/budget":
            self.store.set_budget(float(tail))
            return {"type": "usage", **self.store.usage()}
        if command == "/audit":
            return {"type": "audit", "items": self.store.events()}
        if command == "/verify":
            errors = self.store.verify()
            if errors:
                raise ValueError("Integrity check failed: " + "; ".join(errors))
            return {"type": "message", "message": "Snapshot hashes, audit chain, branch pointers and completed artifacts verified."}
        raise ValueError("Unknown command. Use /help.")


def format_result(result: dict) -> str:
    kind = result["type"]
    if kind in ("empty", "quit"):
        return ""
    if kind in ("help", "diff"):
        return result["text"]
    if kind == "saved":
        return result["message"] + "\nSnapshot " + result["snapshot"][:12]
    if kind == "message":
        return result["message"]
    if kind == "conversation":
        return result["reply"] + ("\n\nA proposal is staged. Review /draft, then /adopt to use it." if result["draft_available"] else "")
    if kind == "ruling":
        metrics = result["metrics"]
        wait = (f"{metrics['wait_value']:.4g}" if metrics["observation_available"] else "unavailable")
        return (result["decision"].upper() + "\n" + "\n".join("  " + r for r in result["reasons"]) +
                f"\n  Act: {metrics['act_value']:.4g} | Wait: {wait} | Net information: {metrics['net_information_value']:.4g}")
    if kind == "plan":
        plan = result["plan"]
        rows = [f"{result['branch']} @ {result['snapshot'][:12]}", "", plan["goal"], "", "Prerequisites"]
        for req in result["ruling"]["requirements"]:
            rows.append(f"  [{req['state']}] {req['id']}: {req['text']}")
            if req["depends_on"]:
                rows.append("    requires: " + ", ".join(req["depends_on"]))
            for item in req["evidence"]:
                rows.append(f"    {item['verdict']} ({item['source'] or 'unattributed'}): {item['note']}")
        rows += ["", "Recovery: " + (plan["rollback"] or "undefined"), "", format_result({"type": "ruling", **result["ruling"]})]
        return "\n".join(rows)
    if kind == "usage":
        return (f"Credits: {result['spent']:g} / {result['limit']:g} spent; {result['remaining']:g} remaining\n"
                f"Model requests: {result['model_calls']} | Reported tokens: {result['prompt_tokens']} input, {result['completion_tokens']} output\n"
                f"Requests with unknown token usage: {result['calls_with_unknown_tokens']}")
    if kind == "action":
        receipt = result["receipt"]
        return f"{result['message']}\n{receipt['path']}\nUndo: /undo {receipt['id']}"
    if kind == "branches":
        return "\n".join(f"{'*' if r['name'] == result['current'] else ' '} {r['name']}  {r['head'][:12]}" for r in result["items"])
    if kind == "history":
        return "\n".join(f"{r['hash'][:12]}  {r['message']}" for r in result["items"])
    if kind == "audit":
        return "\n".join(f"{r['seq']:>4} {r['hash'][:12]} {r['branch']} {r['kind']} {json.dumps(r['payload'], ensure_ascii=False)}" for r in result["items"])
    if kind == "receipts":
        return "\n".join(f"{r['id']}  {r['status']}  snapshot {r['snapshot'][:12]}" for r in result["items"]) or "No action receipts."
    return json.dumps(result.get("plan", result.get("values", result)), indent=2, ensure_ascii=False, allow_nan=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dao - proposed futures must earn permission to happen")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--workspace", type=Path, default=Path(".dao"), help="state directory (default: .dao)")
    parser.add_argument("-c", "--command", action="append", default=[], help="execute a terminal command (repeatable)")
    parser.add_argument("--script", type=Path, help="execute commands from a UTF-8 file; stop on first error")
    parser.add_argument("--json", action="store_true", help="emit one JSON record per command")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    args = parser.parse_args(argv)
    store: Store | None = None
    try:
        store = Store(args.workspace)
        terminal = Terminal(store)
        commands = list(args.command)
        if args.script:
            commands.extend(line.strip() for line in args.script.read_text(encoding="utf-8").splitlines()
                            if line.strip() and not line.lstrip().startswith("#"))
        batch = bool(args.command) or args.script is not None
        color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ and not args.json

        def emit(result: dict) -> None:
            if args.json:
                print(json.dumps(result, ensure_ascii=True, allow_nan=False))
            else:
                text = clean(format_result(result))
                if text:
                    print(("\033[36m" + text + "\033[0m") if color else text)

        if not batch:
            mode = "model configured" if os.environ.get("DAO_MODEL") and os.environ.get("DAO_BASE_URL") else "manual planning; model unconfigured"
            print(f"DAO {__version__}  |  {mode}\nPropose -> establish prerequisites -> adjudicate -> act / reject / wait\n/help for commands. State: {clean(store.root)}\n")
        iterator = iter(commands)
        while True:
            try:
                line = next(iterator) if batch else input(f"dao:{clean(store.branch)}> ")
            except (StopIteration, EOFError):
                break
            try:
                result = terminal.run_line(line)
                if result["type"] == "quit":
                    break
                emit(result)
            except (ValueError, OSError, sqlite3.Error) as error:
                message = clean(error)
                if args.json:
                    print(json.dumps({"type": "error", "message": message}))
                else:
                    print("Error: " + message, file=sys.stderr)
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
        print("\nInterrupted. Saved history and usage are preserved.", file=sys.stderr)
        return 130
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
