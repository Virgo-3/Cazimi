# Operating Cazimi

Cazimi automates a research process: collect configured observations, preserve
what was known, test explicit theses, compare alternatives, and record meaningful
changes. Its postures are research suggestions. It has no order execution or
portfolio account integration.

## Configure the evidence boundary

Each branch has a registry of at most 32 sources and 64 watched theses. Register a
source with `/source-add "path/to/source.json"`. Relative source locations resolve
against the source definition's directory, not the worker's later working
directory. Filenames with spaces should be quoted; forward slashes also work for
Windows paths.

```json
{
  "id": "company-results",
  "kind": "json",
  "location": "normalized-results.json",
  "category": "company_filings_ir",
  "quality": "primary",
  "ticker": "DEMO",
  "timeout": 15,
  "request_cost": 0,
  "header_env": {}
}
```

This is a schema illustration. Declaring a source `primary` is the registrant's
provenance assertion, not verification by Cazimi. The bundled fictional fixtures
correctly declare `user` quality.

| Source field | Meaning |
| --- | --- |
| `id` | Unique registry key, 1–64 letters/digits/dots/underscores/hyphens, starting with a letter or digit |
| `kind` | `json` for normalized evidence; `rss` for either RSS 2.0 or Atom documents |
| `location` | Explicit local filename or HTTPS URL; HTTP is permitted only for loopback servers |
| `category` | One of the five categories below |
| `quality` | `primary`, `secondary`, or `user`; derived into every ingested observation |
| `ticker` | Optional JSON default/binding; required for RSS/Atom |
| `timeout` | Request timeout in seconds, 0.001–60; default 15 |
| `request_cost` | Nonnegative abstract credits charged per polling attempt; default 0 |
| `header_env` | Map of HTTP header names to environment variable names; default empty |

Categories are `company_filings_ir`, `market_data_estimates`,
`earnings_transcripts_events`, `internal_research`, and
`portfolio_models_trackers`. These organize provenance; they do not grant data
access or add vendor-specific APIs. A service that returns another schema needs
an upstream transformation into the normalized schema. No credentials, licensed
feeds, scraping bypasses, or financial data subscriptions are bundled.

For authenticated HTTPS feeds, use environment variable references:

```json
"header_env": {"Authorization": "CAZIMI_FEED_AUTHORIZATION"}
```

Set that variable in the worker's environment to the complete header value, such
as `Bearer ...`. Secrets are resolved for each request; configuration stores the
variable name. Local files cannot use request headers. URLs and their query
strings, citations, evidence, and reports are persisted: put secrets in headers,
not URLs or source text. Protect the workspace as research data.

Adapters disable redirects and implicit environment/system HTTP proxies. Configure
the final HTTPS URL directly. Embedded URL credentials and fragments are rejected.
Each response/file is limited to 2 MiB and 256 observations or feed entries.
Malformed inputs fail that source; error messages exclude server bodies and
credentials. Timeouts and retry backoff do not guarantee provider availability.

## Supply normalized observations

A JSON feed is an object with an `observations` array:

```json
{
  "observations": [{
    "id": "revenue-2026q3",
    "ticker": "DEMO",
    "kind": "metric",
    "as_of": "2026-10-23T12:00:00Z",
    "metric": "revenue_growth",
    "value": 0.12,
    "unit": "ratio",
    "period": "2026Q3",
    "citation": "https://example.invalid/illustration-only",
    "title": "Illustrative record, not an actual filing"
  }]
}
```

- Every record needs an upstream `id`, ticker (or a registered default), kind,
  timezone-aware ISO `as_of`, and nonempty citation. `title` is optional.
- `metric` records require an explicit metric, finite numeric value, unit, and
  period. Booleans, NaN, infinity, and numeric strings are rejected. A ratio of
  `0.12` means 12% only if your definition says so; units are matched exactly.
- `price` records need a positive value and three-letter currency unit. The
  adapter normalizes `metric` to `price`, `period` to `spot`, and currency to
  uppercase. No currency conversion is performed.
- `document` records contain no numeric value. Their metric/unit/period become
  empty and value becomes null. They are review items, not extracted facts.
- Observation `source_id`, `source_quality`, and `source_category` derive from the
  registered source. Supplied provenance fields cannot override it. IDs are
  prefixed with the source ID; duplicates within one response fail the source.
- Future-dated records fail ingestion. Evidence dates are not replaced with
  collection time. Configure actual publication/availability dates consistently.

RSS/Atom sources require a single ticker in their source definition. Entries use
their title, link, ID, and publication/update date. Missing dates are skipped;
invalid or future dates fail the source. Atom's updated date takes precedence over
its publication date. Relative links on remote feeds resolve against the feed
URL. Missing links fall back to the feed location. UTF-8 XML is required; DTD and
entity declarations are rejected. Cazimi never follows document links to infer
metric values.

New document revisions appear in `pending_documents` and keep readiness
conditional until reviewed. Inspect the report and evidence, then use
`/document-review REVISION NOTE` with the exact revision key. This records your
review note; it does not mark the document's claims true, extract a metric, or
approve thresholds. A changed document payload requires its own review. Optional
model commentary cannot clear this human review requirement.

## State a falsifiable thesis

Register `/watch-add "path/to/watch.json"`; use
[the complete example](../examples/research-watch.json) as a template.

| Watch setting | Purpose |
| --- | --- |
| `id`, `ticker`, `thesis` | Identify the provisional investment case; use ordinary uppercase security symbols up to 24 characters |
| `pillars` | At most 64 explicit claims, each with metric, unit, period, approved source IDs, and a `core` flag |
| `confirm`, `warning`, `kill` | Optional `{ "op": "gte", "value": 0.1 }` tests; operators are `gte`, `gt`, `lte`, `lt` |
| `threshold_origin` | `draft`, `inherited`, or explicitly `approved`; defaults to draft |
| `conflict_tolerance` | Absolute allowed difference between current sources for a pillar; defaults to zero |
| `valuation` | Currency, dated assumptions, exactly bull/base/bear prices and probabilities, return hurdle, and maximum downside fraction |
| `position` | Current and maximum portfolio weights, expressed as fractions from 0 to 1 |
| `catalyst` | Dated future information event and explicit incremental information-value estimates |
| `horizon_days` | Scenario/forecast horizon, 1–3650 days; default 365 |
| `quote_max_age_seconds` | Quote lifetime; default one day |
| `valuation_max_age_seconds` | Valuation assumption lifetime; default 30 days |
| `max_observation_age_seconds` | Metric lifetime; default 90 days |

Scenario probabilities must sum to one. Target prices are positive user
assumptions, expressed at the stated horizon. Approval is a configuration choice:
`/approve WATCH_ID` approves all its pillar and catalyst thresholds, or you can
explicitly import already-approved rules. Draft/inherited thresholds cannot grant
a ready conclusion. Reimporting a watch with draft thresholds makes those rules
draft again.

For matching metric/unit/period/source combinations, the engine considers the
latest dated observations from each source. Tied latest records remain visible;
conflicting values are not averaged. A core pillar needs an approved confirmation
test and fresh matching evidence. Kill tests precede warnings and confirmation.
Unproven core claims block readiness; impaired or broken claims require fresh
underwriting. Secondary/user metric evidence prevents full readiness. A
user-declared price also requires verification before full readiness.

An approved kill criterion latches a break for that exact thesis configuration
across branch switches and restores. Later favorable metrics do not erase it.
Explicit re-underwriting and registration of a revised thesis creates a new
version; the prior invalidation stays in the audit history.

Business thesis, security readiness, and portfolio posture are separate outputs.
Postures include `consider_buy`, `hold`, `review_trim`, `review_exit`,
`re_underwrite`, and `wait_for_proof`. A price hurdle is not a complete investment
recommendation. The portfolio check compares one declared current weight with its
cap; it does not calculate cross-position correlations, liquidity, factor risk,
or executable position sizes.

Waiting uses explicit **incremental expected information value per share**:

```text
net_information = information_value_per_share - cost_per_share - delay_cost_per_share
```

A future, approved catalyst with positive net information value can favor waiting
when the other readiness requirements are met. The estimate represents the
incremental benefit of a better-informed decision, not the full expected payoff
of the investment. These inputs are user estimates, not a calibrated prediction
of what an earnings release will reveal. An expired catalyst creates a review
gap; waiting is not silently extended.

## Run and supervise

`/run` polls the current branch once. `python -m cazimi run --branch NAME` does the
same without changing the interactive checkout. `/reports` reads recorded
reports; it does not refresh evidence. `/evidence TICKER` shows the latest 50
preserved observations. Reusing the same source ID updates its registration; old
source definitions and evidence remain in history. Only evidence tied to current
source definitions is used for new research decisions.

`/schedule 900` enables a branch-pinned schedule, due immediately, with a 900-second
interval. Allowed intervals are 1–604800 seconds. Start
`python -m cazimi watch --workspace PATH` and keep it running. It checks saved
schedules every five seconds by default; `--poll-seconds` accepts 0.1–60 seconds.
`--cycles N` stops after N worker checks, which may be idle checks. One process
handles due jobs sequentially.

The worker persists job state, a 600-second ownership lease, and evidence/report
updates in SQLite. It renews its lease before source and model requests. Another
worker cannot apply the same leased branch cycle. Concurrent configuration
changes defer the decision rather than applying a report against a changed
branch. A crashed worker's job can resume after its lease expires. Failures back
off at 2, 4, 8, then at most 16 times the configured interval; success resets the
failure count.

On successful reviews, the next run also advances to an earlier known catalyst
or freshness deadline when one precedes the regular interval. A worker must
remain running to revisit that gate.

`/jobs` shows schedules and failures. `/pause [BRANCH]` disables a schedule; a
currently running review can finish. Restarting a worker resumes saved enabled
schedules. Schedules are operational records: restoring a research snapshot does
not unschedule a branch. Pause it explicitly. No operating-system autostart,
remote notification delivery, or Codex heartbeat is installed.

The alert outbox preserves thesis changes, errors, and recoveries. Inspect
`/alerts` or `/alerts --all`; acknowledge with `/ack ID`. Acknowledgment does not
change evidence or approve a report. Repeated unchanged results do not create
duplicate change alerts. The normal worker stays quiet for idle/unchanged
cycles; `--json` emits machine-readable cycle records. Any registered source
failure degrades branch research even if earlier observations remain available.

## Optional opposing-view model review

Configure `DAO_BASE_URL`, `DAO_MODEL`, and optional `DAO_API_KEY` in the worker
environment. The endpoint must support the Chat Completions protocol; no provider
is chosen automatically. The endpoint receives report content, including source
citations and user assumptions. `DAO_TIMEOUT` and `DAO_MAX_OUTPUT_TOKENS` configure
the inherited bounded adapter.

Use `/run --model-review`, or save `/schedule 900 --model-review`. Inspect results
with `/reviews`. New report versions trigger commentary; successfully reviewed
unchanged versions make no extra model calls. Failed reviews retry with backoff.
Model output can challenge the conclusion but
cannot edit evidence, approve a threshold, review a document, or submit a trade.
Research records survive a failed optional review.

`/usage` shows cumulative workspace credits and reported token counts. The default
limit is 100 credits; `/budget TOTAL` changes that total. Polling attempts charge
the source's configured `request_cost`; model attempts charge `DAO_REQUEST_COST`
(default one). Credits are abstract accounting units, not dollars. Configure
costs for your use; zero source cost does not establish that an external service
is free. Transport failures can still incur work and do not refund credits;
unknown token counts remain unknown. Branching or restoring state cannot reset
spending.

## Audit and evaluate outcomes

Snapshots, evidence payload revisions, source configuration hashes, first-seen
times, report changes, forecasts, outcomes, and alerts are retained locally.
`/verify` checks their correspondence with audit history. It detects recorded
integrity problems, not falsity in a cited source. Keep backups of the complete
workspace. Research history can be restored; market prices, executions, taxes,
and fees cannot be reversed by a state restore.

Generated `cases/.../bull`, `base`, and `bear` branches preserve scenario views
against shared evidence. Use `/branches`, `/checkout`, `/log`, and `/diff` for
comparison. These branches do not perform three independent investigations.

Eligible ready reports create timestamped forecasts tied to the known snapshot,
quote, watch revision, and source. After the horizon, `/outcomes` compares the
reference price with the earliest unambiguous same-source, same-currency raw
price at or after that horizon and no later than the run time. Missing or
conflicting horizon prices leave the forecast unevaluated. New data does not
rewrite what an earlier forecast knew.

This is forward outcome tracking, not a historical backtest or evidence of
profitable investment skill. Splits, dividends, fees, execution, taxes,
benchmark-relative returns, survivorship, and a complete portfolio model are
outside that raw-price comparison. Tests and bundled demonstrations use
synthetic observations; they do not demonstrate live returns.

The [inherited Dao guide](dao-terminal.md) documents manual commitments,
conversation drafts, action-artifact receipts, and the original decision engine.
Research configuration coexists with those manual tools. A new `/goal` preserves
the research registry; manual `/act` exports an approved plan artifact rather
than executing a securities transaction.
