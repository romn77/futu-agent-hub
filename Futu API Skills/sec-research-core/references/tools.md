# Public SEC tool and CLI contract

Prefer existing authorized EdgarTools MCP tools for ordinary question-driven research. Read this file for exact public helper commands or when an MCP response lacks complete text, periods or holdings.

## MCP

- edgar_company / edgar_search identify entities and filings.
- edgar_filing identifies an exact filing; edgar_read reads chosen sections. Retain its accession.
- edgar_notes provides notes; verify which filing it actually chose.
- edgar_trends is a useful lead, not proof of four standalone quarters or provenance.
- edgar_ownership fund_portfolio / portfolio_diff is an overview with limits and automatic period selection. Never infer full-sample exits or allocations from its default top-N output.

Tool availability and the runtime version must be verified from the host. The same name is not a permission grant. On denial, preserve the gap rather than reconstructing a denied interface.

## Interpreter and commands

Run from the repository root with SEC_RESEARCH_PYTHON set to an **existing** interpreter. SDK reads require edgartools 5.62.0; offline JSON analysis uses the Python >=3.10 standard library. YAML is optional and requires PyYAML in that interpreter; use the equivalent JSON configuration when it is absent. Inspect the existing uv tool or supplied installation; never install/upgrade automatically or use a global fallback after an import failure.

EDGAR_IDENTITY must already be supplied to SDK reads through the authorized process environment. Do not print its value or fabricate a name/email. MCP can have its own identity configuration; MCP success does not prove the shell is configured.

The user may persist their request identity in `Futu API Skills/sec-research-core/.env.local` (git-ignored, mode 0600). If the process has no EDGAR_IDENTITY, load only that key from this user-configured file into the command's environment before calling the helper; never execute arbitrary file contents or expose the value. An explicit process value takes precedence. Keep this identity out of institution lists and reports.

Executable commands (replace public IDs and paths with actual inputs):

```bash
"$SEC_RESEARCH_PYTHON" "Futu API Skills/sec-research-core/scripts/sec_read.py" --help
"$SEC_RESEARCH_PYTHON" "Futu API Skills/sec-research-core/scripts/sec_read.py" validate-universe --managers /path/to/user-managers.yaml
"$SEC_RESEARCH_PYTHON" "Futu API Skills/sec-research-core/scripts/sec_read.py" filing-index --cik 320193 --form 10-K --form 10-K/A --start 2025-01-01 --cutoff 2026-10-16T00:00:00Z
"$SEC_RESEARCH_PYTHON" "Futu API Skills/sec-research-core/scripts/sec_read.py" filing-content --cik 320193 --accession 0000320193-25-000079 --section mda --offset 0 --limit 12000 --cutoff 2026-10-16T00:00:00Z
"$SEC_RESEARCH_PYTHON" "Futu API Skills/sec-research-core/scripts/sec_read.py" financial-facts --cik 320193 --accession 0000320193-25-000079 --concept us-gaap:Revenues --cutoff 2026-10-16T00:00:00Z
"$SEC_RESEARCH_PYTHON" "Futu API Skills/sec-research-core/scripts/sec_read.py" collect --managers /path/to/user-managers.yaml --target-period 2026-06-30 --quarters 4 --cutoff 2026-10-16T00:00:00Z
"$SEC_RESEARCH_PYTHON" "Futu API Skills/sec-research-core/scripts/sec_read.py" analyze --managers /path/to/user-managers.yaml --bundle /path/to/same-run-public-bundle.json --enrichment /path/to/verified-public-enrichment.json --quarters 4 --json
```

The accession in these examples is a public historical example, not a default research choice. Resolve actual concept names; an empty result is not zero. The facts reader preserves versions and filters to requested accessions/concepts; no target-price calculation is implied.

For exact raw 13F reads use holdings-13f with --cik, --accession and --cutoff. This alone has discovery_complete=false because a single file does not establish the complete amendment chain. Use collect for full manager-quarter discovery.

All SDK commands accept --seconds (default 600) and --max-bytes (default 209715200). Filing index/collect accept --max-filings (default 100 per manager). Responses are streamed through a read-only SEC-host filter, use temporary SDK state and terminate on denial or resource exhaustion. There is no blanket retry loop on denied operations.

The optional local bridge accepts `--cache-mode off|prefer|refresh_all` (standalone default `off`; cache-aware skills explicitly choose `prefer`). It calls the sibling analyst `scripts/cache.py` or installed `portfolio_analyst.cache.cli` using the existing interpreter; `RESEARCH_CACHE_PYTHON` may select an already installed absolute interpreter. No package is installed automatically. `RESEARCH_CACHE_HOME` selects a private, non-repository public cache root. Cache failures appear in diagnostics and leave the authorized source path available.

`prefer` reuses complete fixed filings and slices cached whole text for pagination. Filing-index validity is one hour and must cover the requested cutoff; collect still rechecks amendment chains. `refresh_all` bypasses evidence hits. The controlled SEC transport reserves shared half-second request slots across cooperating bridge processes, in addition to SDK safeguards. Other MCP/host sources are not covered by this rate budget.

## Outputs

JSON reads return success/data or success=false/error with a code. Exit 2 denotes failure. Partial collection may exit 0; inspect discovery_complete, diagnostics, truncation, issues and analysis status.

collect emits a bundle; analyze accepts either the full collect receipt or its data object. Keep it in host memory or authorized temporary storage for the current run. --output explicitly creates a new destination and refuses to overwrite. No output file is created by default.

analyze defaults to a Markdown report; --json includes full calculations, coverage and provenance. It checks the configured universe version and collected cutoff; refresh the index for a different cutoff. Missing action/classification/relationship evidence yields observations and gaps, not invented qualified signals.

Regular 13F deadlines include weekend and February holiday rolls; extraordinary SEC closures require checking the filing calendar and an explicit target period. [SEC FAQ](https://www.sec.gov/rules-regulations/staff-guidance/frequently-asked-questions-about-form-13f).
