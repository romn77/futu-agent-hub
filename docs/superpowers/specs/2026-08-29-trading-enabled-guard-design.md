# Trading-Enabled Safety Guard Design

## Goal

Add a fail-closed environment switch that controls every trading write performed by the bundled Futu API scripts. The switch must protect both `SIMULATE` and `REAL` environments without restricting market data, account reads, portfolio analysis, or non-executable previews.

## Configuration

The switch is named `FUTU_TRADING_ENABLED`.

```dotenv
FUTU_TRD_ENV=REAL
FUTU_TRADING_ENABLED=false
```

Only the case-insensitive, whitespace-trimmed value `true` enables trading writes. A missing value, `false`, an empty value, or any other value disables trading. This is intentionally fail-closed.

The repository does not automatically parse `.env`. Callers must export its values before launching a script, for example with `set -a; source .env; set +a`. If they do not, the missing switch disables trading. The project-root `.env` remains ignored by Git and must not contain a trading password.

`FUTU_TRD_ENV` and `FUTU_TRADING_ENABLED` have separate responsibilities:

- `FUTU_TRD_ENV` selects the account data source (`SIMULATE` or `REAL`).
- `FUTU_TRADING_ENABLED` authorizes bundled scripts to reach an SDK trading-write call.

Selecting `REAL` alone never authorizes a trading write.

## Guard Architecture

`scripts/trading_guard.py` will expose one small shared guard, a dedicated error type, and the CLI error formatter. It uses only the Python standard library and has no SDK or OpenD import side effects. The guard reads `FUTU_TRADING_ENABLED` at call time so a new process always receives the current exported configuration. It does not cache the result.

Every bundled script that can mutate trading state imports the pure guard directly and must invoke it after input validation and any non-executable preview, but before creating or using a context for an SDK write. The current protected entry points are:

- `trade/place_order.py`
- `trade/place_combo_order.py`
- `trade/place_crypto_order.py`
- `trade/modify_order.py`
- `trade/cancel_order.py`
- `trade/cancel_crypto_order.py`, including cancel-all

The guard applies equally to simulated and real accounts. It covers order placement, combination placement, modification, cancellation, and cancel-all operations.

Read-only scripts do not call the guard. This includes quotes, account discovery, funds, positions, orders, fills, trading-information queries, portfolio snapshots, risk analysis, and hypothetical rebalance simulation.

## Execution Flow

1. A command validates its arguments.
2. If the command supports a non-executable preview, it may return that preview without requiring trading to be enabled.
3. Immediately before the first possible SDK mutation, the command calls the shared guard.
4. If the switch is not exactly enabled, execution stops before an SDK trading-write call or write-capable context operation.
5. If enabled, the existing flow continues unchanged.
6. Real order placement still requires the existing `--confirmed` second step and manual OpenD GUI unlock.

The switch is an additional control and never replaces real-order confirmation or OpenD's own safeguards.

## Error Behavior

When disabled, the shared guard raises `TradingDisabledError`. CLI entry points catch it, write a clear message to stderr identifying `FUTU_TRADING_ENABLED` as the blocking control, and exit with status `3`. This stays distinct from the existing real-order preview status `2`. The message must not suggest that changing `FUTU_TRD_ENV` enables trading and must not request or expose a trading password.

Invalid switch values fail closed. Provider connections, trade-context construction for mutation, audit success records, and SDK write methods must not run after the guard rejects a command.

## Testing Strategy

Implementation follows red-green-refactor.

Unit tests will verify:

- missing, empty, `false`, and invalid values are rejected;
- `true` is accepted case-insensitively with surrounding whitespace;
- the guard evaluates the environment at call time;
- the disabled error clearly names the switch.

Entry-point regression tests will verify that each bundled mutation path is blocked before its SDK mutation when the switch is disabled, including crypto cancel-all. Tests will also verify that:

- non-executable previews remain available while disabled;
- `SIMULATE` does not bypass the guard;
- `REAL` plus `--confirmed` does not bypass the guard;
- enabled commands retain their existing confirmation behavior;
- representative read-only account and portfolio paths remain unaffected.

No test may connect to OpenD or place, modify, or cancel a real or simulated order.

## Documentation and Local Configuration

The Futu API skill documentation will list `FUTU_TRADING_ENABLED`, its fail-closed default, and its independence from `FUTU_TRD_ENV`. The ignored project `.env` will be updated locally to keep trading disabled by default:

```dotenv
FUTU_TRD_ENV=REAL
FUTU_TRADING_ENABLED=false
```

## Scope Limits

This guard protects only the trading scripts bundled in this repository. It cannot globally block:

- user-authored Python that calls the Futu SDK directly;
- another application connected to OpenD;
- orders entered through moomoo or OpenD interfaces.

Extending the guard by monkey-patching or wrapping the third-party SDK is explicitly out of scope.

## Acceptance Criteria

1. Trading is disabled when `FUTU_TRADING_ENABLED` is absent or is not `true`.
2. All current bundled write entry points stop before their SDK mutation in both `SIMULATE` and `REAL`.
3. Quotes, accounts, positions, orders, portfolio analysis, and previews continue to work while disabled.
4. Enabling the switch does not weaken `--confirmed` or manual OpenD unlock requirements.
5. Tests exercise every current bundled mutation entry point without connecting to OpenD.
6. `.env` remains ignored and contains no credentials.
