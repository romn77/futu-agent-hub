# Owned temporary-state implementation

Maintenance-only reference for debugging, auditing, or changing the one-shot helper. Routine
portfolio analysis need not read this file. Runtime invocation and result handling belong to
[workflow](../workflow.md); failure decisions belong to
[failure and degradation](../failure-and-degradation.md).

The [current-analysis helper](../../portfolio_analyst/current_analysis.py) owns restricted temporary
state for one-report binding. These are existing implementation guarantees and limitations, not
instructions to create an alternative cleanup routine.

## Startup envelope

Expected startup failures return exit status 2 with a stable JSON envelope: `CONFIG_ERROR` for
invalid settings, `PRIVATE_HOME_ERROR` for an unavailable/unsafe data home, and `IO_ERROR` for
pre-acquisition I/O failure. These messages omit exception detail, tracebacks, and source paths.
Unexpected programming errors and process-control exceptions are not caught.

## Creation, identity checks, and cleanup

The helper resolves `Settings.from_env()` and calls
`ensure_private_home(settings.data_home, create=True)`. It creates one direct mode-0700 temporary
child and mode-0600 regular files. It records the device and inode of the private home, temporary
directory, and every file at creation.

Cleanup validates those identities through anchored directory descriptors before making any change.
It requires the directory entries to equal its explicit whitelist and rechecks the directory and
file identity immediately before each unlink. It verifies the directory identity and remaining entry
set after each deletion, then rechecks before and after removing the empty directory.
It never recursively deletes. If a checked identity changed, a link or unknown entry appeared, or a
check/removal failed, cleanup fails closed, preserves the remaining state, and returns an error
containing the absolute retained path. No cleanup exception or error may omit that path.

Threat model: these checks defend against detected path replacement, symlinks, and accidental or
non-cooperative changes. Portable POSIX APIs provide no portable atomic unlink-if-inode operation;
therefore same-UID malicious concurrent mutation is outside this portable guarantee. The adjacent
pre-unlink and post-deletion checks narrow that time-of-check/time-of-use window and detect many
races, but do not claim an atomic owned-only deletion guarantee against such an attacker.

## Evidence validation and component boundaries

Snapshot acquisition/validation failure stops because current holdings authority is unavailable.
The helper validates the full snapshot schema, content identity and financial semantics before
downstream commands. It validates each successful component's schema/semantics, snapshot ID/as-of,
embedded snapshot when present, and the technical report's exact risk binding. A declared source
mismatch is fatal; malformed component evidence is a local `INVALID_COMPONENT_OUTPUT` degradation.
In contrast, risk, technical, and options failures are local degradation: the helper returns the
failed component as `null`, records its structured error in `degradations`, continues with the
reliable snapshot and other components, and still performs safe cleanup. In particular,
`UNSUPPORTED_PORTFOLIO` for stock shorts does not abort the remaining report.

Successful completion follows guarded cleanup. Local component failure still runs cleanup; cleanup
failure cannot be reinterpreted as a successful report. Cleanup targets owned temporary state and
never canonical user history. No cleanup exception or error may omit the absolute retained path.
