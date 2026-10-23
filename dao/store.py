"""Transactional state snapshots and a workspace-wide, hash-linked audit ledger."""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Iterator
from uuid import uuid4


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def credit(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Credits must be a finite nonnegative number.")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError("Credits must be a finite nonnegative number.") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError("Credits must be a finite nonnegative number.")
    return result


class Store:
    def __init__(self, directory: str | Path):
        self.root = Path(directory).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite3", timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _initialize(self) -> None:
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS commits (
                hash TEXT PRIMARY KEY, parent TEXT REFERENCES commits(hash),
                state TEXT NOT NULL, message TEXT NOT NULL, created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS branches (
                name TEXT PRIMARY KEY, head TEXT NOT NULL REFERENCES commits(hash)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, branch TEXT NOT NULL,
                commit_hash TEXT, payload TEXT NOT NULL, created TEXT NOT NULL,
                prev_hash TEXT NOT NULL, hash TEXT NOT NULL UNIQUE
            );
        """)
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM commits LIMIT 1").fetchone():
                head = self._insert_commit({"plan": None, "messages": [], "draft": None}, None, "Initialize Dao")
                self.db.execute("INSERT INTO branches VALUES ('main', ?)", (head,))
                self.db.execute("INSERT INTO settings VALUES ('active_branch','main')")
                self._event("branch", {"name": "main", "head": head})
                self._event("checkout", {"name": "main"})
                self._event("budget", {"limit": 100.0})
        problems = self.verify(check_files=False)
        if problems:
            raise ValueError("Workspace integrity check failed: " + "; ".join(problems[:3]))

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.db.close()

    @property
    def branch(self) -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key='active_branch'").fetchone()
        if row is None:
            raise ValueError("Missing active branch.")
        return row[0]

    @property
    def head(self) -> str:
        row = self.db.execute("SELECT head FROM branches WHERE name=?", (self.branch,)).fetchone()
        if row is None:
            raise ValueError("Missing branch head.")
        return row[0]

    def load(self) -> dict:
        return self.snapshot()[0]

    def snapshot(self) -> tuple[dict, str, str]:
        row = self.db.execute("""SELECT c.state,c.hash,b.name FROM settings s
            JOIN branches b ON b.name=s.value JOIN commits c ON c.hash=b.head
            WHERE s.key='active_branch'""").fetchone()
        if row is None:
            raise ValueError("Missing current snapshot.")
        return json.loads(row[0]), row[1], row[2]

    def _event(self, kind: str, payload: dict, commit_hash: str | None = None,
               branch_override: str | None = None) -> None:
        last = self.db.execute("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        entry = {"seq": last[0] + 1 if last else 1, "kind": kind, "branch": branch_override or self.branch,
                 "commit_hash": commit_hash, "payload": payload, "created": now(),
                 "prev_hash": last[1] if last else "0" * 64}
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", (
            entry["seq"], kind, entry["branch"], commit_hash, canonical(payload), entry["created"],
            entry["prev_hash"], digest(entry),
        ))

    def _insert_commit(self, state: dict, parent: str | None, message: str) -> str:
        created = now()
        value = digest({"parent": parent, "state": state, "message": message, "created": created})
        self.db.execute("INSERT INTO commits VALUES (?,?,?,?,?)", (value, parent, canonical(state), message, created))
        return value

    def commit(self, state: dict, message: str, expected_head: str | None = None,
               expected_branch: str | None = None) -> str:
        with self.transaction():
            parent = self.head
            if ((expected_head is not None and expected_head != parent) or
                    (expected_branch is not None and expected_branch != self.branch)):
                raise ValueError("State changed in another terminal. Review it and retry.")
            head = self._insert_commit(state, parent, message)
            self.db.execute("UPDATE branches SET head=? WHERE name=?", (head, self.branch))
            self._event("commit", {"parent": parent, "head": head}, head)
            return head

    def create_branch(self, name: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}", name):
            raise ValueError("Use 1-64 letters, digits, dots, slashes, underscores or hyphens for a branch.")
        with self.transaction():
            if self.db.execute("SELECT 1 FROM branches WHERE name=?", (name,)).fetchone():
                raise ValueError("That branch already exists.")
            self.db.execute("INSERT INTO branches VALUES (?,?)", (name, self.head))
            self._event("branch", {"name": name, "head": self.head})

    def checkout(self, name: str) -> None:
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM branches WHERE name=?", (name,)).fetchone():
                raise ValueError("Unknown branch.")
            self.db.execute("UPDATE settings SET value=? WHERE key='active_branch'", (name,))
            self._event("checkout", {"name": name})

    def branches(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT name,head FROM branches ORDER BY name")]

    def resolve(self, ref: str) -> str:
        branch = self.db.execute("SELECT head FROM branches WHERE name=?", (ref,)).fetchone()
        if branch:
            return branch[0]
        if not re.fullmatch(r"[a-f0-9]{6,64}", ref):
            raise ValueError("Use a branch name or at least six characters of a snapshot hash.")
        rows = self.db.execute("SELECT hash FROM commits WHERE hash LIKE ?", (ref + "%",)).fetchall()
        if len(rows) != 1:
            raise ValueError("Unknown or ambiguous snapshot.")
        return rows[0][0]

    def restore(self, ref: str) -> str:
        _, expected, branch = self.snapshot()
        target = self.resolve(ref)
        state = json.loads(self.db.execute("SELECT state FROM commits WHERE hash=?", (target,)).fetchone()[0])
        return self.commit(state, "Restore " + target[:12], expected, branch)

    def history(self, limit: int = 20) -> list[dict]:
        head: str | None = self.head
        result = []
        while head and len(result) < limit:
            row = self.db.execute("SELECT hash,parent,message,created FROM commits WHERE hash=?", (head,)).fetchone()
            result.append(dict(row))
            head = row["parent"]
        return result

    def diff(self, ref: str) -> str:
        target = self.resolve(ref)
        other = json.loads(self.db.execute("SELECT state FROM commits WHERE hash=?", (target,)).fetchone()[0])
        return "\n".join(difflib.unified_diff(
            json.dumps(other, indent=2, ensure_ascii=False, sort_keys=True).splitlines(),
            json.dumps(self.load(), indent=2, ensure_ascii=False, sort_keys=True).splitlines(),
            fromfile=ref, tofile=self.branch, lineterm="")) or "No differences."

    def events(self, limit: int = 30) -> list[dict]:
        rows = self.db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in reversed(rows)]

    def usage(self) -> dict:
        rows = self.db.execute("SELECT kind,payload FROM events WHERE kind IN ('budget','usage','model_result') ORDER BY seq").fetchall()
        results = {json.loads(row["payload"])["request_id"]: json.loads(row["payload"])
                   for row in rows if row["kind"] == "model_result"}
        limit, spent, remaining = self._amounts(rows)
        calls, inputs, outputs, unknown = 0, 0, 0, 0
        for row in rows:
            payload = json.loads(row["payload"])
            if row["kind"] == "usage":
                if payload["category"] == "model":
                    calls += 1
                    tokens = results.get(payload.get("request_id"), payload)
                    if tokens.get("prompt_tokens") is None or tokens.get("completion_tokens") is None:
                        unknown += 1
                    inputs += tokens.get("prompt_tokens") or 0
                    outputs += tokens.get("completion_tokens") or 0
        return {"limit": float(limit), "spent": float(spent), "remaining": float(max(Decimal(0), remaining)),
                "model_calls": calls, "prompt_tokens": inputs, "completion_tokens": outputs,
                "calls_with_unknown_tokens": unknown}

    def set_budget(self, limit: float) -> None:
        limit = credit(limit)
        with self.transaction():
            if Decimal(str(limit)) < self._amounts()[1]:
                raise ValueError("The total budget cannot be below credits already spent.")
            self._event("budget", {"limit": limit})

    def _amounts(self, rows=None) -> tuple[Decimal, Decimal, Decimal]:
        # Cover the entire finite float exponent range without rounding tiny spends away.
        with localcontext() as context:
            context.prec = 1000
            limit, spent = Decimal(0), Decimal(0)
            if rows is None:
                rows = self.db.execute("SELECT kind,payload FROM events WHERE kind IN ('budget','usage') ORDER BY seq")
            for row in rows:
                payload = json.loads(row["payload"])
                if row["kind"] == "budget":
                    limit = Decimal(str(payload["limit"]))
                elif row["kind"] == "usage":
                    spent += Decimal(str(payload["cost"]))
            return limit, spent, limit - spent

    def _charge(self, cost: float, category: str, note: str, **metadata: object) -> None:
        cost = credit(cost)
        if Decimal(str(cost)) > self._amounts()[2]:
            raise ValueError("Insufficient workspace credits. Increase /budget or reduce the cost.")
        self._event("usage", {"cost": cost, "category": category, "note": note, **metadata}, self.head)

    def charge(self, cost: float, category: str, note: str, **metadata: object) -> None:
        with self.transaction():
            self._charge(cost, category, note, **metadata)

    def record(self, kind: str, payload: dict) -> None:
        with self.transaction():
            self._event(kind, payload, self.head)

    def _artifact_path(self, artifact_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
            raise ValueError("Invalid artifact receipt ID.")
        folder = self.root / "artifacts"
        if folder.is_symlink() or (folder.exists() and folder.resolve().parent != self.root):
            raise ValueError("Artifact directory must be inside this workspace and cannot be a link.")
        folder.mkdir(exist_ok=True)
        path = folder / (artifact_id + ".json")
        if path.is_symlink():
            raise ValueError("Artifact cannot be a symbolic link.")
        return path

    def execute_export(self, content: dict, cost: float, expected_head: str,
                       expected_branch: str | None = None) -> dict:
        artifact_id = uuid4().hex
        body = (canonical(content) + "\n").encode("utf-8")
        receipt = {"id": artifact_id, "sha256": hashlib.sha256(body).hexdigest(), "snapshot": expected_head}
        path = self._artifact_path(artifact_id)
        with self.transaction():
            if expected_head != self.head or (expected_branch is not None and expected_branch != self.branch):
                raise ValueError("State changed. Adjudicate again before acting.")
            self._charge(cost, "action", "Export approved commitment", artifact_id=artifact_id)
            self._event("artifact_prepared", receipt, expected_head)
        try:
            with path.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            self.record("artifact_completed", receipt)
        except OSError:
            self.record("artifact_failed", receipt)
            raise ValueError("Artifact write failed. Inspect /receipts; the attempted action remains accounted for.") from None
        return {**receipt, "path": str(path)}

    def receipts(self) -> list[dict]:
        result: dict[str, dict] = {}
        rows = self.db.execute("SELECT kind,payload FROM events WHERE kind LIKE 'artifact_%' ORDER BY seq")
        for row in rows:
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict) or not all(key in payload for key in ("id", "sha256", "snapshot")):
                raise ValueError("Malformed artifact receipt in audit history.")
            result[payload["id"]] = {**payload, "status": row["kind"].removeprefix("artifact_")}
        return list(result.values())

    def undo_export(self, artifact_id: str) -> None:
        # A prepared receipt is sufficient for recovery after an interrupted write.
        with self.transaction():
            receipt = next((r for r in self.receipts() if r["id"] == artifact_id), None)
            if not receipt or receipt["status"] == "undone":
                raise ValueError("Unknown or already undone receipt.")
            path = self._artifact_path(artifact_id)
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != receipt["sha256"]:
                    raise ValueError("Artifact changed outside Dao. Refusing to delete it.")
                path.unlink()
            self._event("artifact_undone", {"id": artifact_id, "sha256": receipt["sha256"],
                                           "snapshot": receipt["snapshot"]}, self.head)

    def verify(self, check_files: bool = True) -> list[str]:
        if self.db.in_transaction:
            return self._verify_snapshot(check_files)
        self.db.execute("BEGIN")
        try:
            return self._verify_snapshot(check_files)
        finally:
            self.db.execute("ROLLBACK")

    def _verify_snapshot(self, check_files: bool) -> list[str]:
        errors: list[str] = []
        if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            errors.append("SQLite integrity failure")
        commits = {row["hash"]: dict(row) for row in self.db.execute("SELECT * FROM commits")}
        for key, row in commits.items():
            try:
                expected = digest({"parent": row["parent"], "state": json.loads(row["state"]),
                                   "message": row["message"], "created": row["created"]})
                if expected != key:
                    errors.append("Snapshot hash mismatch: " + key[:12])
                if row["parent"] and row["parent"] not in commits:
                    errors.append("Missing snapshot parent: " + key[:12])
            except (ValueError, TypeError):
                errors.append("Invalid snapshot: " + key[:12])
        previous, expected_seq, active = "0" * 64, 1, None
        branches: dict[str, str] = {}
        for row in self.db.execute("SELECT * FROM events ORDER BY seq"):
            entry = dict(row)
            try:
                payload = json.loads(entry["payload"])
                entry["payload"] = payload
                saved_hash = entry.pop("hash")
                if entry["prev_hash"] != previous or digest(entry) != saved_hash or entry["seq"] != expected_seq:
                    errors.append("Audit chain mismatch at event " + str(entry["seq"]))
                if entry["commit_hash"] and entry["commit_hash"] not in commits:
                    errors.append("Audit references missing snapshot")
                if entry["kind"] == "branch":
                    branches[payload["name"]] = payload["head"]
                elif entry["kind"] == "commit":
                    if branches.get(entry["branch"]) != payload["parent"]:
                        errors.append("Branch transition mismatch")
                    branches[entry["branch"]] = payload["head"]
                elif entry["kind"] == "checkout":
                    active = payload["name"]
                previous = saved_hash
                expected_seq += 1
            except (ValueError, KeyError, TypeError):
                errors.append("Invalid audit event " + str(row["seq"]))
        actual = {row["name"]: row["head"] for row in self.db.execute("SELECT * FROM branches")}
        if actual != branches or any(head not in commits for head in actual.values()):
            errors.append("Branch pointers differ from audit history")
        if active != self.branch:
            errors.append("Active branch differs from audit history")
        if check_files and not errors:
            try:
                receipts = self.receipts()
            except (ValueError, KeyError, TypeError):
                errors.append("Malformed artifact receipts in audit history")
                receipts = []
            for receipt in receipts:
                if receipt["status"] == "completed":
                    try:
                        path = self._artifact_path(receipt["id"])
                        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != receipt["sha256"]:
                            errors.append("Artifact missing or changed: " + receipt["id"])
                    except (OSError, ValueError):
                        errors.append("Artifact inaccessible: " + receipt["id"])
        return errors
