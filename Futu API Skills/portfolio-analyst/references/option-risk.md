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

At the live source boundary, an explicit `SHORT` position with a positive source quantity is
normalized to a negative quantity with an auditable quality note. Explicit `LONG`/`SHORT`
direction conflicts and nonzero quantity/value sign conflicts fail closed. Lifecycle roles such as
`CORE` and `HEDGE` are not directional evidence.

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
wall time to 30 seconds. `quote_acquisition_started_at` independently anchors this Phase 6 budget;
the earlier report `analysis_now` remains snapshot-future authority and does not consume quote time.
A daemon worker applies the remaining per-call/total deadline even if the provider ignores its own
timeout. Its first join timeout quarantines the provider for the rest of that analysis, while three
ordinary consecutive source failures open the circuit. A normalized response that first crosses the
4 MiB total is represented only by its bounded byte count and SHA-256 overflow marker; all later
selected contracts are omitted without another provider call. Remaining contracts stay in structural
metrics and are marked budget-omitted, circuit-open, or unsupported.

Static contract metadata is acquired separately by the package-owned quote-only
`option_metadata_worker.py`. The parent sends one canonical JSON object containing only the validated
`FUTU_OPEND_HOST`, `FUTU_OPEND_PORT`, and at most 30 US/HK identities to `sys.executable -s`; no
other Futu endpoint, account, or credential configuration is inherited. The child creates only
`OpenQuoteContext` at that exact endpoint,
groups the request into at most one
US and one HK `get_stock_basicinfo(..., SecurityType.OPTION, code_list=...)` call, closes the context,
and returns only code, lot size, stock owner, option type, strike date, and strike price. No account,
portfolio, order, credential, or trade context crosses this boundary. The parent applies a five-second
timeout covering the worker and inherited-pipe lifetime, plus a running 256 KiB combined
stdout/stderr budget. On POSIX, the worker starts in a new session; deadline or overflow kills its
entire process group before the direct child is reaped and pipes are closed. The parent never
accumulates or spools unbounded output. Platforms without this process-tree guarantee fail closed as
`OPTION_METADATA_PROCESS_TREE_UNSUPPORTED`. The parent also applies a controlled environment,
strict identity-set and field validation, and stable sanitized failures.

## Raw units and calculations

Every raw Greek remains labelled `RAW_CONTRACT_UNITS_NO_MULTIPLIER`; static metadata never changes
raw-Greek status or aggregate authority. Multiplier-adjusted and money Delta are separate derived
fields and are emitted only from validated static metadata and the embedded snapshot.

- `leg_greek = signed_snapshot_quantity * unit_greek`
- `contract_adjusted_delta_units = leg_greek * contract_multiplier`
- `notional_delta_local = contract_adjusted_delta_units * same-snapshot underlying price`
- `notional_delta_base = notional_delta_local * same-snapshot underlying FX`
- `net_greek = sum(leg_greek)` only with complete population coverage
- `gross_abs_greek = sum(abs(leg_greek))` only with complete population coverage
- `net_market_value_base = sum(signed snapshot market values)`
- `gross_market_value_base = sum(abs(signed snapshot market values))`
- `gross_to_nav = gross_market_value_base / snapshot nav_base`

Delta, Gamma, Vega, Theta, and Rho are parsed independently as finite Decimal values. Missing,
nonfinite, or malformed values remain unavailable and are never filled with zero. A partial sum is
not published as a portfolio total. Valid response metadata may retain `CALL`/`PUT`, expiry, strike,
and implied volatility; invalid metadata is omitted. Implied volatility has its own total/available
leg coverage and status. It remains a nonaggregated raw vendor value with no high/low
interpretation, and inherits the quote's `UNVERIFIED` freshness. Expiry buckets aggregate the same expiry and
record calendar-day DTE separately for every represented market, using the snapshot `as_of` in the
market's local timezone. Negative DTE is explicitly expired; trading days are not inferred.

Static metadata is authoritative for multiplier, underlying, right, expiry, and strike. When static
and quote evidence conflict, the disputed field is suppressed and `OPTION_METADATA_CONFLICT` is
recorded; unrelated fields and raw-Greek status remain intact. The underlying price is usable only
when the exact `stock_owner` exists as a positive stock holding in the same account and every matching
row agrees on local price, currency, and FX. No later quote is fetched. Otherwise money Delta is null
and moneyness is `UNKNOWN`. `ATM` means the absolute price/strike difference is no more than
`max(abs(strike) * 0.001, 0.01)` local currency units; values outside that deterministic band are
`ITM` or `OTM` according to evidenced Call/Put right.

Covered-call and protective-put labels are inventory relationships, not strategy or payoff claims.
For each account and exact underlying, short-Call or long-Put required shares are aggregated across
all relevant legs once (`abs(quantity) * multiplier`) before comparison with positive stock quantity.
The result is `FULL`, `PARTIAL`, `NONE`, or `UNKNOWN`. Long Calls and short Puts are
`NOT_APPLICABLE`. The report does not infer cash-secured puts, spreads, collars, margin, or payoff.
Underlying rows are indexed once by `(account_ref, market-qualified identity)`; duplicate rows retain
the same strict price/currency/FX agreement rule. Quality reasons are deduplicated by
`(code, scope, detail)`, sorted by that key, and capped at 128 with an explicit deterministic
truncation reason if the unique set ever exceeds the bound.

## Status and freshness

- `NOT_APPLICABLE`: every nonzero security type is known and none is `OPTION`.
- `COMPLETE`: the snapshot is complete, all recognized contracts are supported and within budget,
  and all five Greeks cover every option leg.
- `PARTIAL`: at least one Greek is available, but unknown types, unsupported markets, budget
  omissions, incomplete snapshot quality, or incomplete Greek coverage prevent a complete total.
- `UNAVAILABLE`: an option is recognized but no Greek is available, including an unsupported-only
  population or missing capability, SDK/OpenD, timeout, source, or response-contract failures.

A snapshot later than the single trusted UTC analysis instant is rejected as input error
`FUTURE_SNAPSHOT`; no future-dated option report is produced or accepted by runtime replay.

The official response has no trusted server quote time. Every request therefore records trusted
client `request_started_at` and `request_finished_at` bounds while freshness is always
`UNVERIFIED`. Evidence uses finite stable reason codes for capability, timeout, audited read,
source, response-contract, single/total byte budget, total wall budget, and circuit failures; raw
exception text is never serialized. Runtime validation recomputes canonical response byte counts,
overflow progression, provider quarantine/circuit behavior, request order, the independent
acquisition anchor, and both wall limits.

## Output, validation, and non-goals

`options` defaults to no persistence. `--output DIRECTORY` atomically publishes
`portfolio_snapshot.json`, `option_risk.json`, `option_risk.md`, and a manifest last, without
replacement. The JSON embeds the authoritative snapshot. Runtime validation applies the complete
bounded snapshot schema/identity/financial semantics, then reconstructs source selection, quote ordering,
legs, every Greek and coverage count, aggregates, expiry/type buckets, quality reasons, status,
snapshot digest, and the 20-character SHA-256 report identity. Shape, document, operation, and wall
budgets are independent of live acquisition budgets.

New reports use option-risk schema `1.1`. Runtime replay still accepts schema `1.0` reports and
reconstructs their original raw-Greek-only shape. Version `1.1` adds static metadata evidence,
multiplier/money-Delta/moneyness fields, and aggregated underlying relationships; migration does not
rewrite stored `1.0` artifacts.

The report intentionally excludes bid/ask, combination pricing, max profit/loss, breakeven,
probability, margin, scenario P/L, beta adjustment, inferred strategies, and trading advice.
`analyze` embeds this subreport without changing Phase 2's
exactly-three findings, risk policy, or signed-portfolio fail-closed behavior.
