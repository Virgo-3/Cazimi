# Cazimi

**An automated equity research terminal whose starting thesis can be disproved.**

Cazimi maintains investment theses, polls explicitly configured evidence sources,
and records how new evidence changes a research conclusion. It separates the
business thesis, the stock's readiness for a decision, and the suggested portfolio
review. Competing bull, base, and bear scenarios remain visible alongside proof
points, kill criteria, freshness gaps, and the estimated value of waiting.

Python **3.11+**; **no runtime dependencies**. Research evaluation works without a
language model. Optional model conversations and opposing-view reviews use a
provider you configure.

## Start

From this repository:

```console
python -m cazimi
```

Or install the terminal command:

```console
python -m pip install -e .
cazimi
```

The default local workspace is `.cazimi/`. Nothing is monitored until you register
sources and a thesis. No real securities, market-data subscriptions, accounts, or
credentials are selected automatically.

For an isolated demonstration with generated synthetic data and current dates:

```console
python -m cazimi.demo --workspace .cazimi/demo
python -m cazimi --workspace .cazimi/demo
```

The demo registers fictional rules, approves those sample thresholds, and
demonstrates a change from conditional evidence to a broken thesis. It pauses its
sample schedule before finishing. Its evidence remains **user assertions**, not
verified primary evidence. It does not establish investment performance, submit
orders, start a service, or call paid providers. Use a fresh demo directory.

## Configure research

Inspect the deliberately fictional [source definition](examples/research-source.json),
[observations](examples/research-feed.json), and [thesis](examples/research-watch.json).
They demonstrate the schema with fixed dates on **October 23, 2026**. Their prices,
business figures, probabilities, and ticker are illustrative. The quote becomes
stale after its configured one-day lifetime; old sample dates are not refreshed
or treated as current facts.

To inspect that fixed-date example from the repository root:

```console
python -m cazimi --script examples/research.dao
```

The script imports draft monitoring rules, polls once, and shows the resulting
gaps. Before using your own research, replace the example files with dated,
cited observations and your explicit assumptions. In the terminal:

```text
/source-add "examples/research-source.json"
/watch-add "examples/research-watch.json"
/watches
/run
/reports illustrative-thesis
/approve illustrative-thesis
/run
/evidence DEMO
/verify
```

`/approve` approves that thesis's pillar and catalyst thresholds. Importing a watch
whose thresholds already declare `approved` also constitutes explicit user
configuration authorization. Model text and feed observations cannot approve
rules. Approval does not repair missing, stale, conflicting, or insufficiently
sourced evidence. The included user-sourced example remains conditional.

Use local normalized JSON, HTTPS normalized JSON, or RSS/Atom feeds. Source
categories cover filings/IR, market data/estimates, earnings/events, internal
research, and portfolio/model trackers. RSS and Atom entries are dated document
review items; Cazimi does not extract financial facts from their prose. New
documents require human review before research can regain full readiness;
acknowledging a document does not create numeric evidence.

[The automation guide](docs/automation.md) explains source and thesis schemas,
operating behavior, costs, and recovery.

## Keep it running

Persist a polling schedule for the current research branch:

```text
/schedule 900
/jobs
```

Start a worker in another terminal, using the same workspace:

```console
python -m cazimi watch
```

The worker runs due schedules, preserves evidence revisions and reports, and
records material changes in a local alert outbox. It stays quiet on unchanged
cycles. Schedules survive restarts and remain pinned to their research branch
when an interactive terminal checks out another branch.

```text
/alerts
/ack 1
/pause main
```

The worker must remain running. This repository does not install an operating
system service, startup task, Codex heartbeat, email integration, or brokerage
connection. Restart the same command to resume saved schedules. `Ctrl+C` stops
the process; `/pause` disables a saved schedule, while an in-flight review may
finish. Repeated failures retry with backoff.

One-shot and machine-readable usage:

```console
python -m cazimi run --branch main --json
python -m cazimi reports --branch main
python -m cazimi watch --cycles 3 --poll-seconds 1
python -m cazimi -c "/jobs" -c "/usage"
```

## What a report means

| Assessment | What it evaluates |
| --- | --- |
| Company thesis | Core proof, warnings, and thesis breakers against matching evidence |
| Security readiness | Freshness, provenance, conflicts, valuation, and unresolved gaps |
| Portfolio review posture | Consider buying, hold, review a trim/exit, re-underwrite, or wait |

Scenario probabilities, target prices, return hurdles, position limits, and the
incremental value of future information are supplied assumptions. They are not
learned confidence scores. A positive estimated value of information after
observation and delay costs can favor waiting for an approved future catalyst.
Expired catalysts require review; their expected information is not assumed to
have arrived.

Automatic scenario branches share the same evidence and assumptions. They are
alternative valuation views, not independent research votes. Source quality is
declared by the source registrant; a preserved citation or a `primary` label does
not independently verify the truth of the underlying statement.

An approved kill criterion invalidates that exact thesis version across branches
and restores. A subsequent favorable metric does not silently reinstate it;
explicitly re-underwrite and register a revised thesis to establish a new version.

## Models, usage, and audit

Model access is optional and explicit. Set `DAO_BASE_URL`, `DAO_MODEL`, and, when
required, `DAO_API_KEY` in the worker environment. Use `/run --model-review` or
`/schedule 900 --model-review` for opposing-view commentary on changed reports.
Successfully reviewed unchanged reports make no additional model request; failed
reviews retry with backoff. `/reviews` shows
recorded commentary. Model suggestions cannot change evidence or monitoring
rules, or acknowledge a document on the user's behalf.

`/usage` and `/budget TOTAL` track a workspace-wide allowance, initially 100
abstract credits. Source `request_cost` defaults to zero; `DAO_REQUEST_COST`
defaults to one credit per model attempt. These are configured accounting units,
not dollar quotes or a provider's billing ledger. Attempts are charged before
requests; failures and unknown token usage do not create refunds. External
provider charges can differ.

State snapshots, source revisions, reports, and audit events are preserved in
the local workspace. `/branch`, `/checkout`, `/log`, `/diff`, and `/restore`
support reviewable alternative histories. `/verify` checks recorded integrity;
it cannot prove external facts. Restoring research state does not erase spending
or undo real-world consequences.

`/outcomes` compares eligible timestamped forecasts with later same-source raw
prices at or after their configured horizon. It excludes dividends, splits,
fees, actual execution, and benchmark returns. It is not a backtester, total
return calculation, demonstrated alpha, or a complete portfolio risk model.

## Development and provenance

```console
python -m unittest discover -s tests -v
```

Tests use synthetic fixtures and local HTTP servers; they do not require paid
services. Cazimi extends the [Dao terminal](docs/dao-terminal.md), preserving its
backward planning, branchable state, audit ledger, cumulative usage accounting,
and suggestion-only model adapter. The original manual terminal remains
available with `python -m dao`.

Released under the [MIT License](LICENSE).
