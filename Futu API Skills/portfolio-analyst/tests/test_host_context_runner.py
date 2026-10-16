"""Only immutable synthetic inputs can reach the optional host-context launcher."""

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "evals" / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def source(tmp_path):
    producer = module("run_codex_evals")
    folder = tmp_path / "source"
    folder.mkdir()
    producer.prepare_workspace(folder / "workspace", "explicit-rebalance-request")
    (folder / "prompt.txt").write_text("Analyze this entirely synthetic portfolio; no simulation.")
    return folder


@pytest.mark.parametrize(
    "tamper",
    ["real_version", "real_script", "extra_private_file", "nested_private_file", "extra_script"],
)
def test_real_or_nonfixture_account_path_is_rejected_before_launch(tmp_path, tamper):
    folder = source(tmp_path)
    if tamper == "real_version":
        path = folder / "workspace/.fixture/envelope.json"
        value = json.loads(path.read_text())
        value["snapshot"]["source_version"] = "LIVE_SOURCE"
        path.write_text(json.dumps(value))
    elif tamper == "real_script":
        (folder / "workspace/scripts/current_analysis.py").write_text(
            "print('not the synthetic fixture')"
        )
    elif tamper == "extra_private_file":
        (folder / "workspace/personal-report.md").write_text("private")
    elif tamper == "nested_private_file":
        (folder / "workspace/references/private-note.md").write_text("private")
    else:
        (folder / "workspace/scripts/extra_account_helper.py").write_text("print('not allowed')")
    with pytest.raises(ValueError):
        module("run_host_context").prepare(folder, tmp_path / "run", mode="host-context")


@pytest.mark.parametrize(
    "extra_command",
    [None, "python scripts/extra_account_helper.py", "python scripts/portfolio.py snapshot"],
)
def test_exact_cli_input_and_original_file_read_gate_are_kept_separate(tmp_path, extra_command):
    host = module("run_host_context")
    folder = source(tmp_path)
    prepared = tmp_path / "run"
    host.prepare(folder, prepared, mode="host-context")

    def runner(command, **kwargs):
        assert command[-1] == "-" and command[command.index("--sandbox") + 1] == "read-only"
        assert kwargs["input"] == (prepared / "payload.txt").read_bytes()
        fixture = json.loads((prepared / "workspace/.fixture/envelope.json").read_text())
        snapshot = fixture["snapshot"]
        headings = module("policy_context").DEFAULT_DOCUMENTS
        assert b"<host-policy" in kwargs["input"] and b"SKILL.md" in kwargs["input"]
        assert len(headings) == 12
        report = f"## Scope and data time\n{snapshot['snapshot_id']} {snapshot['as_of']}\n"
        report += "## One-line conclusion\nSynthetic.\n## Overview\nSynthetic.\n## Three principal risks\nSynthetic.\n## Complete holdings table\n"
        report += "| Symbol | Weight | Market value | Unrealized P&L |\n|---|---:|---:|---:|\n"
        for row in snapshot["positions"]:
            report += f"| US.{row['symbol']} | {float(row['weight']) * 100}% | {row['market_value_base']} | {row['unrealized_pl_base']} |\n"
        report += "## Selected deep research\nSynthetic.\n## Macro impact\nSynthetic.\n## Observations\nSynthetic.\n## Data gaps and sources\nSynthetic.\n"
        events = [
            {"type": "thread.started"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "helper",
                    "type": "command_execution",
                    "command": "python scripts/current_analysis.py",
                    "aggregated_output": json.dumps(fixture),
                    "status": "completed",
                    "exit_code": 0,
                },
            },
            {
                "type": "item.completed",
                "item": {"id": "report", "type": "agent_message", "text": report},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30},
            },
        ]
        if extra_command:
            events.insert(
                -2,
                {
                    "type": "item.completed",
                    "item": {
                        "id": "extra",
                        "type": "command_execution",
                        "command": extra_command,
                        "aggregated_output": "not authorized",
                        "status": "completed",
                        "exit_code": 0,
                    },
                },
            )
        return subprocess.CompletedProcess(
            command, 0, "\n".join(json.dumps(e) for e in events).encode(), b""
        )

    result = host.execute(prepared, cli="synthetic-test-cli", runner=runner)
    assert result["execution_evidence_pass"] is (extra_command is None)
    if extra_command is None:
        assert result["policy_access"]["reuse_pass"] is True
    original = json.loads((prepared / "audit.json").read_text())
    assert (
        original["trace"]["observations"]["execution"]["checks"][
            "required_reference_reads_complete"
        ]
        is False
    )
    assert original["trace"]["loaded_references"] == []
    assert result["delivery"]["status"] == "VERIFIED_CLI_INPUT"


def test_changed_payload_stops_before_any_model_invocation(tmp_path):
    host = module("run_host_context")
    prepared = tmp_path / "run"
    host.prepare(source(tmp_path), prepared, mode="host-context")
    (prepared / "payload.txt").write_text("truncated")

    def forbidden_runner(*args, **kwargs):
        pytest.fail("changed payload reached the model")

    with pytest.raises(ValueError):
        host.execute(prepared, cli="synthetic-test-cli", runner=forbidden_runner)


def test_existing_run_cannot_be_overwritten(tmp_path):
    host = module("run_host_context")
    folder = source(tmp_path)
    prepared = tmp_path / "run"
    host.prepare(folder, prepared, mode="host-context")
    with pytest.raises(FileExistsError):
        host.prepare(folder, prepared, mode="host-context")


def test_request_and_policy_crlf_bytes_survive_actual_stdin_submission(tmp_path):
    host = module("run_host_context")
    folder = source(tmp_path)
    request = "合成请求\r\nSecond line.\r\n".encode()
    policy = "# Skill\r\n完整正文。\r\n".encode()
    (folder / "prompt.txt").write_bytes(request)
    (folder / "workspace/SKILL.md").write_bytes(policy)
    prepared = tmp_path / "run"
    host.prepare(folder, prepared, mode="host-context")
    assert (prepared / "prompt.txt").read_bytes() == request
    assert policy in (prepared / "payload.txt").read_bytes()
    calls = []

    def runner(command, **kwargs):
        calls.append(kwargs["input"])
        return subprocess.CompletedProcess(command, 1, b"", b"synthetic failure")

    host.execute(prepared, cli="synthetic-test-cli", runner=runner)
    assert calls == [(prepared / "payload.txt").read_bytes()]


def test_wrong_cli_digest_is_rejected_without_executing_its_version_command(tmp_path):
    host = module("run_host_context")
    prepared = tmp_path / "run"
    host.prepare(source(tmp_path), prepared, mode="host-context")
    marker = tmp_path / "must-not-exist"
    cli = tmp_path / "fake-cli"
    cli.write_text(f"#!/bin/sh\ntouch '{marker}'\necho fake-version\n")
    cli.chmod(0o700)
    with pytest.raises(ValueError, match="CLI_IDENTITY"):
        host.execute(prepared, cli=cli, expected_cli_sha256="0" * 64)
    assert not marker.exists()
