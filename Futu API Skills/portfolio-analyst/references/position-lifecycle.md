# Position lifecycle and journal contract

Lifecycle analysis compares the current immutable snapshot with private, user-authored records. It
does not infer investment intent from price, profit, loss, security name, or role metadata.

## Deterministic states

Every non-zero economic position appears exactly once. A missing record is `UNCONFIGURED` with
`UNREVIEWED` thesis status. A configured current position is `INTACT` unless evidenced findings apply.
Precedence is `BROKEN`, `ROLE_DRIFT`, `WEIGHT_DRIFT`, `HORIZON_DRIFT`, `WEAKENING`, then
`STRENGTHENING`. An absent recorded position is `EXITED`. A current position whose record says
`EXITED` is `BROKEN` because the user fact conflicts with current holdings.

Target bounds are inclusive. Supported horizons are `N` or `N-M` days, weeks, months, or years. The
upper value is authoritative; months equal 30 days and years equal 365 days. Dates compare against
the snapshot's timezone-aware `as_of` calendar date. Unsupported horizons fail rather than guess.

`SATELLITE` role drift requires weight at or above 10% in at least three immutable snapshots spanning
at least 30 days. Without that evidence the report records the check as unavailable/partial. Thesis
states require an explicit user review input and never derive from returns.

Every accepted history observation records the full snapshot ID, timezone-aware `as_of`, canonical
market-qualified identity, and Decimal weight. Inputs are fully schema/identity/financial-semantics
validated, deduplicated, and stably sorted. Observations before the record's `opened_at` date or after
the current snapshot `as_of` are excluded with explicit coverage and quality notes. Runtime report
validation recomputes the eligible window, minimum count, 30-day duration, threshold persistence,
history availability, and `ROLE_DRIFT` result from the serialized evidence; report-ID recomputation
cannot turn an insufficient window into an authoritative role-drift finding.

Human and Markdown views include lifecycle coverage, quality, role/status, current and target bounds,
dates/horizon, thesis review status, every finding and history provenance. The user-authored original
thesis is not printed; renderers expose only a SHA-256/length configuration proof. JSON remains the
strict private authoritative artifact and all displayed untrusted fields are control-safe/escaped.

## Private persistence

`PORTFOLIO_AGENT_HOME` defaults to `~/.moomoo-portfolio-agent` and must be outside the repository.
On POSIX the home and state directories use mode `0700`, files use `0600`, and reads/writes reject
symlinks and hard links. Anchored descriptors, no-follow opens, inode revalidation, no-overwrite
links, locking, fsync, and bounded reads protect the local contract. The Windows fallback rejects
symlinks/junctions/reparse points and revalidates identity, but is explicitly weaker than dirfd.

Snapshots are immutable and content-addressed under `snapshots/YYYY/MM`. Journal decisions and
analysis references are explicit `decisions.jsonl` and `analyses.jsonl` appends. Each record has a
full-body identity and previous-record hash. Existing records are never silently repaired.

Comprehensive analysis treats lifecycle as an independent local module. Invalid or inaccessible
lifecycle configuration produces an `UNAVAILABLE` lifecycle section while the risk report remains
valid and fully attributable to the same `source_snapshot_id`.
