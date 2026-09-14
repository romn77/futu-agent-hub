# Runtime and diagnostics

Run these commands from the repository root. The existing root virtual environment is the live
runtime; the component virtual environment is the test runtime. Choose the interpreter explicitly.
Do not fall back to a global `python`, change global packages, or reinstall an existing SDK to work
around a different interpreter or a logging permission error.

## Live runtime

```bash
./.venv/bin/python "Futu API Skills/portfolio-analyst/scripts/portfolio.py" doctor --json --no-save
```

The JSON records the actual interpreter executable, Python version, and virtual-environment prefix
under `runtime`. The worker uses that same interpreter with user-site imports disabled and a
controlled environment. It does not inherit Python import overrides, login credentials, or account
IDs. `--no-save` prevents saving a doctor report; importing the Futu SDK may still write its own
normal local logs.

The `sdk` check separates these facts:

| Field | Meaning |
| --- | --- |
| `installed` | Whether `futu-api` distribution metadata exists in this interpreter; `null` means unverified. |
| `package_version` | The installed distribution version, retained even if import fails or times out. |
| `package_status` | `installed`, `not_installed`, or a metadata/worker failure reason. |
| `imported` | Whether importing `futu` succeeded; `null` means the import could not be verified. |
| `quote_api_available` | Whether `OpenQuoteContext` is callable, exposes callable `get_global_state` and `close` methods (including inherited methods), and the SDK exposes `RET_OK`; `null` means unverified. |
| `available` | Import and required quote API checks both succeeded. |
| `version` | Installed distribution version, falling back to the imported module version when metadata is absent. |
| `status` | Import result or failure reason. |

The import stage checks the quote context methods on the class without constructing a context.
Package metadata and SDK import each run in a separate subprocess with a four-second deadline.
OpenD gets its own four-second subprocess only after SDK validation succeeds. The total worker
budget is at most twelve seconds, plus process launch/cleanup overhead; this is not a shared
four-second deadline. The OpenD stage first attempts a TCP connection (two-second socket timeout)
and emits its result before importing the SDK again and starting the quote handshake. Its outer
process deadline also bounds DNS, SDK startup, handshake, and context cleanup.

`opend.reachable: true` proves a TCP connection succeeded, which alone does not identify an OpenD
server. Only a successful `OpenQuoteContext.get_global_state()` response and context cleanup make
`opend.available` true. A stalled handshake retains TCP reachability and the earlier SDK facts.
The diagnostic never creates a trade context, reads accounts, unlocks trading, or submits orders.

| Status | Interpretation |
| --- | --- |
| SDK `missing_sdk` | The `futu` module is missing in the selected interpreter. |
| SDK `missing_dependency` | Import reached a missing dependency; this is distinct from a missing `futu` module. |
| SDK `permission_denied` | Import raised `PermissionError`; check the SDK log-directory permissions for this interpreter. The package may still be installed. |
| SDK `missing_quote_api` | Import succeeded, but required quote API symbols are absent. |
| SDK `timeout` | Import was not verified before its deadline; package metadata remains independent. |
| OpenD `not_attempted` | SDK validation did not succeed. |
| OpenD `connection_refused` | The endpoint refused the TCP connection. |
| OpenD `connection_timeout` | Connection setup exceeded its deadline. |
| OpenD `connection_permission_denied` | The runtime was denied permission to open the connection. |
| OpenD `sdk_import_timeout` / `sdk_import_failed` | TCP succeeded, but SDK preparation in the OpenD worker did not finish successfully. Earlier SDK validation remains recorded. |
| OpenD `handshake_timeout` | TCP succeeded, but the quote handshake or its cleanup exceeded the deadline. |
| OpenD `handshake_failed` | TCP alone was insufficient: the quote response, API call, or cleanup failed. |

Unexpected worker exits or malformed responses are reported as `worker_failed` or
`invalid_response`. A failed OpenD worker retains any validated TCP reachability it already emitted,
while `opend.available` remains false even if an earlier payload claimed success. Exception messages, SDK output, and quote response data are not copied into the
diagnostic JSON. `ok: false` / exit code 1 means at least one required check is unavailable; it does
not by itself mean the SDK is uninstalled.

## Automated tests

The component test environment can intentionally omit `futu-api`; tests exercise SDK/OpenD results
with controlled substitutes. From the repository root:

```bash
"Futu API Skills/portfolio-analyst/.venv/bin/python" -c 'import sys; print(sys.executable); print(sys.version)'
(cd "Futu API Skills/portfolio-analyst" && .venv/bin/python -m pytest)
```

For diagnostic regression tests only:

```bash
(cd "Futu API Skills/portfolio-analyst" && .venv/bin/python -m pytest tests/test_doctor.py tests/test_skill_trust.py tests/test_cli_mutation_gate.py)
```

A test uses an ephemeral loopback TCP listener to distinguish an unrelated service from an OpenD
handshake. It requires permission to bind a local socket. A sandbox denial is an execution
restriction, not evidence that this protocol regression failed. Do not delete or skip the assertion
to hide that restriction.

When testing a separate worktree, run from that worktree's component directory and use the absolute
path to the existing component test interpreter. Likewise, run the worktree's
`scripts/portfolio.py` with the absolute root runtime interpreter; do not create an implicit global
fallback or assume the worktree has a copied `.venv`.

## Verified environment, 2026-09-13

The corrected diagnostic was run from the `portfolio-correctness` worktree using
`/Users/yaoma/project/futu-agent-hub/.venv/bin/python` (Python 3.13.3):

- In the filesystem sandbox, `futu-api` 10.10.7008 was installed, while SDK import reported
  `permission_denied`; its version was retained and OpenD was not attempted.
- With the SDK's normal local log writes allowed, installation, import, and quote API validation
  all succeeded. OpenD at `127.0.0.1:11111` reported `connection_refused`; overall exit code was 1.

This verifies the unavailable-OpenD diagnostic path. A real successful OpenD handshake and a live
account analysis remain unverified in this environment. Synthetic quote tests do not replace those
integration checks.
