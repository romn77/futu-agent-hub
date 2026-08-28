# Sanitized fixture provenance

`accounts.json`, `portfolio_account_1.json`, and `portfolio_account_2.json` are synthetic,
sanitized fixtures shaped exactly like the audited `get_accounts --json` and
`get_portfolio --json` output contracts. Account IDs, card values, names, and amounts are invented;
they contain no live user data. Position records intentionally do not add currency or security-type
fields because the audited production command does not return them.

`security_metadata.json` is a synthetic `USER_CONFIG` fixture. It is separate from audited command
output and supplies exact-code currency/security metadata without market-prefix or name inference.
`fx_rates.json`, `account_currencies.json`, and `classifications.json` are likewise synthetic
user-configuration fixtures. The FX source label and timestamp describe test evidence only; they are
not a claim about a live rate provider. Account-currency IDs are invented and remain private inputs.
# Phase 4

Lifecycle examples are synthetic and contain no account identifiers or real investment theses.
All persistence and concurrency tests write only below pytest temporary directories.

# Phase 5

`performance_attribution_golden_snippet.json` contains only deterministic synthetic endpoint-P&L
expectations. It deliberately keeps portfolio return and IRR unavailable because no authoritative
external cash-flow source is present in the fixture.
