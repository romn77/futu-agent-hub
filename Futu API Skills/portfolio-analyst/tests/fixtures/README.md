# Sanitized fixture provenance

`accounts.json`, `portfolio_account_1.json`, and `portfolio_account_2.json` are synthetic,
sanitized fixtures shaped exactly like the audited `get_accounts --json` and
`get_portfolio --json` output contracts. Account IDs, card values, names, and amounts are invented;
they contain no live user data. Position records intentionally do not add currency or security-type
fields because the audited production command does not return them.

`security_metadata.json` is a synthetic `USER_CONFIG` fixture. It is separate from audited command
output and supplies exact-code currency/security metadata without market-prefix or name inference.
`fx_rates.json` and `classifications.json` are likewise synthetic user-configuration fixtures.
