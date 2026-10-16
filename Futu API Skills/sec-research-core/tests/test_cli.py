import json
from pathlib import Path
import subprocess
import sys

from sec_research.calendar import default_report_period
from test_signals import scenario

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sec_read.py"


def invoke(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)


def test_offline_cli_runs_complete_user_pool_without_sdk_or_account(tmp_path):
    universe, filings, enrichment = scenario()
    config = {"schema_version": 1, "universe_id": "synthetic", "managers": [
        {"cik": m.cik, "family_id": m.family_id, "relationship_source": m.relationship_source}
        for m in universe.managers]}
    pool, bundle = tmp_path / "managers.json", tmp_path / "bundle.json"
    pool.write_text(json.dumps(config))
    bundle.write_text(json.dumps({"schema_version": 1, "data_kind": "synthetic",
                                  "knowledge_cutoff": "2026-08-31T23:59:59Z",
                                  "filings": filings, "enrichment": enrichment}))
    output = invoke("analyze", "--managers", str(pool), "--bundle", str(bundle),
                    "--target-period", "2026-06-30", "--quarters", "2",
                    "--cutoff", "2026-08-31T23:59:59Z", "--json")
    assert output.returncode == 0, output.stderr + output.stdout
    result = json.loads(output.stdout)
    assert result["data"]["data_kind"] == "synthetic"
    assert len(result["data"]["research_leads"]) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bundle.json", "managers.json"]
    markdown = invoke("analyze", "--managers", str(pool), "--bundle", str(bundle),
                      "--target-period", "2026-06-30", "--quarters", "2",
                      "--cutoff", "2026-08-31T23:59:59Z")
    assert "合成数据" in markdown.stdout
    assert "Utilities" in markdown.stdout
    assert "5/5" in markdown.stdout
    assert "INCREASED" in markdown.stdout


def test_cli_does_not_accept_unbounded_or_private_commands():
    assert invoke("trade").returncode != 0
    assert invoke("holdings-13f", "--account-id", "secret").returncode != 0


def test_missing_configuration_fails_without_default_pool(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text('{"schema_version":1,"universe_id":"x","managers":[]}')
    output = invoke("validate-universe", "--managers", str(path))
    assert output.returncode == 2
    assert json.loads(output.stdout)["error"]["code"] == "EMPTY_UNIVERSE"


def test_default_period_waits_for_business_day_deadline():
    assert default_report_period("2026-10-16T12:00:00+08:00") == "2026-06-30"
    # Feb 14, 2026 is Saturday; Feb 16 is Washington's Birthday.
    assert default_report_period("2026-02-17T15:00:00Z") == "2025-09-30"
    assert default_report_period("2026-02-18T05:00:00Z") == "2025-12-31"


def test_live_collection_cannot_use_synthetic_independence_metadata(tmp_path):
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps({"schema_version": 1, "universe_id": "x", "managers": [
        {"cik": "1", "family_id": "x", "relationship_source": "fixture://example"}]}))
    output = invoke("collect", "--managers", str(path))
    assert output.returncode == 2
    assert json.loads(output.stdout)["error"]["code"] == "SYNTHETIC_EVIDENCE_IN_LIVE_RUN"


def test_explicit_output_refuses_to_overwrite_existing_file(tmp_path):
    universe, filings, enrichment = scenario(2)
    path = tmp_path / "pool.json"
    path.write_text(json.dumps({"schema_version": 1, "universe_id": "synthetic",
        "managers": [{"cik": m.cik, "family_id": m.family_id,
                      "relationship_source": m.relationship_source} for m in universe.managers]}))
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"schema_version": 1, "data_kind": "synthetic",
                                  "knowledge_cutoff": "2026-08-31T23:59:59Z",
                                  "filings": filings, "enrichment": enrichment}))
    output = tmp_path / "report.md"
    output.write_text("keep this user document")
    result = invoke("analyze", "--managers", str(path), "--bundle", str(bundle),
                    "--target-period", "2026-06-30", "--quarters", "2", "--output", str(output))
    assert result.returncode == 2
    assert output.read_text() == "keep this user document"
