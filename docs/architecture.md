# Architecture

Dao starts with a desired future and asks what must be established before it
becomes admissible. The desired future remains provisional throughout the
process: any required prerequisite can disconfirm it. An earlier approval never
overrides current contradictory evidence.

```text
User commands                         Optional configured chat server
     |                                            |
     v                                            v
CLI orchestration <---------------------- bounded proposal adapter
     |                                    suggestion-only draft
     +---------> pure decision engine
     |            validate -> adjudicate -> compare act / abandon / wait
     |
     +---------> SQLite store
                  snapshots + branches + shared active branch
                  hash-linked audit + global budget and usage
                  local export receipts -> artifacts/<receipt>.json
```

## Components and authority

| Component | Responsibility | Boundary |
| --- | --- | --- |
| `dao/engine.py` | Validate plans, evaluate evidence and dependencies, compute expected values | Pure functions; no I/O, model access or stored approval authority |
| `dao/provider.py` | Send bounded conversational requests to an explicitly configured server; validate its structured response | May suggest a goal, prerequisites and rollback text; may not create evidence, economics, approval or actions |
| `dao/store.py` | Transactional snapshots, branch pointers, audit chain, credit ledger, receipts and local artifacts | Usage and action effects survive branch switches and state restoration |
| `dao/cli.py` | Parse commands, invalidate stale evidence after edits, record rulings and coordinate explicit actions | `/act` exports a JSON record; no general-purpose action executor |

No runtime dependency or remote service is needed for manual operation. The
optional transport uses the Python standard library and requires a compatible
Chat Completions server with JSON mode and `max_completion_tokens` support.

## Plans and adjudication

A plan contains a goal, required prerequisites, rollback text, explicit economic
assumptions and a display status. Each prerequisite has an identifier, text,
dependencies, and append-only evidence entries within that snapshot. Evidence
entries record `support` or `refute`, a note and a source. Application snapshots
preserve the earlier versions when a user changes a plan.

Identifiers must be unique and dependencies must form a directed acyclic graph.
Finite bounded inputs are required. The engine normalizes a fresh copy rather
than mutating its caller's plan. A prior `status` string has no authority.

- A refutation on any required node causes `REJECT`, even alongside support.
- A supported node is satisfied only when all of its dependencies are satisfied.
- An unknown prerequisite, an empty prerequisite list or missing recovery path
  prevents `ACT`; unresolved conditions normally produce `INVESTIGATE`.
- An available, affordable observation with greater expected value produces
  `WAIT` unless a prerequisite is refuted.
- With all requirements satisfied and a recovery path recorded, a positive,
  affordable action produces `ACT`; nonpositive action value produces `REJECT`.

`/amend` and changes to `/rollback` clear all current prerequisite evidence.
`/adopt` turns a reviewed model proposal into a provisional plan with empty
evidence and default economics. `/import` validates user-supplied assertions;
it does not establish their truth. Restored evidence is historical assertion
data and may require new review when the external world has changed.

## Uncertainty and the option to wait

The model has two underlying outcomes, success and failure. With prior success
probability `p` and symmetric signal accuracy `a`:

```text
P(positive)           = p*a + (1-p)*(1-a)
P(success | positive) = p*a / P(positive)
P(negative)           = p*(1-a) + (1-p)*a
P(success | negative) = p*(1-a) / P(negative)

A(p) = p*gain - (1-p)*loss - action_cost
W    = sum_s P(s)*max(0, A(P(success | s))) - observation_cost - delay_cost
NIV  = W - max(0, A(p))
```

Impossible signals are skipped. Taking the maximum with zero after each signal
preserves the ability to abandon the action after learning. Waiting must preserve
credits for both the observation and a possible action. `delay_cost` measures
opportunity cost, with no credit debit. Values assume a consistent utility unit.

The worked example `p=0.6`, gain `100`, loss `120`, action cost `0`, accuracy
`0.8`, observation cost `5`, delay cost `2` gives:

| Quantity | Value |
| --- | ---: |
| Immediate action value | 12 |
| Positive-signal probability | 0.56 |
| Success probability after positive signal | 6/7 |
| Success probability after negative signal | 3/11 |
| Expected value after the signal, before observation/delay costs | 38.4 |
| Waiting value | 31.4 |
| Net information value | 19.4 |

These are illustrative inputs. The signal model is binary and symmetric; its
accuracy is assumed known and supplied by the user. Dao does not calibrate
probabilities, estimate payoffs or verify the relationship between a signal and
the world. `/observe` records a supplied signal and updates the prior; it is not
a data-collection tool. Prerequisite evidence remains a separate assertion
channel. `WAIT` creates no scheduler, subscription or background monitor.

## State and concurrency

The workspace stores content-hashed snapshots in `.dao/state.sqlite3` by default.
A snapshot points to one parent, while named branches point to snapshot heads.
Creating a branch does not switch it. Checkout changes the workspace's shared
active branch, including for other processes using the same directory. State
branches are independent of the repository's Git branches.

SQLite write transactions serialize mutations. State commits and local exports
check their expected branch and head so a concurrent update cannot silently
apply an operation against a stale decision. A failed concurrency check requires
the user to inspect current state and retry. A model request already attempted
can remain accounted for when a later state write is rejected.

Restore appends a new snapshot containing prior state. It neither rewrites the
existing ancestry nor rewinds the usage ledger or action receipts. There is no
merge command or claim that checking out a branch forks the external world.

## Local actions and recovery

`/act` obtains a fresh ruling before exporting a commitment. It checks affordability
and the expected branch/head, charges the configured action cost and records an
`artifact_prepared` receipt. It then writes a unique JSON file with exclusive
creation, flushes it, and records completion or failure. The receipt carries the
file hash and the snapshot that authorized it.

Database and filesystem writes are separate phases rather than one atomic
transaction. An interruption can leave a prepared receipt or a file requiring
review. `/receipts` exposes the latest lifecycle state, and `/undo RECEIPT` can
recover a prepared or completed local export. It deletes a present file only if
the recorded hash matches; altered artifacts need manual review. Undo appends
an audit event and preserves spent credits. It cannot reverse an arbitrary
real-world consequence, and rollback text itself is never executed.

## Accounting and audit

Budget, usage, model results and action lifecycle events belong to the whole
workspace. Credit balances use decimal arithmetic over the ledger; changing
branches and restoring snapshots cannot refund them. The budget limits abstract
credits, with a default total of 100. It does not estimate dollars or impose a
monetary limit at the configured service.

Each configured model request attempt is charged before the call using
`DAO_REQUEST_COST` (default 1), including failed attempts. Provider-reported input
and output token counts are recorded separately. Missing usage remains unknown,
while explicit zeros remain known zero counts. Observation and action costs are
also charged; opportunity costs are not. No paid remote model test is required
to run the mocked transport tests.

Audit entries carry the prior event hash. `/verify` checks snapshot content,
the event chain, reconstructed branch pointers and hashes of completed exports.
An adjudication event binds the ruling and its inputs to a snapshot, so review
can inspect the basis for a decision instead of accepting an approval label.
The chain is tamper-evident against inconsistent partial changes, with no external
anchor or protection against a privileged rewrite of the complete database.
Storage is plaintext and unencrypted. Local integrity is not proof that evidence
is correct or that an external action happened.

## Model boundary

The optional adapter sends at most 40 recent conversation messages, bounded text
and current plan context to the explicitly configured server. It allows no tool
requests. Response and proposal validation rejects malformed structures,
non-finite JSON values, prohibited authority fields and invalid dependencies.
HTTPS is required except for loopback; redirects and environment proxies are
disabled. The bearer key is read from the environment and not persisted by the
client. Conversation and plan content are persisted locally and transmitted to
the configured service when requested.

A model can help identify prerequisites and reasons to abandon a commitment.
Its suggestions are not evidence, independent verification or execution. The
user must inspect and adopt a draft before it affects the active plan, then
establish evidence and explicit economics before adjudication can permit export.
