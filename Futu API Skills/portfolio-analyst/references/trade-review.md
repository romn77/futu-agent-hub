# Trade review contract

The authoritative report contract is version `1.6`; earlier reports without total-source
unavailability semantics or canonical context markets are rejected.

`trades` is a read-only review of historical execution records. The inclusive range must be valid,
must not end in the future, and is capped at 90 days. At most 20 securities account contexts and 40
history requests are attempted, with 10-second call and 60-second wall budgets plus a three-failure
circuit breaker.

Coverage markets are canonical evidence keys. Source `HKCC` is recorded as canonical `HK`, matching
normalized orders/fills; non-canonical coverage documents are rejected. If no context completes
successfully—including zero requested contexts because no eligible account exists—top-level quality
is `UNAVAILABLE`, all observed activity metrics are JSON `null`, and Markdown/terminal output says
`Unavailable`. A genuine zero is reported only when at least one context completed successfully,
including a successful empty context.

The two source scripts and their shared dependency are content pinned. Calls require exact account,
market, REAL/SIMULATE environment, start/end, firm, SEC context, JSON, and (for orders) limit 200
options. Credentials, unlock flags, future dates, unbounded limits, and write-capable scripts are
rejected before execution.

Naive source timestamps are interpreted in the security exchange timezone: US New York, HK Hong
Kong, CN Shanghai, JP Tokyo, SG Singapore, MY Kuala Lumpur, CA Toronto, and AU Sydney. IANA timezone
rules provide DST handling. This is a regular-session approximation, not a full exchange holiday or
auction calendar.
Naive timestamps that are ambiguous or nonexistent at a DST transition fail closed. Acquisition
coverage is checked in each requested exchange timezone, and an exchange-local current date is
explicitly partial because the session may still be in progress.

Orders are normalized with masked account/order identity, canonical security, status, side,
quantity, dealt quantity, price, and exchange-local timestamps, then reconciled to normalized fills.
Partial fills, missing/orphan fills, quantity mismatches, and status conflicts are explicit coverage
and quality evidence. Stock fills are grouped by masked account and masked order ID. FIFO matching is isolated by account
and canonical `MARKET.SYMBOL`. Open lots do not enter closed-trip win rate. A sell without evidenced
opening inventory is omitted from FIFO metrics and marks the review partial. Corporate actions,
transfers, cost adjustments, incomplete history, or externally supplied ambiguity notes remain
quality evidence rather than inferred trades. External notes are sanitized and bounded to 20 entries
of at most 512 characters, stored as `external_source_evidence` with `EXTERNAL_SOURCE_NOTE` and
`UNVERIFIED_EXTERNAL`, and rendered as externally provided, independently unverified ambiguity
evidence. Before NFKC, traversal, regex, or encoding, raw input must be a Python string of at most 512
characters; oversize input immediately raises `EXTERNAL_SOURCE_EVIDENCE_TOO_LONG`. A separate bounded
detection views respectively delete Unicode C*/Zl/Zp or map each to one space. Checking both views
reconnects labels/values split internally and also preserves controls used as their delimiter.
Complete password/secret/token/API-key/credential labels followed by `:`, `=`, whitespace, or a
mapped control separator and any nonempty value always fail closed. Bearer plus any nonempty token
also fails closed except the explicit financial phrases bearer shares, securities, bonds,
instruments, or certificates when no later credential punctuation or secret header syntax exists.
The `sk-` rule still requires a complete prefix and sufficiently long suffix. Word boundaries keep
`tokenized` and `secretary` benign. Matches
fail-close the entire note to `[REDACTED_EXTERNAL_CREDENTIAL]`. A separate detection copy removes
whitespace only between digits; a rejoined 8+-digit identifier fail-closes the whole note to
`[REDACTED_EXTERNAL_IDENTIFIER]`. Only a noncredential display copy is
single-line normalized and account-masked. JSON, Markdown, and terminal output use only this sanitized authoritative text; semantic
validation recomputes it and rejects noncanonical text. Internal history-service row-cap, snapshot
turnover, budget, and failure limitations never flow through `source_notes`; coverage/status rebuilds
their canonical internal quality codes. They are never converted into factual or psychological assertions. The audited upstream history fields do not identify
corporate actions, transfers, or cost adjustments, so reports say `UNSUPPORTED_EVIDENCE`; they never
claim those events are absent. Explicitly identified non-trade events remain normalized evidence but
are excluded from FIFO and trade metrics.

Every fill is currency-qualified. Local monetary metrics are emitted only as currency buckets.
Base-currency notional and NAV turnover are computed only when one immutable source snapshot supplies
base currency, NAV, security currency, and FX for every analyzed fill. Snapshot FX is current
valuation evidence, not transaction-date FX. When a fill currency equals `base_currency`, currency
identity is exact 1:1 transaction evidence, so FIFO `realized_pl_base`, average win/loss, payoff,
profit factor, and expectancy are exact. Cross-currency trips remain `null` without transaction-date
FX and make base P&L coverage partial. Per-currency local P&L pools and statistics remain available;
currencies are never added together directly.

Raw order and deal IDs must be non-empty and unique in their account/context. Masked identities are
HMAC-derived from raw identity plus account/canonical market, so aliases and report identity do not
depend on upstream row order. Both response bodies must fit per-context/total row budgets and fully
normalize before the context is atomically successful; duplicate, orphan, status, or queried-market
conflicts fail that context without partial evidence.
Acquisition separately caps 20 unique accounts and 20 account/market contexts; context count is not
misreported as an account limit.
The adapter bounds combined child stdout/stderr at 4 MiB. POSIX uses size-limited temporary capture;
platforms without `resource` use concurrent pipe readers that retain only the remaining byte budget
and immediately terminate/kill the child on overflow. It never uses an unbounded `capture_output`,
`communicate`, or temporary-file path.

An `OrderGroup` is the behavior/frequency trade unit: masked account plus order ID, one canonical
identity and side, summed fill quantity, quantity-weighted fill price, fill count, first/last fill
times, and `effective_at` equal to the first fill. Reversal, reentry, open-window, repeated-symbol,
add-to-position, and maximum-trades-per-day logic uses these groups, so partial fills within one
order never trigger behavior against each other. FIFO lots and monetary P&L continue to use exact
fills. Rates use `trade_count` (order groups), while `fill_count` remains separately reported.

Status reconciliation is a strict matrix. `FILLED_ALL` requires dealt/fill quantity equal order
quantity. `FILLED_PART` and `CANCELLED_PART` require `0 < dealt == fills < quantity`.
`WAITING_SUBMIT`, `SUBMITTING`, `SUBMITTED`, `CANCELLED_ALL`, `FAILED`, `DISABLED`, and `DELETED`
require zero dealt quantity and no fills. Unknown statuses fail closed; every mismatch is partial
quality evidence.

`quality_issues` is the authoritative structured limitation list. Stable codes cover failed or
budget-omitted contexts, current sessions, unsupported corporate-event evidence, orphan/missing/
mismatched/status-conflicting reconciliation, unmatched sells, incomplete FX, unavailable fees,
and total source unavailability. Runtime semantics reconstruct the required code set; free-form
notes cannot erase it. Realized P&L is explicitly `GROSS_PRE_FEE`; when closed trips exist without
fee evidence, `FEES_UNAVAILABLE` is required. Live acquisition admits a context only after both
orders and fills fully normalize and reconcile. The offline builder may represent incomplete
evidence as PARTIAL, but does not weaken live context atomicity.
Code ordering, detail text, and `data_quality_notes` come from the same fixed canonical templates;
rewriting either text surface and recomputing the report hash is invalid.

Analysis uses indexed account/order/security/day maps and explicit operation and wall-clock budgets.
Budget exhaustion fails closed with `ANALYSIS_BUDGET_EXCEEDED`; no partial metric body is fabricated.
Snapshot recency and current-session checks use every requested market's exchange-local date, so a
valid UTC cross-midnight snapshot is not rejected.
Direct analysis accepts at most 20,000 combined normalized input rows, 20,000 groups, and 20,000 FIFO
trip rows. Its cumulative budget is charged during normalization, grouping, FIFO, reconciliation,
metrics, and behavior phases; FIFO uses a deque. Semantic validation independently rejects oversized
documents before JSON Schema traversal or report-ID canonicalization. Its iterative whole-body
preflight caps nesting depth, total nodes, total array items, total map entries, individual/aggregate
string bytes, and normalized JSON bytes. Business caps cover requested/successful/failed/omitted
contexts, policy checks, quality issues/notes, behavior findings and their event/symbol evidence,
unverified interpretations, currency/P&L buckets, symbol turnover, and provenance maps; every schema
array/map publishes a matching `maxItems`/`maxProperties`.

The derived shape budget follows the builder rather than multiplying independent public maxima:
orders plus fills are at most 20,000; groups, trips, and open lots are each no larger than fills;
behavior findings are at most `floor(groups / 2) + 7` (10,007 at the row cap); each finding's event
evidence is no larger than groups, symbols are no larger than its events, and cumulative event and
symbol references are each at most eight times groups. Therefore a valid worst-case body stays below
450,000 array items, 1,500,000 nodes/map entries, 64 MiB aggregate string content, and 80 MiB compact
normalized JSON (with depth 64 and 1 MiB per string). A 10,000-symbol SELL-to-BUY reproduction has
20,000 fills/groups and 10,003 findings and must validate; Cartesian findings-by-evidence payloads
fail early with `VALIDATION_RELATIONSHIP_LIMIT_EXCEEDED`. Validation then applies its own cumulative
operation/wall budget and rebuilds FIFO and indexed evidence in linear time. The 20,000-row acceptance
test uses the generous default budget rather than a fragile machine-specific elapsed-time threshold.

`trade-policy.yaml` uses this strict shape:

```yaml
schema_version: "1.0"
market_open_rules:
  US:
    no_trade_minutes_after_open: 20
  HK:
    no_trade_minutes_after_open: 20
reentry:
  cooldown_minutes: 60
frequency:
  max_round_trips_per_symbol_per_day: 2
  max_trades_per_day: 8
position_management:
  flag_add_to_loser: true
  flag_cost_proximity_stop: true
```

Unknown keys, aliases, collections, negative limits, unsafe modes, symlinks, and replacement during
read fail as `CONFIG_ERROR`. Reports use structured verified findings with affected event IDs,
symbols, and time bounds. Reversal, same-day sell-then-reentry, a fixed informational first-30-minute
session observation, repeat, and add-to-position patterns are reported even without policy. Policy
changes only breach/severity. Interpretations are separate and explicitly unverified; reports never
claim fear, revenge, conviction, or any other motive. Without a configured cooldown or open-window
rule, the corresponding policy metric count and rate are `null`, not zero.
The normalized policy body is embedded in the report. Runtime validation rebuilds configured
open-window, cooldown, daily-frequency, completed-inventory-close, and add-to-position observations
from OrderGroups in each exchange timezone; policy checks and verified findings cannot be edited
independently and rehashed into an apparently valid report.
Same-day reversal and configured open-window rates use total OrderGroup trade count as denominator.
Cost-proximity evidence means an evidenced FIFO exit within 1% of its matched entry price; this is
an observable fixed diagnostic definition, not a claim about intent. Add `--latest` or
`--snapshot PATH` to provide explicit single-snapshot NAV evidence for turnover. Without it,
turnover is unavailable; with it, the report remains partial because period-average NAV is unknown.

No-save is the default. `--output DIR` writes the four immutable artifacts as one transaction:
`normalized_trades.json`, `trade_metrics.json`, `trade_review.json`, and escaped
`trade_review.md`. Report `as_of` is the UTC acquisition timestamp captured after the history reads
through an injectable clock; it is never the requested period end or snapshot timestamp. The
optional `source_snapshot_as_of` is separate. JSON, Markdown, and terminal output include requested,
successful, failed, and budget-omitted masked contexts (including successful empty contexts), range,
currency/quality, and pinned read-command provenance. Raw account IDs never enter coverage.
A context is successful only after both order and deal bodies normalize; a failed second body cannot
leave the first body's rows in report evidence.
