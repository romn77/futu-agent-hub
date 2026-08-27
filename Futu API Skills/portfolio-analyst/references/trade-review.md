# Trade review contract

`trades` is a read-only review of historical execution records. The inclusive range must be valid,
must not end in the future, and is capped at 90 days. At most 20 securities account contexts and 40
history requests are attempted, with 10-second call and 60-second wall budgets plus a three-failure
circuit breaker.

The two source scripts and their shared dependency are content pinned. Calls require exact account,
market, REAL/SIMULATE environment, start/end, firm, SEC context, JSON, and (for orders) limit 200
options. Credentials, unlock flags, future dates, unbounded limits, and write-capable scripts are
rejected before execution.

Naive source timestamps are interpreted in the security exchange timezone: US New York, HK Hong
Kong, CN Shanghai, JP Tokyo, SG Singapore, MY Kuala Lumpur, CA Toronto, and AU Sydney. IANA timezone
rules provide DST handling. This is a regular-session approximation, not a full exchange holiday or
auction calendar.

Stock fills are grouped by masked account and masked order ID. FIFO matching is isolated by account
and canonical `MARKET.SYMBOL`. Open lots do not enter closed-trip win rate. A sell without evidenced
opening inventory is omitted from FIFO metrics and marks the review partial. Corporate actions,
transfers, cost adjustments, incomplete history, or externally supplied ambiguity notes remain
quality evidence rather than inferred trades.

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
read fail as `CONFIG_ERROR`. Reports identify observed reversal, reentry, open-window, repeat, and
add-to-position patterns. They never claim fear, revenge, conviction, or any other motive.
Same-day reversal and configured open-window rates use total fill count as their denominator.
Cost-proximity evidence means an evidenced FIFO exit within 1% of its matched entry price; this is
an observable fixed diagnostic definition, not a claim about intent. Add `--latest` or
`--snapshot PATH` to provide explicit single-snapshot NAV evidence for turnover. Without it,
turnover is unavailable; with it, the report remains partial because period-average NAV is unknown.

No-save is the default. `--output DIR` writes the four immutable artifacts as one transaction:
`normalized_trades.json`, `trade_metrics.json`, `trade_review.json`, and escaped
`trade_review.md`.
