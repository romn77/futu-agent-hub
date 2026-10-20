"""Offline cache audit: exercise real orchestration with synthetic provider receipts.

Run from the repository root using the portfolio-analyst test interpreter.
No Futu connection, account discovery, or public network request is performed.
"""

import json
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "Futu API Skills" / "portfolio-analyst"
sys.path[:0] = [str(SKILL), str(SKILL / "tests")]

from portfolio_analyst.current_analysis import CliResult, run_current_analysis
from portfolio_analyst.price_history import FutuAdjustedPriceProvider
from portfolio_analyst.settings import Settings
from portfolio_analyst.technical import FutuTechnicalHistoryProvider
from test_current_analysis_helper import _component, _snapshot


def repeat_analysis(held_options):
    """Identical synthetic snapshot, identical home, two immediate sequential runs."""
    calls = []
    per_run = []
    snapshot = _snapshot(held_options=held_options)
    receipts = {
        name: _component(name, held_options=held_options)
        for name in ("risk", "technical", "options")
    }

    def invoke(arguments):
        calls.append(arguments[0])
        if arguments[0] == "snapshot":
            return CliResult(0, json.dumps(snapshot), "")
        return receipts[arguments[0]]

    with tempfile.TemporaryDirectory(prefix="portfolio-cache-audit-") as temporary:
        private_home = Path(temporary).resolve() / "private"
        private_home.mkdir(mode=0o700)
        for run in (1, 2):
            before = len(calls)
            result = run_current_analysis(settings=Settings(private_home), invoke=invoke)
            per_run.append({
                "run": run,
                "commands": calls[before:],
                "same_snapshot": result["snapshot"]["snapshot_id"] == snapshot["snapshot_id"],
                "remaining_files": len(list(private_home.rglob("*"))),
                "helper_degradations": result["degradations"],
            })
    return {"held_options": held_options, "runs": per_run, "command_counts": dict(Counter(calls))}


class RecordingReader:
    def __init__(self):
        self.requests = []

    def read_private_json(self, command, arguments, *, timeout):
        def arg(flag):
            return arguments[arguments.index(flag) + 1]

        self.requests.append({
            "command": command, "code": arguments[0], "interval": arg("--ktype"),
            "start": arg("--start"), "end": arg("--end"),
            "adjustment": arg("--rehab"), "session": arg("--session"),
            "page_size": arg("--num"),
        })
        return {
            "code": arguments[0], "ktype": arg("--ktype"), "source": "history",
            "data": [{"time": "2026-10-19 00:00:00", "open": 99, "high": 101,
                      "low": 98, "close": 100, "volume": 1000}],
        }


def repeat_history():
    reader = RecordingReader()
    risk = FutuAdjustedPriceProvider(reader)
    technical = FutuTechnicalHistoryProvider(reader)
    for _ in range(2):
        risk.history("AAA", market="US", lookback=120, as_of=date(2026, 10, 19))
        technical.bars("AAA", market="US", interval="1d", as_of=date(2026, 10, 19))
        technical.bars("AAA", market="US", interval="1w", as_of=date(2026, 10, 19))
    return {"adapter_calls": len(reader.requests), "requests": reader.requests}


def repeat_sec_pages():
    sys.path.insert(0, str(ROOT / "Futu API Skills" / "sec-research-core"))
    from sec_research.reader import EdgarReader
    from sec_research.transport import Budget
    from sec_research import cli as sec_cli

    counts = dict(company=0, get_filings=0, text=0)

    class Filing:
        cik = 1
        accession_no = "0000000001-26-000001"
        filing_date = "2026-05-01"
        form = "10-K"
        homepage_url = "https://www.sec.gov/Archives/synthetic"

        def text(self):
            counts["text"] += 1
            return "abcdefghij"

    class Company:
        def __init__(self, cik):
            counts["company"] += 1

        def get_filings(self, **kwargs):
            counts["get_filings"] += 1
            return [Filing()]

    sdk = SimpleNamespace(Company=Company)
    reader = EdgarReader(sdk, Budget())
    for offset in (0, 4):
        reader.content("1", Filing.accession_no, "2026-05-03T00:00:00Z", offset=offset, limit=4)
    same_reader = counts.copy()
    counts.update(dict.fromkeys(counts, 0))

    @contextmanager
    def fake_sdk_session(budget):
        yield sdk

    with patch.object(sec_cli, "sdk_session", fake_sdk_session):
        for offset in (0, 4):
            args = sec_cli.parser().parse_args([
                "filing-content", "--cik", "1", "--accession", Filing.accession_no,
                "--cutoff", "2026-05-03T00:00:00Z", "--offset", str(offset), "--limit", "4",
            ])
            sec_cli.execute(args)
    return {"same_reader_two_pages": same_reader, "two_cli_executions": counts.copy()}


if __name__ == "__main__":
    result = {
        "scope": "offline synthetic fixtures; command counts, not network or latency measurements",
        "repeated_analysis": [repeat_analysis(False), repeat_analysis(True)],
        "repeated_history_provider_reads": repeat_history(),
        "sec_pagination": repeat_sec_pages(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
