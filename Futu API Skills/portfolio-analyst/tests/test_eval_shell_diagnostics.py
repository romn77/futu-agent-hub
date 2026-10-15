"""Host startup chatter must not erase an otherwise exact immutable fixture receipt."""

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTICS = "(eval):5: parse error near `end'\n(eval):1: parse error near `end'\n"


def producer():
    spec = importlib.util.spec_from_file_location(
        "shell_receipt_producer", ROOT / "evals/run_codex_evals.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("prefix", ["", DIAGNOSTICS])
def test_exact_successful_login_shell_receipt_retains_snapshot_authority(tmp_path, prefix):
    module = producer()
    case = module.load_cases()["explicit-rebalance-request"]
    workspace = tmp_path / "workspace"
    fixture = module.prepare_workspace(workspace, case["id"])
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "/bin/zsh -lc 'python scripts/current_analysis.py'",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": prefix + json.dumps(fixture),
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Incomplete report"}},
    ]
    audit = module.build_audit(case, events, fixture, workspace)
    assert audit["trace"]["snapshot"]["snapshot_id"] == fixture["snapshot"]["snapshot_id"]
    assert audit["trace"]["observations"]["execution"]["evidence_pass"] is False


@pytest.mark.parametrize(
    "output",
    [
        'unrecognized warning\n{"ok":true}',
        '(eval):1: parse error near `other\'\n{"ok":true}',
        '{"different":true}\n{"ok":true}',
        DIAGNOSTICS + '{"ok":true}\ntrailing text',
    ],
)
def test_other_prefixes_or_extra_documents_are_never_silently_skipped(output):
    with pytest.raises(ValueError):
        producer().parse_command_json(output, "/bin/zsh -lc 'python scripts/current_analysis.py'")


@pytest.mark.parametrize(
    "command",
    [
        "python scripts/current_analysis.py",
        "/bin/zsh -c 'python scripts/current_analysis.py'",
        "/bin/bash -lc 'python scripts/current_analysis.py'",
    ],
)
def test_startup_diagnostics_are_not_accepted_without_the_observed_login_shell(command):
    with pytest.raises(ValueError):
        producer().parse_command_json(DIAGNOSTICS + '{"ok":true}', command)


def test_a_truncated_json_receipt_still_fails():
    with pytest.raises(ValueError):
        producer().parse_command_json(
            DIAGNOSTICS + '{"snapshot":', "/bin/zsh -lc 'python scripts/current_analysis.py'"
        )
