import json
import subprocess
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNNER = SKILL_ROOT / "evals" / "run_evals.py"


def _write_documents(tmp_path: Path, *, trd_env: str = "REAL", case_id: str = "case-1"):
    evals = {
        "skill_name": "portfolio-analyst",
        "schema_version": "1.0",
        "evals": [
            {
                "id": "case-1",
                "prompt": "Analyze the current account.",
                "expected_behavior": ["Use a real snapshot."],
                "assertions": [
                    {"type": "equals", "target": "trace.snapshot.trd_env", "value": "REAL"},
                    {
                        "type": "contains",
                        "target": "trace.loaded_references",
                        "value": "references/workflow.md",
                    },
                    {
                        "type": "not_contains",
                        "target": "trace.executed_commands",
                        "value": "--save",
                    },
                    {
                        "type": "count_equals",
                        "target": "trace.observations.holdings.symbols",
                        "value": 2,
                    },
                ],
            }
        ],
    }
    audit = {
        "schema_version": "1.0",
        "case_id": case_id,
        "response_markdown": "# Portfolio review",
        "trace": {
            "snapshot": {"trd_env": trd_env, "base_currency": "USD"},
            "loaded_references": ["references/workflow.md"],
            "executed_commands": ["portfolio.py risk --snapshot run/snapshot.json --no-save"],
            "observations": {"holdings": {"symbols": ["US.AAPL", "US.MSFT"]}},
        },
    }
    evals_path = tmp_path / "evals.json"
    audit_path = tmp_path / "audit.json"
    evals_path.write_text(json.dumps(evals), encoding="utf-8")
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    return evals_path, audit_path


def _run(evals_path: Path, audit_path: Path):
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--evals",
            str(evals_path),
            "--audit",
            str(audit_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_eval_runner_returns_evidence_for_a_passing_case(tmp_path):
    evals_path, audit_path = _write_documents(tmp_path)

    completed = _run(evals_path, audit_path)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["passed"] is True
    assert result["summary"] == {
        "total_cases": 1,
        "passed_cases": 1,
        "failed_cases": 0,
        "total_assertions": 4,
        "passed_assertions": 4,
        "failed_assertions": 0,
    }
    assert all(item["passed"] for item in result["cases"][0]["assertions"])


def test_eval_runner_returns_nonzero_and_actual_evidence_for_a_failure(tmp_path):
    evals_path, audit_path = _write_documents(tmp_path, trd_env="SIMULATE")

    completed = _run(evals_path, audit_path)

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["passed"] is False
    failed = [item for item in result["cases"][0]["assertions"] if not item["passed"]]
    assert failed == [
        {
            "index": 0,
            "type": "equals",
            "target": "trace.snapshot.trd_env",
            "expected": "REAL",
            "actual": "SIMULATE",
            "passed": False,
        }
    ]


def test_eval_runner_rejects_an_unknown_case_id(tmp_path):
    evals_path, audit_path = _write_documents(tmp_path, case_id="unknown")

    completed = _run(evals_path, audit_path)

    assert completed.returncode == 2
    result = json.loads(completed.stdout)
    assert result["passed"] is False
    assert "unknown case_id" in result["error"]


def test_eval_runner_fails_cleanly_for_a_nonmatching_object_key(tmp_path):
    evals_path, audit_path = _write_documents(tmp_path)
    document = json.loads(evals_path.read_text(encoding="utf-8"))
    document["evals"][0]["assertions"] = [
        {"type": "contains", "target": "trace.snapshot", "value": []}
    ]
    evals_path.write_text(json.dumps(document), encoding="utf-8")

    completed = _run(evals_path, audit_path)

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["cases"][0]["assertions"][0]["passed"] is False


def test_eval_runner_rejects_empty_response_markdown(tmp_path):
    evals_path, audit_path = _write_documents(tmp_path)
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["response_markdown"] = "  \n"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    completed = _run(evals_path, audit_path)

    assert completed.returncode == 2
    result = json.loads(completed.stdout)
    assert result["passed"] is False
    assert "response_markdown must be a non-empty string" in result["error"]
