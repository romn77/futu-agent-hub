"""Opt-in synthetic host-context prototype. Never launches the live account helper."""

import argparse
from datetime import datetime, timezone
import importlib.util
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "evals" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


POLICY = _load("policy_context")
PRODUCER = _load("run_codex_evals")


def _write(path, value):
    PRODUCER._write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


@lru_cache(maxsize=1)
def _trusted_workspace_paths():
    # This evaluation prototype runs from a Git checkout; untracked notes are not inputs.
    tracked = (
        subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True)
        .stdout.decode()
        .split("\0")
    )
    allowed = {
        name
        for name in tracked
        if name == "SKILL.md" or name.startswith(("references/", "portfolio_analyst/", "schemas/"))
    }
    allowed.update(
        {
            "scripts/current_analysis.py",
            "scripts/portfolio.py",
            ".fixture/envelope.json",
            ".fixture/snapshot.json",
            ".fixture/request.json",
            "research/data.json",
            "research/public_reader.py",
        }
    )
    prefix = "evals/research-fixtures/skills/"
    allowed.update(
        "research/skills/" + name.removeprefix(prefix)
        for name in tracked
        if name.startswith(prefix)
    )
    return allowed


def _manifest(workspace):
    allowed = {
        "SKILL.md",
        ".fixture",
        "portfolio_analyst",
        "references",
        "schemas",
        "scripts",
        "research",
    }
    if any(path.name not in allowed for path in workspace.iterdir()):
        raise ValueError("UNEXPECTED_WORKSPACE_ENTRY")
    if any(path.is_symlink() for path in workspace.rglob("*")):
        raise ValueError("SYMLINKED_WORKSPACE")
    names = {str(path.relative_to(workspace)) for path in workspace.rglob("*") if path.is_file()}
    if not names <= _trusted_workspace_paths():
        raise ValueError("UNTRUSTED_WORKSPACE_FILE")
    return {
        str(path.relative_to(workspace)): POLICY.digest(path.read_bytes())
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
    }


def _synthetic_fixture(workspace):
    fixture = json.loads((workspace / ".fixture/envelope.json").read_text())
    if not fixture["snapshot"]["source_version"].startswith("SYNTHETIC"):
        raise ValueError("SYNTHETIC_INPUT_REQUIRED")
    script = (ROOT / "evals/fixture_cli.py").read_bytes()
    for name in ("current_analysis.py", "portfolio.py"):
        if (workspace / "scripts" / name).read_bytes() != script:
            raise ValueError("FIXTURE_HELPER_REQUIRED")
    return fixture


def prepare(source, output, *, mode, additional=()):
    source, output = Path(source), Path(output)
    if mode not in {"filesystem", "host-context"}:
        raise ValueError("UNKNOWN_DELIVERY_MODE")
    _manifest(source / "workspace")
    _synthetic_fixture(source / "workspace")
    request = (source / "prompt.txt").read_bytes().decode("utf-8")
    output.mkdir(parents=False, mode=0o700)
    workspace = output / "workspace"
    shutil.copytree(source / "workspace", workspace)
    bundle = (
        POLICY.build_bundle(workspace, additional=additional) if mode == "host-context" else None
    )
    payload = POLICY.render_submission(request, bundle) if bundle else request
    PRODUCER._write(output / "prompt.txt", request.encode())
    PRODUCER._write(output / "payload.txt", payload.encode())
    if bundle:
        _write(output / "policy-bundle.json", bundle)
    record = {
        "synthetic": True,
        "mode": mode,
        "files": _manifest(workspace),
        "request_sha256": POLICY.digest(request.encode()),
        "payload_sha256": POLICY.digest(payload.encode()),
    }
    _write(output / "inputs.json", record)
    return record


def execute(output, *, cli, expected_cli_sha256=None, runner=None, timeout=600):
    from portfolio_analyst.option_metadata import _bounded_subprocess_run

    output = Path(output)
    if not 1 <= timeout <= 600:
        raise ValueError("EXECUTION_BUDGET_INVALID")
    if any((output / name).exists() for name in ("request.json", "events.jsonl", "execution.json")):
        raise FileExistsError("attempt already started; preserve it")
    workspace = output / "workspace"
    record = json.loads((output / "inputs.json").read_text())
    if record.get("synthetic") is not True or _manifest(workspace) != record["files"]:
        raise ValueError("INPUT_MANIFEST_MISMATCH")
    fixture = _synthetic_fixture(workspace)
    request = (output / "prompt.txt").read_bytes().decode("utf-8")
    payload = (output / "payload.txt").read_bytes().decode("utf-8")
    if (
        POLICY.digest(request.encode()) != record["request_sha256"]
        or POLICY.digest(payload.encode()) != record["payload_sha256"]
    ):
        raise ValueError("INPUT_PAYLOAD_CHANGED")
    bundle, delivery = None, None
    if record["mode"] == "host-context":
        bundle = json.loads((output / "policy-bundle.json").read_text())
        delivery = POLICY.verify_submission(workspace, request, bundle, payload)
    elif record["mode"] != "filesystem" or payload != request:
        raise ValueError("DELIVERY_MODE_MISMATCH")
    identity = {"cli_version": None, "cli_sha256": None}
    if runner is None:
        cli = str(Path(cli).resolve(strict=True))
        cli_digest = POLICY.digest(Path(cli).read_bytes())
        if not expected_cli_sha256 or cli_digest != expected_cli_sha256:
            raise ValueError("CLI_IDENTITY_MISMATCH")
        identity = {
            "cli_version": subprocess.run(
                [cli, "--version"], check=True, capture_output=True, text=True, timeout=10
            ).stdout.strip(),
            "cli_sha256": cli_digest,
        }
    command = [
        str(cli),
        "-a",
        "never",
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--model",
        "gpt-6-astra",
        "-c",
        'model_reasoning_effort="medium"',
        "--json",
        "--color",
        "never",
        "-",
    ]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "CODEX_HOME",
            "CODEX_API_KEY",
            "OPENAI_API_KEY",
            "TMPDIR",
            "LANG",
            "SYSTEMROOT",
        }
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    _write(
        output / "request.json",
        {
            "argv": command,
            **identity,
            "server_model_revision": None,
            "stdin_sha256": POLICY.digest(payload.encode()),
            "delivery": delivery,
        },
    )
    result = {
        "status": "EXECUTION_UNAVAILABLE",
        "mode": record["mode"],
        "execution_evidence_pass": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        completed = (runner or _bounded_subprocess_run)(
            command,
            input=payload.encode(),
            cwd=workspace,
            env=environment,
            timeout=timeout,
            max_output_bytes=PRODUCER.MAX_EVENTS_BYTES,
            shell=False,
        )
        PRODUCER._write(output / "events.jsonl", completed.stdout)
        PRODUCER._write(output / "stderr.txt", completed.stderr)
        if completed.returncode != 0:
            raise ValueError("CODEX_EXEC_FAILED")
        events = PRODUCER.parse_events(completed.stdout)
        if _manifest(workspace) != record["files"]:
            raise ValueError("WORKSPACE_MUTATED")
        case = {"id": "host-context-feasibility", "fixture": "synthetic", "prompt": request}
        audit = PRODUCER.build_audit(case, events, fixture, workspace)
        _write(output / "audit.json", audit)
        PRODUCER._write(output / "response.md", audit["response_markdown"].encode())
        observed = audit["trace"]["observations"]
        checks = dict(observed["execution"]["checks"])
        if delivery:
            # Retain the original audit unchanged: no fictional command or file read.
            del checks["required_reference_reads_complete"]
            checks["required_policies_in_verified_cli_input"] = (
                delivery["status"] == "VERIFIED_CLI_INPUT"
            )
        items = [event.get("item", {}) for event in events if event.get("type") == "item.completed"]
        argv = [
            PRODUCER._argv(item.get("command", ""))
            for item in items
            if item.get("type") == "command_execution"
        ]
        checks["one_helper_attempt"] = (
            sum(
                len(args) > 1
                and args[1] in {"scripts/current_analysis.py", "./scripts/current_analysis.py"}
                for args in argv
            )
            == 1
        )
        checks["no_nonfixture_tools"] = not any(
            item.get("type") in {"web_search", "mcp_tool_call", "collab_tool_call", "file_change"}
            for item in items
        )
        checks["recognized_command_forms"] = all(
            POLICY.recognized_command(item.get("command", ""))
            for item in items
            if item.get("type") == "command_execution"
        )
        access = POLICY.observe_policy_access(workspace, events, bundle)
        result.update(
            status="REPORT_GENERATED",
            execution_evidence_pass=all(checks.values()),
            checks=checks,
            delivery=delivery,
            policy_access=access,
            field_integrity=observed["holdings"]["field_integrity"],
            section_integrity=observed["report"]["section_integrity"],
            token_usage=observed["token_usage"],
            original_file_read_checks=observed["execution"]["checks"],
            source_audit_sha256=POLICY.digest((output / "audit.json").read_bytes()),
        )
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result["error_type"] = type(exc).__name__
        result["error_code"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        for name, data in (
            ("events.jsonl", getattr(exc, "output", b"")),
            ("stderr.txt", getattr(exc, "stderr", b"")),
        ):
            if not (output / name).exists():
                PRODUCER._write(output / name, data if isinstance(data, bytes) else b"")
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write(output / "execution.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--source", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--mode", choices=("filesystem", "host-context"), required=True)
    prep.add_argument("--policy", action="append", default=[], choices=POLICY.OPTIONAL_DOCUMENTS)
    run = sub.add_parser("execute")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--cli", required=True)
    run.add_argument("--cli-sha256", required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare(args.source, args.output, mode=args.mode, additional=args.policy)
        print(
            json.dumps({"mode": result["mode"], "synthetic": True, "files": len(result["files"])})
        )
    else:
        result = execute(args.output, cli=args.cli, expected_cli_sha256=args.cli_sha256)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["execution_evidence_pass"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
