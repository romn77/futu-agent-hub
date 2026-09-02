# Comprehensive current-holdings workflow

This is the default orchestration for a new comprehensive analysis. Its unit of evidence is one
fresh portfolio snapshot, not a mixture of saved history and live reads.

## 1. Confirm scope without needless questions

Default to the user's current REAL securities account, USD base currency, and US-listed equity/ETF
holdings. Ask only when an ambiguity prevents reliable account selection, current holdings, NAV, or
USD valuation. A request such as “analyze/review/diagnose my portfolio” selects this route.

## 2. Diagnose and acquire once

Run `doctor` when environment readiness is unknown, then acquire a fresh snapshot with the current
snapshot contract. Do not substitute `--latest` or an old saved snapshot for a new comprehensive
request. Freeze `snapshot_id` and `as_of`; reject accidental later-snapshot mixing.

The default `snapshot --no-save` response is transient. If downstream deterministic commands need a
path, materialize exactly that response as a regular `0600` file inside a newly created private
`0700` temporary directory below `PORTFOLIO_AGENT_HOME`. Use an explicit relative `--snapshot` path
for every downstream command. Remove that temporary directory after the response, including after a
failure. Do not place it in canonical `snapshots/YYYY/MM` history.

## One-shot restricted temporary recipe

Run this from the Skill directory as one Bash invocation. It resolves `Settings.from_env()`, uses
`ensure_private_home(settings.data_home, create=True)`, creates only an exact restricted child,
runs snapshot and risk plus conditional options in the same shell, emits one internal JSON envelope,
and validates the exact target again before cleanup. Capture the envelope for report composition;
do not paste raw JSON into chat or send it to web tools.

```bash
set -euo pipefail
umask 077

portfolio_data_home="$(
python - <<'PY'
from portfolio_analyst.private_home import ensure_private_home
from portfolio_analyst.settings import Settings

settings = Settings.from_env()
print(ensure_private_home(settings.data_home, create=True))
PY
)"
test -n "$portfolio_data_home"
test -d "$portfolio_data_home"
test ! -L "$portfolio_data_home"

portfolio_run_dir="$(mktemp -d -- "$portfolio_data_home/.portfolio-current.XXXXXXXX")"
portfolio_snapshot_path="$portfolio_run_dir/snapshot.json"
portfolio_risk_path="$portfolio_run_dir/risk.json"
portfolio_options_path=""

cleanup_portfolio_run() {
  test -n "${portfolio_run_dir:-}" || return 0
  python - "$portfolio_data_home" "$portfolio_run_dir" <<'PY'
from pathlib import Path
import shutil
import stat
import sys

home = Path(sys.argv[1]).resolve(strict=True)
candidate = Path(sys.argv[2])
if candidate.is_symlink():
    raise SystemExit("refusing to clean a symlink")
resolved = candidate.resolve(strict=True)
if resolved.parent != home or not resolved.name.startswith(".portfolio-current."):
    raise SystemExit("refusing to clean an unconfined path")
metadata = resolved.stat()
if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
    raise SystemExit("refusing to clean a non-private directory")
for child in resolved.iterdir():
    child_metadata = child.lstat()
    if (
        child.is_symlink()
        or not stat.S_ISREG(child_metadata.st_mode)
        or stat.S_IMODE(child_metadata.st_mode) != 0o600
        or child_metadata.st_nlink != 1
    ):
        raise SystemExit("refusing to clean unexpected temporary content")
shutil.rmtree(resolved)
PY
}
trap cleanup_portfolio_run EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

chmod 0700 "$portfolio_run_dir"
python scripts/portfolio.py snapshot --trd-env REAL --base-currency USD --json --no-save \
  > "$portfolio_snapshot_path"
chmod 0600 "$portfolio_snapshot_path"

portfolio_snapshot_rel="$(
python - "$portfolio_data_home" "$portfolio_snapshot_path" <<'PY'
from pathlib import Path
import stat
import sys

home = Path(sys.argv[1]).resolve(strict=True)
target = Path(sys.argv[2])
metadata = target.lstat()
if target.is_symlink() or not stat.S_ISREG(metadata.st_mode):
    raise SystemExit("snapshot temporary file is not regular")
if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
    raise SystemExit("snapshot temporary file is not private")
print(target.resolve(strict=True).relative_to(home))
PY
)"

python scripts/portfolio.py risk --snapshot "$portfolio_snapshot_rel" --json --no-save \
  > "$portfolio_risk_path"
chmod 0600 "$portfolio_risk_path"

if python - "$portfolio_snapshot_path" <<'PY'
from decimal import Decimal, InvalidOperation
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    snapshot = json.load(stream)
try:
    held = any(
        position.get("security_type") == "OPTION"
        and Decimal(str(position.get("quantity"))) != 0
        for position in snapshot["positions"]
    )
except (InvalidOperation, KeyError, TypeError, ValueError):
    raise SystemExit(2) from None
raise SystemExit(0 if held else 1)
PY
then
  portfolio_options_path="$portfolio_run_dir/options.json"
  python scripts/portfolio.py options --snapshot "$portfolio_snapshot_rel" --json --no-save \
    > "$portfolio_options_path"
  chmod 0600 "$portfolio_options_path"
else
  portfolio_option_probe_status=$?
  test "$portfolio_option_probe_status" -eq 1 || exit "$portfolio_option_probe_status"
fi

python - "$portfolio_snapshot_path" "$portfolio_risk_path" "$portfolio_options_path" <<'PY'
import json
from pathlib import Path
import sys

def load(path: str):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)

print(json.dumps({
    "snapshot": load(sys.argv[1]),
    "risk": load(sys.argv[2]),
    "options": load(sys.argv[3]) if sys.argv[3] else None,
}, ensure_ascii=False))
PY
```

`set -e` makes a snapshot/risk/options or validation error fail the invocation. The `EXIT` trap then
cleans only the resolved private child; signal traps exit through that same cleanup. If cleanup
validation detects replacement, linkage, a symlink, a wrong mode, or an unexpected child, it refuses
deletion and reports the exact leftover path for manual inspection. This recipe is an interim prompt-
layer bridge until a lifecycle-free bundled current-analysis command exists.

## 3. Build the portfolio layer

Validate current holdings, NAV, cash, USD basis, and snapshot identity. Run deterministic `risk`
against the bound snapshot, then compose the comprehensive Markdown from snapshot/risk evidence and
the bounded research layers. Reconcile position count and ensure every nonzero current filled holding
will appear in the final table. Keep cash outside the position table. Do not run `analyze`; it reads
local lifecycle records and snapshot history even without output persistence.

If stock shorts make the current deterministic risk calculation unsupported, retain available
snapshot facts, label full short-risk analysis unavailable, and continue the other reliable sections.
Do not change the Python calculation or portray the unsupported result as complete.

## 4. Select and gather deep evidence

Apply the deterministic selection in `research-policy.md`, capped at eight. Gather company,
valuation, news/event, industry/macro sensitivity, and technical evidence only for those names.
Research failure for one name does not erase its baseline holding row.

## 5. Add conditional layers

Run `options` only when option positions exist. Load recommendation/simulation,
trades, performance, lifecycle, candidates, pending orders, or persistence only upon their explicit
triggers in `intent-routing.md`.

## 6. Compose and clean up

Return the output-contract sections as Markdown in chat. Cite all account facts with the same
`snapshot_id` and `as_of`; cite changing external facts with traceable links and timestamps. Show
local gaps next to impacted conclusions and summarize them in the final gaps section.

Delete the restricted temporary snapshot after composing the report. Do not save a canonical
snapshot, report, journal entry, research cache, or history artifact unless the user explicitly asks.
