"""Durable research polling, evidence revisions, forecasts and change alerts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from dao.store import Store, canonical, credit, digest
from .feeds import fetch_source, validate_source
from .research import evaluate_watch, validate_watch


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Research clocks must include a timezone.")
    return value.astimezone(timezone.utc).isoformat()


def parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp requires a timezone.")
    return parsed.astimezone(timezone.utc)


def research_state(state: dict) -> dict:
    return state.setdefault("research", {"watches": {}, "sources": {}, "reports": {}})


def signature(report: dict) -> str:
    comparable = deepcopy(report)
    comparable.pop("as_of", None)
    comparable.pop("next_review_at", None)
    return digest(comparable)


class ResearchRuntime(Store):
    """One SQLite database owns state, spending, leases and evidence history.

    A running job is pinned to a branch, regardless of the interactive checkout.
    A lease prevents another worker from applying the same concurrent cycle.
    """

    def __init__(self, directory: str | Path):
        super().__init__(directory)
        try:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS research_jobs (
                    branch TEXT PRIMARY KEY REFERENCES branches(name), enabled INTEGER NOT NULL,
                    interval_seconds INTEGER NOT NULL, next_run TEXT NOT NULL,
                    owner TEXT, lease_until TEXT, last_run TEXT, failures INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, model_review INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS research_observations (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL, source_revision TEXT NOT NULL,
                    first_seen TEXT NOT NULL, source_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_seen (
                    branch TEXT NOT NULL REFERENCES branches(name),
                    observation_key TEXT NOT NULL REFERENCES research_observations(key),
                    first_seen TEXT NOT NULL, PRIMARY KEY(branch, observation_key)
                );
                CREATE TABLE IF NOT EXISTS research_alerts (
                    id INTEGER PRIMARY KEY, alert_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL, created TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS research_forecasts (
                    id TEXT PRIMARY KEY, branch TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_outcomes (
                    forecast_id TEXT PRIMARY KEY REFERENCES research_forecasts(id), payload TEXT NOT NULL
                );
            """)
            errors = self.verify_research()
            if errors:
                raise ValueError("Research integrity check failed: " + "; ".join(errors[:3]))
        except BaseException:
            self.close()
            raise

    def branch_snapshot(self, branch: str) -> tuple[dict, str]:
        row = self.db.execute("SELECT c.state,c.hash FROM branches b JOIN commits c ON c.hash=b.head WHERE b.name=?", (branch,)).fetchone()
        if row is None:
            raise ValueError("Unknown research branch.")
        return json.loads(row[0]), row[1]

    def _branch_commit(self, state: dict, message: str, branch: str, expected: str) -> str:
        row = self.db.execute("SELECT head FROM branches WHERE name=?", (branch,)).fetchone()
        if row is None or row[0] != expected:
            raise ValueError("Research branch changed during the run; review and retry.")
        head = self._insert_commit(state, expected, message)
        self.db.execute("UPDATE branches SET head=? WHERE name=?", (head, branch))
        self._event("commit", {"parent": expected, "head": head}, head, branch_override=branch)
        return head

    def add_source(self, raw: dict, base_dir: Path | None = None) -> dict:
        source = validate_source(raw, base_dir)
        state, head, branch = self.snapshot()
        research = research_state(state)
        if len(research["sources"]) >= 32 and source["id"] not in research["sources"]:
            raise ValueError("A branch supports at most 32 configured sources.")
        research["sources"][source["id"]] = source
        self.commit(state, "Register research source " + source["id"], head, branch)
        return source

    def add_watch(self, raw: dict) -> dict:
        watch = validate_watch(raw)
        state, head, branch = self.snapshot()
        research = research_state(state)
        if len(research["watches"]) >= 64 and watch["id"] not in research["watches"]:
            raise ValueError("A branch supports at most 64 watched theses.")
        research["watches"][watch["id"]] = watch
        research["reports"].pop(watch["id"], None)
        self.commit(state, "Register provisional equity thesis " + watch["id"], head, branch)
        return watch

    def approve(self, watch_id: str) -> dict:
        state, head, branch = self.snapshot()
        research = research_state(state)
        if watch_id not in research["watches"]:
            raise ValueError("Unknown thesis.")
        watch = research["watches"][watch_id]
        for pillar in watch["pillars"]:
            pillar["threshold_origin"] = "approved"
        if watch.get("catalyst"):
            watch["catalyst"]["threshold_origin"] = "approved"
        research["reports"].pop(watch_id, None)
        self.commit(state, "Approve monitoring rules for " + watch_id, head, branch)
        return watch

    def schedule(self, interval_seconds: int, *, branch: str | None = None,
                 model_review: bool = False, now: datetime | None = None) -> dict:
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or not 1 <= interval_seconds <= 604800:
            raise ValueError("Polling interval must be 1 to 604800 seconds.")
        branch = branch or self.branch
        self.branch_snapshot(branch)
        if model_review:
            from dao.provider import Config
            Config.from_env()
        current = stamp(now or utc_now())
        with self.transaction():
            self.db.execute("""INSERT INTO research_jobs(branch,enabled,interval_seconds,next_run,model_review)
                VALUES (?,1,?,?,?) ON CONFLICT(branch) DO UPDATE SET enabled=1,
                interval_seconds=excluded.interval_seconds,next_run=excluded.next_run,model_review=excluded.model_review""",
                            (branch, interval_seconds, current, int(model_review)))
            self._event("research_schedule", {"branch": branch, "enabled": True, "interval_seconds": interval_seconds,
                                               "model_review": model_review}, branch_override=branch)
        return self.job(branch)

    def pause(self, branch: str | None = None) -> None:
        branch = branch or self.branch
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM research_jobs WHERE branch=?", (branch,)).fetchone():
                raise ValueError("No schedule exists for this branch.")
            self.db.execute("UPDATE research_jobs SET enabled=0 WHERE branch=?", (branch,))
            self._event("research_pause", {"branch": branch}, branch_override=branch)

    def job(self, branch: str) -> dict:
        row = self.db.execute("SELECT * FROM research_jobs WHERE branch=?", (branch,)).fetchone()
        if row is None:
            raise ValueError("No research job exists for this branch.")
        return dict(row)

    def jobs(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM research_jobs ORDER BY branch")]

    def _claim(self, branch: str | None, current: datetime, force: bool, model_review: bool | None) -> dict | None:
        timestamp = stamp(current)
        with self.transaction():
            if force:
                branch = branch or self.branch
                self.branch_snapshot(branch)
                self.db.execute("INSERT OR IGNORE INTO research_jobs(branch,enabled,interval_seconds,next_run) VALUES (?,0,900,?)", (branch, timestamp))
                row = self.db.execute("SELECT * FROM research_jobs WHERE branch=?", (branch,)).fetchone()
            else:
                row = self.db.execute("""SELECT * FROM research_jobs WHERE enabled=1 AND next_run<=?
                    AND (owner IS NULL OR lease_until<=?) AND (? IS NULL OR branch=?)
                    ORDER BY next_run,branch LIMIT 1""", (timestamp, timestamp, branch, branch)).fetchone()
            if row is None:
                return None
            job = dict(row)
            if job["owner"] and job["lease_until"] > timestamp:
                if force:
                    raise ValueError("A research worker already owns this branch; retry after it finishes or its lease expires.")
                return None
            owner = uuid4().hex
            self.db.execute("UPDATE research_jobs SET owner=?,lease_until=? WHERE branch=?",
                            (owner, stamp(current + timedelta(seconds=600)), job["branch"]))
            job["owner"] = owner
            if model_review is not None:
                job["model_review"] = int(model_review)
            self._event("research_claim", {"branch": job["branch"], "owner": owner, "forced": force}, branch_override=job["branch"])
            return job

    def _owned(self, job: dict, current: datetime) -> None:
        row = self.db.execute("SELECT owner,lease_until FROM research_jobs WHERE branch=?", (job["branch"],)).fetchone()
        if row is None or row["owner"] != job["owner"] or row["lease_until"] <= stamp(current):
            raise ValueError("Research lease expired or changed; this worker cannot apply results.")

    def _renew(self, job: dict, current: datetime) -> None:
        with self.transaction():
            self._owned(job, current)
            self.db.execute("UPDATE research_jobs SET lease_until=? WHERE branch=?", (stamp(current + timedelta(seconds=600)), job["branch"]))

    def _finish(self, job: dict, current: datetime, error: str | None) -> None:
        with self.transaction():
            row = self.db.execute("SELECT * FROM research_jobs WHERE branch=?", (job["branch"],)).fetchone()
            if row is None or row["owner"] != job["owner"]:
                return
            failures = min(row["failures"] + 1, 100) if error else 0
            delay = row["interval_seconds"] * (2 ** min(failures, 4))
            next_run = current + timedelta(seconds=delay)
            if not error:
                state, _ = self.branch_snapshot(job["branch"])
                for report in state.get("research", {}).get("reports", {}).values():
                    if report.get("next_review_at"):
                        deadline = parse_stamp(report["next_review_at"])
                        if current < deadline < next_run:
                            next_run = deadline
            self.db.execute("""UPDATE research_jobs SET owner=NULL,lease_until=NULL,last_run=?,next_run=?,
                failures=?,last_error=? WHERE branch=?""", (stamp(current), stamp(next_run), failures, error, job["branch"]))
            self._event("research_finish", {"branch": job["branch"], "owner": job["owner"], "error": error,
                                             "failures": failures}, branch_override=job["branch"])

    def _alert(self, key: str, payload: dict, current: datetime, branch: str) -> bool:
        fingerprint = digest(payload)
        row = self.db.execute("SELECT fingerprint FROM research_alerts WHERE alert_key=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        if row and row[0] == fingerprint:
            return False
        cursor = self.db.execute("INSERT INTO research_alerts(alert_key,fingerprint,payload,created) VALUES (?,?,?,?)",
                                 (key, fingerprint, canonical(payload), stamp(current)))
        self._event("research_alert", {"id": cursor.lastrowid, "key": key, "fingerprint": fingerprint,
                                        "payload": payload}, branch_override=branch)
        return True

    def alerts(self, limit: int = 30, include_acknowledged: bool = False) -> list[dict]:
        rows = self.db.execute("SELECT * FROM research_alerts WHERE (? OR acknowledged=0) ORDER BY id DESC LIMIT ?",
                               (int(include_acknowledged), limit))
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def acknowledge(self, alert_id: int) -> None:
        with self.transaction():
            cursor = self.db.execute("UPDATE research_alerts SET acknowledged=1 WHERE id=?", (alert_id,))
            if cursor.rowcount != 1:
                raise ValueError("Unknown alert ID.")
            self._event("research_acknowledge", {"id": alert_id})

    def _ingest(self, source: dict, observations: list[dict], branch: str, job: dict,
                current: datetime) -> int:
        revision = digest(source)
        added = []
        with self.transaction():
            self._owned(job, current)
            for observation in observations:
                key = digest({"payload": observation, "source_revision": revision})
                self.db.execute("INSERT OR IGNORE INTO research_observations VALUES (?,?,?,?,?)",
                                (key, canonical(observation), revision, stamp(current), source["id"]))
                cursor = self.db.execute("INSERT OR IGNORE INTO research_seen VALUES (?,?,?)", (branch, key, stamp(current)))
                if cursor.rowcount:
                    added.append(key)
            if added:
                self._event("research_ingest", {"branch": branch, "keys": added, "source_revision": revision,
                                                "first_seen": stamp(current)}, branch_override=branch)
        return len(added)

    def observations(self, branch: str | None = None, sources: dict | None = None,
                     now: datetime | None = None) -> list[dict]:
        branch = branch or self.branch
        rows = self.db.execute("""SELECT o.*,s.first_seen AS branch_seen FROM research_observations o
            JOIN research_seen s ON s.observation_key=o.key WHERE s.branch=? ORDER BY o.first_seen,o.key""", (branch,))
        revisions = {digest(source) for source in sources.values()} if sources is not None else None
        result = []
        for row in rows:
            payload = json.loads(row["payload"])
            if digest({"payload": payload, "source_revision": row["source_revision"]}) != row["key"]:
                raise ValueError("Evidence content hash mismatch.")
            if revisions is not None and row["source_revision"] not in revisions:
                continue
            if now is not None and parse_stamp(row["branch_seen"]) > now:
                continue
            result.append(payload)
        return result

    def review_document(self, revision: str, note: str) -> None:
        if not isinstance(note, str) or not note.strip() or len(note) > 8000:
            raise ValueError("Document review requires a note of 1 to 8000 characters.")
        branch = self.branch
        documents = [o for o in self.observations(branch) if o["kind"] == "document" and digest(o) == revision]
        if len(documents) != 1:
            raise ValueError("Unknown document revision; use the revision from /reports.")
        with self.transaction():
            self._event("research_document_review", {"branch": branch, "revision": revision,
                "observation_id": documents[0]["id"], "note": note.strip(),
                "limitation": "Review acknowledgement creates no numeric evidence."}, branch_override=branch)

    def _decision_inputs(self, watch: dict, observations: list[dict]) -> list[dict]:
        # Keep the complete evidence ledger; evaluate only current relevant series.
        keys = {(source, p["metric"], p["unit"], p["period"])
                for p in watch["pillars"] for source in p["source_ids"]}
        latest, groups = {}, {}
        currency = (watch.get("valuation") or {}).get("currency")
        for item in observations:
            if item["ticker"] != watch["ticker"]:
                continue
            if item["kind"] == "metric" and (item["source_id"], item["metric"], item["unit"], item["period"]) not in keys:
                continue
            if item["kind"] == "document" or item["kind"] == "price" and item["unit"] != currency:
                continue
            key = (item["source_id"], item["kind"], item["metric"], item["unit"], item["period"])
            date = parse_stamp(item["as_of"])
            if key not in latest or date > latest[key]:
                latest[key], groups[key] = date, [item]
            elif date == latest[key]:
                groups[key].append(item)
        result = [item for group in groups.values() for item in group]
        if len(result) > 10000:
            raise ValueError("More than 10000 tied current evidence revisions; resolve source conflicts before review.")
        return result

    def _pending_documents(self, watch: dict, observations: list[dict], branch: str) -> list[dict]:
        reviewed = {json.loads(r[0])["revision"] for r in self.db.execute(
            "SELECT payload FROM events WHERE kind='research_document_review' AND branch=?", (branch,))}
        latest, groups = {}, {}
        for item in observations:
            if item["ticker"] != watch["ticker"] or item["kind"] != "document":
                continue
            date = parse_stamp(item["as_of"])
            if item["id"] not in latest or date > latest[item["id"]]:
                latest[item["id"]], groups[item["id"]] = date, [item]
            elif date == latest[item["id"]]:
                groups[item["id"]].append(item)
        result = []
        for item in sorted((o for group in groups.values() for o in group), key=lambda o: (o["as_of"], o["id"])):
            revision = digest(item)
            if revision not in reviewed:
                result.append({**{k: item[k] for k in ("id", "title", "citation", "as_of", "source_id", "source_quality")},
                               "revision": revision})
        return result

    def _cases(self, branch: str, state: dict, head: str) -> None:
        # These are valuation alternatives sharing the same evidence, not independent research votes.
        for watch_id, report in state["research"]["reports"].items():
            watch = state["research"]["watches"][watch_id]
            valuation = watch.get("valuation")
            if not valuation:
                continue
            for case in valuation["scenarios"]:
                name = "cases/" + digest(branch)[:8] + "/" + watch_id + "/" + case["name"]
                case_state = deepcopy(state)
                case_state["research"]["scenario_view"] = {"watch_id": watch_id, "scenario": case,
                                                           "research_branch": branch, "snapshot": head}
                existing = self.db.execute("SELECT head FROM branches WHERE name=?", (name,)).fetchone()
                if not existing:
                    self.db.execute("INSERT INTO branches VALUES (?,?)", (name, head))
                    self._event("branch", {"name": name, "head": head}, branch_override=branch)
                    parent = head
                else:
                    parent = existing[0]
                self._branch_commit(case_state, "Review " + case["name"] + " valuation for " + watch_id, name, parent)

    def _forecasts(self, branch: str, state: dict, head: str, observations: list[dict], current: datetime,
                   changed: list[str]) -> None:
        for watch_id, report in state["research"]["reports"].items():
            if watch_id not in changed:
                continue
            values = report.get("valuation") or {}
            price = values.get("current_price")
            expected = values.get("expected_price")
            quote_time = values.get("quote_as_of")
            if report["security_readiness"] != "ready" or price is None or expected is None or not quote_time:
                continue
            watch = state["research"]["watches"][watch_id]
            quotes = [o for o in observations if o["ticker"] == watch["ticker"] and o["kind"] == "price"
                      and o["unit"] == values["currency"] and parse_stamp(o["as_of"]) == parse_stamp(quote_time)
                      and o["value"] == price]
            if not quotes:
                continue
            source_id = sorted(quotes, key=lambda o: o["source_id"])[0]["source_id"]
            record = {"watch_id": watch_id, "ticker": watch["ticker"], "snapshot": head,
                      "watch_revision": digest(watch), "generated_at": stamp(current), "quote_as_of": quote_time,
                      "reference_price": price, "expected_price": expected, "expected_return": values["expected_return"],
                      "currency": values["currency"], "source_id": source_id,
                      "horizon_at": stamp(current + timedelta(days=watch.get("horizon_days", 365)))}
            forecast_id = digest({"branch": branch, "record": record})
            self.db.execute("INSERT OR IGNORE INTO research_forecasts VALUES (?,?,?)", (forecast_id, branch, canonical(record)))
            self._event("research_forecast", {"id": forecast_id, "branch": branch, "record": record}, head, branch_override=branch)

    def _outcomes(self, branch: str, observations: list[dict], current: datetime) -> int:
        added = 0
        rows = self.db.execute("""SELECT f.* FROM research_forecasts f LEFT JOIN research_outcomes o
            ON o.forecast_id=f.id WHERE f.branch=? AND o.forecast_id IS NULL""", (branch,)).fetchall()
        for row in rows:
            forecast = json.loads(row["payload"])
            if parse_stamp(forecast["horizon_at"]) > current:
                continue
            candidates = [o for o in observations if o["kind"] == "price" and o["ticker"] == forecast["ticker"]
                          and o["source_id"] == forecast["source_id"] and o["unit"] == forecast["currency"]
                          and parse_stamp(forecast["horizon_at"]) <= parse_stamp(o["as_of"]) <= current]
            if not candidates:
                continue
            first_time = min(parse_stamp(o["as_of"]) for o in candidates)
            earliest = [o for o in candidates if parse_stamp(o["as_of"]) == first_time]
            if len({o["value"] for o in earliest}) != 1:
                continue
            actual = earliest[0]
            realized = actual["value"] / forecast["reference_price"] - 1
            record = {"forecast_id": row["id"], "ticker": forecast["ticker"], "observed_at": actual["as_of"],
                      "observation_id": actual["id"], "observed_price": actual["value"], "realized_price_return": realized,
                      "expected_return": forecast["expected_return"], "return_error": realized - forecast["expected_return"],
                      "limitation": "Raw price comparison; excludes dividends, splits, fees, execution and benchmark returns."}
            self.db.execute("INSERT INTO research_outcomes VALUES (?,?)", (row["id"], canonical(record)))
            self._event("research_outcome", record, branch_override=branch)
            added += 1
        return added

    def outcomes(self) -> dict:
        return {"forecasts": [{"id": r["id"], "branch": r["branch"], **json.loads(r["payload"])}
                              for r in self.db.execute("SELECT * FROM research_forecasts")],
                "outcomes": [json.loads(r["payload"]) for r in self.db.execute("SELECT * FROM research_outcomes")]}

    def _model_review(self, branch: str, report: dict, current: datetime) -> None:
        from dao.provider import Config, ModelClient
        config = Config.from_env()
        cost = credit(float(os.environ.get("DAO_REQUEST_COST", "1")))
        request_id = uuid4().hex
        self.charge(cost, "model", "Automated opposing-view review", request_id=request_id, model=config.model,
                    research_branch=branch)
        try:
            result = ModelClient(config).complete([{"role": "user", "content":
                "Challenge this equity research report. Identify the strongest opposing interpretation, missing evidence, "
                "and what would invalidate the proposed posture. Do not invent facts or probabilities. Return proposal null. "
                "Your reply is commentary only, not evidence or trade authority."}], report)
        except (ValueError, OSError):
            self.record("model_result", {"request_id": request_id, "success": False,
                                         "prompt_tokens": None, "completion_tokens": None})
            raise ValueError("Automated model review failed; deterministic research remains recorded.") from None
        with self.transaction():
            self._event("model_result", {"request_id": request_id, "success": True,
                "prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens}, branch_override=branch)
            self._event("research_commentary", {"request_id": request_id, "watch_id": report["watch_id"],
                "reply": result.reply, "proposal_ignored": result.proposal is not None,
                "research_as_of": report["as_of"], "report_signature": signature(report)}, branch_override=branch)

    @staticmethod
    def _latch_break(report: dict, watch: dict) -> None:
        report["company_thesis_status"] = "broken"
        report["security_readiness"] = "re_underwrite"
        report["position_action"] = "review_exit" if watch["position"]["current_weight"] > 0 else "re_underwrite"
        report["reasons"][0] = "Current operating metrics are separate from the previously invalidated underwriting."
        report["reasons"].append("An approved kill criterion previously broke this thesis version. Explicitly re-underwrite and register a revised thesis before restoring readiness.")
        for alternative in report["alternatives"]:
            alternative["preferred"] = alternative["action"] == "exit" and watch["position"]["current_weight"] > 0
            if alternative["action"] in ("buy", "wait"):
                alternative["available"] = False

    def _cycle(self, job: dict, clock) -> dict:
        branch = job["branch"]
        state, head = self.branch_snapshot(branch)
        research = research_state(state)
        if not research["watches"]:
            raise ValueError("Register a thesis before running research.")
        errors, ingested = [], 0
        for source in research["sources"].values():
            self._renew(job, clock())
            try:
                self.charge(source["request_cost"], "source", "Research source request", source_id=source["id"], research_branch=branch)
                observations = fetch_source(source, clock())
                ingested += self._ingest(source, observations, branch, job, clock())
            except (ValueError, OSError):
                errors.append("Source unavailable: " + source["id"])
        observations = self.observations(branch, research["sources"], clock())
        reports, changed, new_breaks = {}, [], []
        broken_revisions = {json.loads(row[0])["watch_revision"] for row in self.db.execute(
            "SELECT payload FROM events WHERE kind='research_thesis_break'")}
        for watch_id, watch in research["watches"].items():
            required = {s for p in watch["pillars"] for s in p["source_ids"]}
            missing = ["Source not registered: " + s for s in sorted(required - set(research["sources"]))]
            report = evaluate_watch(watch, self._decision_inputs(watch, observations), clock(), source_errors=errors + missing)
            report["watch_revision"] = digest(watch)
            if report["company_thesis_status"] == "broken":
                if report["watch_revision"] not in broken_revisions:
                    new_breaks.append({"watch_id": watch_id, "watch_revision": report["watch_revision"],
                        "ticker": watch["ticker"], "pillars": [p for p in report["pillars"] if p["status"] == "broken"],
                        "recorded_at": stamp(clock()), "research_branch": branch})
            elif report["watch_revision"] in broken_revisions:
                self._latch_break(report, watch)
            report["pending_documents"] = self._pending_documents(watch, observations, branch)
            if report["pending_documents"]:
                report["gaps"].append("New source documents require human review; their prose has not been promoted to factual metrics.")
                if report["security_readiness"] == "ready":
                    report["security_readiness"] = "conditional"
                if report["position_action"] in ("consider_buy", "hold", "wait_for_proof"):
                    report["position_action"] = "re_underwrite"
                for alternative in report["alternatives"]:
                    alternative["preferred"] = False
                    if alternative["action"] == "buy":
                        alternative["available"] = False
            reports[watch_id] = report
            prior = research["reports"].get(watch_id)
            if prior is None or signature(prior) != signature(report):
                changed.append(watch_id)
        with self.transaction():
            self._owned(job, clock())
            if self.branch_snapshot(branch)[1] != head:
                raise ValueError("Research configuration changed while fetching; evidence retained, decisions deferred.")
            latest_breaks = {json.loads(r[0])["watch_revision"] for r in self.db.execute(
                "SELECT payload FROM events WHERE kind='research_thesis_break'")}
            for watch_id, report in reports.items():
                if report["watch_revision"] in latest_breaks and report["company_thesis_status"] != "broken":
                    self._latch_break(report, research["watches"][watch_id])
                    if watch_id not in changed:
                        changed.append(watch_id)
            for record in new_breaks:
                if not any(json.loads(r[0])["watch_revision"] == record["watch_revision"] for r in self.db.execute(
                        "SELECT payload FROM events WHERE kind='research_thesis_break'")):
                    self._event("research_thesis_break", record, head, branch_override=branch)
            if changed:
                research["reports"] = reports
                head = self._branch_commit(state, "Automated research review", branch, head)
                self._cases(branch, state, head)
                self._forecasts(branch, state, head, observations, clock(), changed)
            for watch_id in changed:
                report = reports[watch_id]
                self._alert(branch + ":thesis:" + watch_id, {"kind": "thesis_update", "branch": branch,
                    "watch_id": watch_id, "ticker": report["ticker"], "company_thesis_status": report["company_thesis_status"],
                    "security_readiness": report["security_readiness"], "position_action": report["position_action"],
                    "reasons": report["reasons"], "gaps": report["gaps"], "report_signature": signature(report)}, clock(), branch)
            self._alert(branch + ":health", {"kind": "source_health", "branch": branch, "errors": errors}, clock(), branch)
            outcomes = self._outcomes(branch, observations, clock())
            self._event("research_cycle", {"branch": branch, "snapshot": head, "new_evidence": ingested,
                "changed_theses": changed, "source_errors": errors, "new_outcomes": outcomes}, head, branch_override=branch)
        review_error = None
        if job["model_review"]:
            reviewed = {(json.loads(r[0])["watch_id"], json.loads(r[0]).get("report_signature")) for r in self.db.execute(
                "SELECT payload FROM events WHERE kind='research_review_completed' AND branch=?", (branch,))}
            for watch_id in reports:
                if (watch_id, signature(reports[watch_id])) in reviewed:
                    continue
                self._renew(job, clock())
                try:
                    self._model_review(branch, reports[watch_id], clock())
                    with self.transaction():
                        self._owned(job, clock())
                        self._event("research_review_completed", {"watch_id": watch_id,
                            "report_signature": signature(reports[watch_id])}, branch_override=branch)
                except (ValueError, OSError) as error:
                    review_error = str(error)
                    with self.transaction():
                        self._alert(branch + ":model", {"kind": "model_review_error", "branch": branch,
                                                       "error": review_error}, clock(), branch)
                    break
        return {"branch": branch, "snapshot": head, "changed_theses": changed, "new_evidence": ingested,
                "new_outcomes": outcomes, "reports": reports, "source_errors": errors, "review_error": review_error}

    def run(self, *, branch: str | None = None, now: datetime | None = None,
            force: bool = True, model_review: bool | None = None) -> dict:
        clock = (lambda: now) if now is not None else utc_now
        current = clock()
        stamp(current)
        job = self._claim(branch, current, force, model_review)
        if job is None:
            return {"type": "idle", "changed_theses": [], "new_evidence": 0}
        try:
            result = self._cycle(job, clock)
            error = "; ".join(result["source_errors"]) or result["review_error"]
            self._finish(job, clock(), error)
            return {"type": "research_cycle", **result}
        except BaseException as error:
            message = "Research interrupted" if isinstance(error, KeyboardInterrupt) else "Research cycle failed; inspect inputs and retry."
            with self.transaction():
                self._alert(job["branch"] + ":worker", {"kind": "worker_error", "branch": job["branch"], "error": message}, clock(), job["branch"])
            self._finish(job, clock(), message)
            raise

    def verify_research(self) -> list[str]:
        if self.db.in_transaction:
            return self._verify_research_snapshot()
        self.db.execute("BEGIN")
        try:
            return self._verify_research_snapshot()
        finally:
            self.db.execute("ROLLBACK")

    def _verify_research_snapshot(self) -> list[str]:
        errors = []
        evidence_events, forecast_events, outcome_events, alert_events, seen_events = {}, {}, {}, {}, {}
        forecast_branches, acknowledged = {}, set()
        for row in self.db.execute("SELECT kind,payload FROM events WHERE kind LIKE 'research_%'"):
            try:
                payload = json.loads(row["payload"])
                if row["kind"] == "research_ingest":
                    for key in payload["keys"]:
                        evidence_events[key] = payload["source_revision"]
                        seen_events[(payload["branch"], key)] = payload["first_seen"]
                elif row["kind"] == "research_forecast":
                    forecast_events[payload["id"]] = payload["record"]
                    forecast_branches[payload["id"]] = payload["branch"]
                elif row["kind"] == "research_outcome":
                    outcome_events[payload["forecast_id"]] = payload
                elif row["kind"] == "research_alert":
                    alert_events[payload["id"]] = payload
                elif row["kind"] == "research_acknowledge":
                    acknowledged.add(payload["id"])
            except (ValueError, KeyError, TypeError):
                errors.append("Malformed research audit event")
        actual_seen = {(r["branch"], r["observation_key"]): r["first_seen"] for r in self.db.execute("SELECT * FROM research_seen")}
        if actual_seen != seen_events:
            errors.append("Evidence memberships differ from audit history")
        observed_keys = set()
        for row in self.db.execute("SELECT * FROM research_observations"):
            try:
                payload = json.loads(row["payload"])
                key = digest({"payload": payload, "source_revision": row["source_revision"]})
                if key != row["key"] or evidence_events.get(key) != row["source_revision"]:
                    errors.append("Evidence revision hash mismatch")
                observed_keys.add(row["key"])
            except (ValueError, TypeError):
                errors.append("Invalid evidence payload")
        if observed_keys != set(evidence_events):
            errors.append("Evidence rows differ from audit history")
        for table, event_map, key_column in (("research_forecasts", forecast_events, "id"),
                                             ("research_outcomes", outcome_events, "forecast_id")):
            try:
                actual = {r[key_column]: json.loads(r["payload"]) for r in self.db.execute("SELECT * FROM " + table)}
                if actual != event_map:
                    errors.append(table + " differs from audit history")
            except (ValueError, TypeError):
                errors.append("Invalid " + table)
        if {r["id"]: r["branch"] for r in self.db.execute("SELECT id,branch FROM research_forecasts")} != forecast_branches:
            errors.append("Forecast branches differ from audit history")
        rows = self.db.execute("SELECT * FROM research_alerts").fetchall()
        if {r["id"] for r in rows} != set(alert_events):
            errors.append("Alerts differ from audit history")
        for row in rows:
            expected = alert_events.get(row["id"], {})
            try:
                if digest(json.loads(row["payload"])) != row["fingerprint"] or row["fingerprint"] != expected.get("fingerprint"):
                    errors.append("Alert hash mismatch")
                if row["alert_key"] != expected.get("key") or bool(row["acknowledged"]) != (row["id"] in acknowledged):
                    errors.append("Alert routing or acknowledgement differs from audit history")
            except (ValueError, TypeError):
                errors.append("Invalid alert payload")
        return errors
