# Technical implementation

Maintenance-only reference for debugging, auditing, or changing deterministic technical evidence.
Routine portfolio analysis need not read this file. The
[technical contract](../tool-contract/core/technical.md) defines invocation, actual selection,
coverage, periods, and interpretation. Implementation lives in
[technical.py](../../portfolio_analyst/technical.py).

## Selection replay input

The eligible population is nonzero held US stocks and ETFs. Account rows are grouped by canonical
`US.<SYMBOL>` and use absolute snapshot weight. Selection has a hard cap of eight:

1. include all identities at or above 5% of NAV;
2. if fewer than five qualify, fill to five from the remaining identities by weight descending;
3. include explicitly named held identities;
4. include identities affected by each of the first three `top_risks`, in risk order;
5. within a rule break ties by weight descending, then canonical identity ascending; truncate at
   eight.

The output records the bounded, deterministically sorted `explicit_requested` identities separately
from selection reasons, the tie-breaker, truncation, and omitted requested identities. This field is
authoritative even when the eight-name cap omits an explicit request, so replay never infers the
request from selected rows. These explain coverage; they are not scores, rankings, recommendations,
or attention labels.

## Audited input boundary

For every selected identity, the package-owned provider makes at most two audited, read-only
`get_kline` calls, each with a five-second timeout:

- daily: forward-adjusted `1d`, regular session, 200-calendar-day window, maximum 160 bars;
- weekly: forward-adjusted `1w`, regular session, 735-calendar-day window, maximum 110 bars.

The request end is the latest modeled completed US regular-session date at or before the embedded
snapshot `as_of`, never the later execution clock. The adapter permits only these exact shapes (plus
the existing bounded daily correlation shape). Response identity, interval, source, OHLCV semantics,
finite Decimal bounds, and end date are validated. Dates are sorted; exact duplicate bars are
deduplicated, while conflicting duplicate dates fail that timeframe. No official `futuapi` script is
modified.

A symbol/timeframe timeout or invalid, empty, future, or missing series is local. It produces an
explicit unavailable/degraded status and leaves unsupported conclusions unknown.

## Deterministic derivation

This is observed price context only, not a forecast or trade signal.

- The deterministic price quantum is inferred from the greatest significant decimal-place count in
  validated current-price and daily/weekly OHLC inputs, clamped between 2 and 4 decimal places. The
  implementation reads each Decimal tuple's exponent and strips coefficient trailing zeroes
  directly; it never calls context-sensitive normalization. The quantum is therefore `0.01`,
  `0.001`, or `0.0001`.
- All selection-weight and technical-price derivation, including replay, runs in an explicit
  600-digit Decimal context with fixed round-half-even rounding. The 600-digit bound covers the
  report contract's at-most-512-character finite Decimal inputs plus bounded aggregation. Ambient
  process precision and rounding cannot change the result. Round-half-even quantization is applied
  before current/latest display prices, moving averages, ATR14, pivots, volume-bin centers, cluster
  comparisons, zone widths, and zone bounds enter derived report fields. Raw normalized OHLCV
  evidence remains unchanged and hashed. This prevents Decimal division tails from masquerading as
  quote precision while keeping replay deterministic.
- Moving averages are arithmetic means of closes for daily 20/50 and weekly 20/40 bars. Missing
  observation counts produce `null`.
- Swing evidence uses a five-bar local window. A swing low is no higher than the two lows on either
  side and strictly below at least one neighbor; swing high is symmetric. Retain the last 12 pivots.
- Volume congestion uses eight equal-width daily price bins. Each bar's volume is assigned using
  typical price `(high + low + close) / 3`; retain the three greatest-volume bins, with lower-bin
  tie-break. Zero total volume is unavailable.
- Candidate levels are quantized swing prices and volume-bin centers. Zone half-width is
  `max(ATR14 / 2, current snapshot price * 0.5%)`, capped at 5% of current price. Cluster candidates
  within one quantized half-width in nearest-to-current order and emit at most two regions per side.
- After quantization, clamp support upper bounds to current price and resistance lower bounds to
  current price, repairing any collapsed interval to a zero-width observed region. Every
  region records lower/upper, kind, method, evidence types, observation count, and evidence date.

Fewer than 20 daily bars yields no zones. Sparse histories and zero volume remain explicitly
unknown. Never use this technical context alone for advice.

## Integrity

The strict JSON report embeds the source snapshot, optional validated risk report, selected
identities, normalized raw bars, and a SHA-256 digest for every series. It binds the snapshot ID,
risk-report digest when present, cutoff, adjustment, session, and algorithm version. Runtime
validation first applies an iterative conservative 4 MiB encoded-JSON budget, counting container
punctuation, escaped UTF-8 keys and values, and scalar representations before materializing the
document as JSON. Lone Unicode surrogates fail closed. A compact `ensure_ascii=False` serialization
then enforces the exact 4 MiB limit. The validator also preflights eight symbols and 2,160 raw bars,
then applies JSON Schema and fully replays selection, normalization, averages, pivots, congestion,
zones, quality, hashes, and report identity. Tampered raw or derived evidence fails validation.
