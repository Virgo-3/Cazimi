# Dao

**Proposed futures must earn permission to happen.**

Dao is a terminal for reasoning backward from a provisional commitment to the
conditions that would justify it. It keeps branchable state, adjudicates current
evidence, accounts for usage, and compares acting with abandonment or waiting.
Contradictory evidence can invalidate the goal it started from.

It runs on **Python 3.11 or later, with no runtime dependencies**. Optional model
conversations suggest prerequisites; the decision engine remains separate.

```text
dao:main> /decide
WAIT
  An available, affordable observation has higher expected value after
  observation and delay costs than immediate commitment or abandonment.
  Act: 12 | Wait: 31.4 | Net information: 19.4

dao:main> /observe positive illustrative-demo "Simulated positive signal"
dao:main> /decide
ACT
  Every required prerequisite is supported, a recovery path is recorded,
  the action is affordable and its expected value is positive.
  Act: 68.57 | Wait: unavailable | Net information: -7

dao:counterexample> /refute risks illustrative-demo "Illustrative risk test failed"
dao:counterexample> /decide
REJECT
  Required prerequisite refuted: risks. A previous approval cannot override
  contradictory evidence.
```

Illustrative transcript, with wrapped output and snapshot acknowledgments omitted.
The numerical assumptions and evidence are examples, not real-world findings.

## Run it

From the repository root:

```console
python -m dao
```

For an optional installed `dao` command:

```console
python -m pip install -e .
dao
```

Try the complete, offline demonstration in a fresh workspace:

```console
python -m dao --workspace .dao/demo-1 --script examples/decision.dao
```

Use an unused directory name when repeating the demonstration. The script saves
an approved local decision record, forks a counterexample branch, records an
illustrative refutation, and finishes with `REJECT`, integrity verification and
usage accounting. It makes no model requests. The default workspace budget is
100 abstract credits; the demonstration consumes 5 for its simulated observation.

The regular state directory is `.dao/`. `--workspace PATH` selects another one.
Repeat `-c "/command"` to run commands in a batch; `--script FILE` reads a UTF-8
command file, ignoring blank lines and lines beginning with `#`. Batch mode stops
on the first error with a nonzero exit code. `--json` emits one JSON record per
command. `--no-color` or the `NO_COLOR` environment variable disables color.

## Plan, challenge, adjudicate

1. Use `/goal TEXT` to create a provisional outcome. The default prerequisites
   are `success`, `risks` and `rollback`.
2. Set `/rollback TEXT` before recording evidence. Add requirements with
   `/require ID TEXT --after OTHER_ID` when one condition depends on another.
   Missing dependencies and cycles are rejected.
3. Record attributed assertions with `/support` and `/refute`. Support is
   insufficient until dependencies are satisfied. Any refutation on a required
   prerequisite rejects the commitment, including one previously approved.
4. Enter explicit assumptions with `/economics`. `/decide` records an audit
   ruling: `ACT`, `REJECT`, `INVESTIGATE`, or `WAIT`.
5. `/act` re-evaluates the current state and, only for `ACT`, exports a decision
   record to `.dao/artifacts/` in the default workspace. It prints a unique
   receipt and an `/undo RECEIPT` command.

`/amend` and a change to `/rollback` clear **all** prerequisite evidence so old
assertions cannot silently approve a changed plan. `/adopt` clears evidence and
resets economics when adopting a model draft. `/import examples/plan.json` loads a
validated, user-supplied plan; imported evidence, if present, is still an assertion.

**The implemented action is a local JSON export.** Dao does not carry out
arbitrary goals, run shell commands, deploy services, or call outside action
tools. `/undo RECEIPT` compensates that local export by removing the unchanged
file; a hash guard refuses removal if the file was modified. The audit history
and consumed credits remain. Restoring or switching state does not undo exports.

## What waiting means

All economics use a consistent, user-chosen utility unit; expenditure amounts
also consume workspace credits. A configured binary symmetric signal has
`signal_accuracy = P(positive | success) = P(negative | failure)`.

```text
act_value  = p * gain - (1 - p) * loss - action_cost
wait_value = sum over signals [P(signal) * max(0, act_value_given_signal)]
             - observation_cost - delay_cost
net_information_value = wait_value - max(0, act_value)
```

For `p=0.6`, `gain=100`, `loss=120`, `action_cost=0`, accuracy `0.8`,
observation cost `5` and delay cost `2`, acting is worth `12`, waiting is worth
`31.4`, and net information value is `19.4`. The available observation is worth
buying under those assumptions. Waiting also needs enough credits for the
observation and a possible subsequent action.

`WAIT` is a recommendation; it does not schedule a task or watch an external
system. `/observe positive|negative SOURCE NOTE` records a **user-supplied** noisy
signal, charges its configured observation cost, applies Bayes' rule and marks
that observation unavailable. It does not collect or verify a measurement.
Prerequisite evidence is separate and must still be adjudicated. Signal accuracy
and payoff assumptions are supplied by the user, with no automatic calibration.
Delay cost is opportunity cost and does not debit the credit ledger.

## Optional model conversations

Without model configuration, Dao is a **manual planning terminal, not an LLM**.
Planning, branching, adjudication and exports work offline. To enable `/ask` or
ordinary conversational text, configure an existing compatible server explicitly:

```powershell
$env:DAO_BASE_URL = "https://your-server.example/v1"
$env:DAO_MODEL = "your-model-id"
# If required, supply DAO_API_KEY through your process environment or secret manager.
python -m dao
```

No provider, model, account or paid service is chosen automatically. The server
must support the Chat Completions endpoint, JSON mode
(`response_format={"type":"json_object"}`), `max_completion_tokens`, and the
structured reply requested by Dao. The base URL may instead name the full
`/chat/completions` endpoint. HTTPS is required except for loopback servers.
The transport follows the [Chat Completions protocol](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

| Variable | Meaning | Default |
| --- | --- | --- |
| `DAO_BASE_URL` | Explicit API base URL or Chat Completions endpoint | Unconfigured |
| `DAO_MODEL` | Explicit model identifier | Unconfigured |
| `DAO_API_KEY` | Optional bearer credential, read from the environment | Empty |
| `DAO_REQUEST_COST` | Abstract credits charged for each model request attempt | `1` |
| `DAO_TIMEOUT` | Request timeout in seconds; greater than 0, at most 300 | `30` |
| `DAO_MAX_OUTPUT_TOKENS` | Completion token limit, from 1 to 32768 | `1600` |

The client does not persist the API key. Conversations, goals and evidence are
stored in plaintext locally; avoid putting credentials in them. Requests transmit
bounded recent conversation and current plan context to the configured server.
Redirects and environment proxies are disabled.

Model replies can stage a proposal. Inspect `/draft`, then use `/adopt` to make
it the provisional plan. Generated content cannot supply evidence, economics or
an approval, and the model has no execution tools. A request is charged before
the call, including failed attempts; model conversations can therefore consume
credits even when no plan changes.

## Commands

```text
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
```

Quote a source or note containing spaces. `REF` is a branch name or an
unambiguous snapshot hash prefix of at least six characters. Economics keys are
`probability`, `gain`, `loss`, `action_cost`, `observation_cost`, `delay_cost`,
`signal_accuracy`, and `observation_available=true|false`. Empty `/economics`
shows the current assumptions.

## State, audit and accounting boundaries

Branches are application snapshots in SQLite, independent of source-code Git
branches. A workspace has one shared active branch; branch and head checks
reject a write if another terminal changed its base. `/restore` appends a
snapshot and preserves history.

Usage is workspace-global and survives checkout and restore. `/usage` shows
abstract credit spending, model request attempts, reported input/output tokens
and requests whose token usage is unknown. Missing token counts stay unknown,
distinct from a reported zero. Credits are **not dollar estimates** and do not
enforce an outside provider's monetary billing cap.

Audit events are hash-linked, and snapshot and completed-artifact hashes can be
checked with `/verify`. This detects inconsistent partial edits. There is no
external anchor, protection against a privileged rewrite of the entire history,
or encryption. Evidence notes remain human or tool assertions, not verified
facts. An audit ruling records the engine's decision under the recorded inputs.

See [the architecture and recovery model](architecture.md) for the component
contracts and limits.

## Validate

```console
python -m unittest discover -s tests -v
```

Tests cover decisions, prerequisites, state/audit integrity, accounting,
reversible exports and the model protocol. HTTP behavior is tested with mocks;
no paid model smoke test is part of the suite. The example script is an offline
integration demonstration.

Released under the [MIT License](../LICENSE).
