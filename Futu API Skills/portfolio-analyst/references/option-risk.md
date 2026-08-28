# Raw option Greeks risk inventory

Phase 6 inventories option structure and raw unit Greeks from one immutable Phase 1 snapshot. It is
a read-only evidence report, not an option pricer, strategy analyzer, margin model, or trading
recommendation.

## Authority and selection

Only a nonzero snapshot leg with canonical `security_type == OPTION` is an option. `UNKNOWN` never
means not applicable. The snapshot supplies `account_ref`, market, opaque symbol, signed quantity,
and signed base-currency market value; names and codes are not parsed to infer direction or economic
terms. The complete source population is capped at 10,000 positions and is never truncated for
structural metrics.

The official `scripts/quote/get_option_quote.py` file is a capability-specific canonical SHA-256
pin. Its absence or digest mismatch affects only live option quotes. Core accounts, portfolio,
K-line, history, and doctor trust retain their existing full pins. A request is valid only when it
has one canonical positional JSON leg with an opaque `US.` or `HK.` identity, action `BUY`, numeric
quantity `1.0`, followed by `--json`. This is a unit read request, not the snapshot position
direction. Multi-leg, `SELL`, non-unit, extra positional, duplicate, unknown, or credential flags
fail before a subprocess starts.

Supported unique contracts are sorted by gross absolute base market value descending and identity
ascending. The first 30 are requested sequentially, once each even when held in several accounts.
Each call is bounded to 5 seconds and 256 KiB; total response bytes are bounded to 4 MiB and total
wall time to 30 seconds. Three consecutive source failures open the circuit. Remaining contracts
stay in structural metrics and are marked budget-omitted, circuit-open, or unsupported.

## Raw units and calculations

Every Greek is labelled `RAW_CONTRACT_UNITS_NO_MULTIPLIER`. No contract multiplier, underlying,
underlying-share delta, money delta, or beta conversion is guessed.

- `leg_greek = signed_snapshot_quantity * unit_greek`
- `net_greek = sum(leg_greek)` only with complete population coverage
- `gross_abs_greek = sum(abs(leg_greek))` only with complete population coverage
- `net_market_value_base = sum(signed snapshot market values)`
- `gross_market_value_base = sum(abs(signed snapshot market values))`
- `gross_to_nav = gross_market_value_base / snapshot nav_base`

Delta, Gamma, Vega, Theta, and Rho are parsed independently as finite Decimal values. Missing,
nonfinite, or malformed values remain unavailable and are never filled with zero. A partial sum is
not published as a portfolio total. Valid response metadata may retain `CALL`/`PUT`, expiry, strike,
and implied volatility; invalid metadata is omitted. Expiry buckets aggregate the same expiry and
record calendar-day DTE separately for every represented market, using the snapshot `as_of` in the
market's local timezone. Negative DTE is explicitly expired; trading days are not inferred.

## Status and freshness

- `NOT_APPLICABLE`: every nonzero security type is known and none is `OPTION`.
- `COMPLETE`: the snapshot is complete, all recognized contracts are supported and within budget,
  and all five Greeks cover every option leg.
- `PARTIAL`: unknown types, unsupported markets, budget omissions, incomplete snapshot quality, or
  incomplete Greek coverage leave some authoritative evidence but not a complete portfolio total.
- `UNAVAILABLE`: recognized supported options exist but no Greek is available, including missing
  capability, SDK/OpenD, timeout, source, or response-shape failures.
- `FUTURE_SNAPSHOT`: snapshot `as_of` follows the trusted analysis clock; no quote is requested.

The official response has no trusted server quote time. Every request therefore records trusted
client `request_started_at` and `request_finished_at` bounds while freshness is always
`UNVERIFIED`.

## Output, validation, and non-goals

`options` defaults to no persistence. `--output DIRECTORY` atomically publishes
`portfolio_snapshot.json`, `option_risk.json`, `option_risk.md`, and a manifest last, without
replacement. The JSON embeds the authoritative snapshot. Runtime validation applies the complete
snapshot schema/identity/financial semantics, then reconstructs source selection, quote ordering,
legs, every Greek and coverage count, aggregates, expiry/type buckets, quality reasons, status,
snapshot digest, and the 20-character SHA-256 report identity. Shape, document, operation, and wall
budgets are independent of live acquisition budgets.

The report intentionally excludes bid/ask, portfolio or combination pricing, max profit/loss,
breakeven, probability, margin, scenario P/L, money/beta/underlying-share delta, inferred
multiplier/underlying, and trading advice. `analyze` embeds this subreport without changing Phase 2's
exactly-three findings, risk policy, or signed-portfolio fail-closed behavior.
