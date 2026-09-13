import json

import pytest

from portfolio_analyst.current_analysis import CliResult, CurrentAnalysisError, run_current_analysis
from test_account_scope_and_missing_values import _cli_fixture, MissingFieldsAdapter, ACCOUNT_IDS


@pytest.mark.parametrize("kind", ["body_hash", "shape", "semantics"])
def test_invalid_snapshot_is_fatal_before_analysis(tmp_path, monkeypatch, kind):
    settings, real_invoke = _cli_fixture(tmp_path, monkeypatch, MissingFieldsAdapter())
    calls = []

    def invoke(args):
        calls.append(args[0])
        result = real_invoke(args)
        if args[0] == "snapshot":
            body = json.loads(result.stdout)
            if kind == "shape":
                del body["nav_base"]
            else:
                body["nav_base"] = "999999"
                if kind == "semantics":
                    from dataclasses import replace
                    from decimal import Decimal
                    from portfolio_analyst.identity import snapshot_identity
                    from portfolio_analyst.snapshot_io import snapshot_from_document

                    source = snapshot_from_document(json.loads(result.stdout))
                    changed = replace(source, nav_base=Decimal("999999"))
                    body["snapshot_id"] = snapshot_identity(changed)
            return CliResult(0, json.dumps(body), "")
        return result

    with pytest.raises(CurrentAnalysisError, match="snapshot.*invalid"):
        run_current_analysis(settings=settings, invoke=invoke, account_ids=(ACCOUNT_IDS[0],))
    assert calls == ["snapshot"]
    assert {entry.name for entry in settings.data_home.iterdir()} == {"config"}


@pytest.mark.parametrize("component", ["risk", "technical"])
def test_structurally_invalid_component_is_local(tmp_path, monkeypatch, component):
    settings, real_invoke = _cli_fixture(tmp_path, monkeypatch, MissingFieldsAdapter())

    def invoke(args):
        if args[0] == component:
            return CliResult(0, '{"status":"COMPLETE"}', "")
        return real_invoke(args)

    report = run_current_analysis(settings=settings, invoke=invoke, account_ids=(ACCOUNT_IDS[0],))
    assert report[component] is None
    assert report["degradations"][0]["error"]["code"] == "INVALID_COMPONENT_OUTPUT"


@pytest.mark.parametrize("field", ["source_snapshot_id", "as_of"])
def test_valid_but_misbound_component_stops_workflow(tmp_path, monkeypatch, field):
    settings, real_invoke = _cli_fixture(tmp_path, monkeypatch, MissingFieldsAdapter())

    def invoke(args):
        result = real_invoke(args)
        if args[0] == "risk":
            body = json.loads(result.stdout)
            if field == "source_snapshot_id":
                body[field] = body[field][:-16] + "0" * 16
            else:
                body[field] = "2026-08-28T10:00:00Z"
            return CliResult(0, json.dumps(body), "")
        return result

    with pytest.raises(CurrentAnalysisError, match="snapshot.*mismatch"):
        run_current_analysis(settings=settings, invoke=invoke, account_ids=(ACCOUNT_IDS[0],))
