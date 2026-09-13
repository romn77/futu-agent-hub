import importlib.util
from decimal import Decimal
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _producer():
    spec = importlib.util.spec_from_file_location(
        "codex_eval_producer", ROOT / "evals/run_codex_evals.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_workspace_is_synthetic_and_financially_valid(tmp_path):
    producer = _producer()
    from portfolio_analyst.snapshot_io import snapshot_from_document
    from portfolio_analyst.schema import validate_option_risk_document, validate_technical_document

    for case_id in producer.load_cases():
        workspace = tmp_path / case_id
        envelope = producer.prepare_workspace(workspace, case_id)
        source = snapshot_from_document(envelope["snapshot"])
        assert source.nav_source == "OPEND_TOTAL_ASSETS"
        validate_technical_document(envelope["technical"])
        if envelope["options"] is not None:
            validate_option_risk_document(envelope["options"])
        assert not (workspace / "evals").exists()
        assert not (workspace / "futuapi").exists()
        if case_id == "comprehensive-many-holdings":
            assert len(source.positions) == 37
            assert sum(row.weight >= Decimal("0.05") for row in source.positions) == 12
        result = subprocess.run(
            ["python", "scripts/current_analysis.py"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout) == envelope
        if case_id == "explicit-rebalance-request":
            result = subprocess.run(
                [
                    "python",
                    "scripts/portfolio.py",
                    "simulate",
                    "--snapshot",
                    ".fixture/snapshot.json",
                    "--request",
                    ".fixture/request.json",
                    "--json",
                    "--no-save",
                ],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=True,
            )
            from portfolio_analyst.schema import validate_rebalance_result_document

            validate_rebalance_result_document(json.loads(result.stdout))


def test_model_self_report_cannot_manufacture_tool_evidence(tmp_path):
    producer = _producer()
    case = producer.load_cases()["comprehensive-many-holdings"]
    fixture = producer.prepare_workspace(tmp_path / "work", case["id"])
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "answer",
                "type": "agent_message",
                "text": 'I ran every tool. {"trace":{"holdings":37}}',
            },
        },
        {"type": "turn.completed", "usage": {}},
    ]
    audit = producer.build_audit(case, events, fixture, tmp_path / "work")
    assert audit["trace"]["snapshot"] == {}
    assert audit["trace"]["observations"]["holdings"]["symbols"] == []
    assert audit["trace"]["observations"]["workflow"]["stop"] is True


def test_stdout_without_completed_turn_is_not_a_model_pass(tmp_path):
    producer = _producer()
    with pytest.raises(ValueError, match="MODEL_EXECUTION_INCOMPLETE"):
        producer.parse_events(b'{"type":"turn.started"}\n')


def test_codex_execution_is_bounded_read_only_and_failure_is_not_pass(tmp_path):
    producer = _producer()
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--ephemeral" in command and "--ignore-user-config" in command
        assert "--dangerously-bypass-approvals-and-sandbox" not in command
        assert kwargs["timeout"] <= 600
        return subprocess.CompletedProcess(command, 1, b"", b"Read-only file system")

    result = producer.run_case("comprehensive-many-holdings", tmp_path / "run", runner=runner)
    assert commands
    assert result["status"] == "EXECUTION_UNAVAILABLE"
    assert result["behavior_pass"] is False
    assert (tmp_path / "run/events.jsonl").is_file()
    assert not (tmp_path / "run/audit.json").exists()


def test_existing_output_directory_is_never_overwritten(tmp_path):
    producer = _producer()
    marker = tmp_path / "keep"
    marker.write_text("user data")
    with pytest.raises(FileExistsError):
        producer.run_case("comprehensive-many-holdings", tmp_path)
    assert marker.read_text() == "user data"


@pytest.mark.parametrize("duplicate_holding", [False, True])
@pytest.mark.parametrize("localized", [False, True])
def test_complete_host_trace_is_required_in_addition_to_markdown_assertions(
    tmp_path, duplicate_holding, localized
):
    producer = _producer()

    def runner(command, **kwargs):
        workspace = kwargs["cwd"]
        fixture = json.loads((workspace / ".fixture/envelope.json").read_text())
        identities = [
            f"{row['market']}.{row['symbol']}" for row in fixture["snapshot"]["positions"]
        ]
        listed = identities if not duplicate_holding else identities[:-1] + identities[:1]
        headings = [
            "Scope and data time",
            "One-line conclusion",
            "Overview",
            "Three principal risks",
            "Complete holdings table",
            "Selected deep research",
            "Macro impact",
            "Observations",
            "Data gaps and sources",
        ]
        sections = {heading: "fixture analysis" for heading in headings}
        sections["Scope and data time"] = (
            fixture["snapshot"]["snapshot_id"] + " " + fixture["snapshot"]["as_of"]
        )
        sections["Complete holdings table"] = "\n".join(
            f"| {identity} | Unknown | fixture |" for identity in listed
        )
        sections["Selected deep research"] = "\n".join(
            f"### {identity}\nUnavailable in offline fixture" for identity in identities[:8]
        )
        sections["Data gaps and sources"] = "omitted because of cap: " + ", ".join(identities[8:12])
        response = "\n\n".join(f"## {heading}\n{sections[heading]}" for heading in headings)
        if localized:
            for heading in headings:
                response = response.replace(
                    "## " + heading + "\n", "**" + producer.SECTION_ALIASES[heading][0] + "**\n"
                )
        events = []
        for index, path in enumerate(
            [workspace / "SKILL.md", *(workspace / "references").rglob("*.md")]
        ):
            if "options" in str(path):
                continue
            events.append(
                {
                    "type": "item.completed",
                    "item": {
                        "id": str(index),
                        "type": "command_execution",
                        "command": "cat " + str(path.relative_to(workspace)),
                        "aggregated_output": path.read_text(),
                        "status": "completed",
                        "exit_code": 0,
                    },
                }
            )
        events.extend(
            [
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
                    "item": {"id": "answer", "type": "agent_message", "text": response},
                },
                {"type": "turn.completed", "usage": {}},
            ]
        )
        return subprocess.CompletedProcess(
            command, 0, b"\n".join(json.dumps(event).encode() for event in events), b""
        )

    result = producer.run_case("comprehensive-many-holdings", tmp_path / "run", runner=runner)
    assert result["behavior_pass"] is (not duplicate_holding)


@pytest.mark.parametrize(
    "exception", [subprocess.TimeoutExpired("codex", 5), RuntimeError("platform unsupported")]
)
def test_incomplete_execution_retains_failure_evidence_without_traceback(tmp_path, exception):
    producer = _producer()

    def runner(*args, **kwargs):
        raise exception

    result = producer.run_case("comprehensive-many-holdings", tmp_path / "run", runner=runner)
    assert result["status"] == "EXECUTION_UNAVAILABLE"
    assert (tmp_path / "run/events.jsonl").is_file()
    assert (tmp_path / "run/stderr.txt").is_file()


def test_chinese_headings_and_research_table_are_observed_without_model_self_report(tmp_path):
    producer = _producer()
    case = producer.load_cases()["comprehensive-many-holdings"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    symbols = [row["symbol"] for row in fixture["snapshot"]["positions"]]
    response = "**完整持仓**\n" + "\n".join(f"| {symbol} | 未知 | 未知 |" for symbol in symbols)
    response += "\n\n**重点研究范围与结果**\n" + "\n".join(
        f"| {symbol} | 数据不可用 |" for symbol in symbols[:8]
    )
    response += "\n\n**数据缺口与来源**\n因八只上限遗漏：" + "、".join(symbols[8:12])
    audit = producer.build_audit(
        case,
        [
            {
                "type": "item.completed",
                "item": {"id": "reply", "type": "agent_message", "text": response},
            }
        ],
        fixture,
        workspace,
    )
    observations = audit["trace"]["observations"]
    assert len(observations["holdings"]["symbols"]) == 37
    assert observations["deep_research"]["max_names"] == 8
    assert observations["deep_research"]["eligible_omissions_disclosed"] is True


def test_chinese_option_status_and_gaps_are_independently_observed(tmp_path):
    producer = _producer()
    case = producer.load_cases()["conditional-options-degradation"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    response = "**期权风险**\n工具状态：COMPLETE\n分析能力缺口：缺少合约乘数、标的映射和价内/价外判定。原始单位 RAW_CONTRACT_UNITS_NO_MULTIPLIER。"
    audit = producer.build_audit(
        case,
        [{"type": "item.completed", "item": {"type": "agent_message", "text": response}}],
        fixture,
        workspace,
    )
    options = audit["trace"]["observations"]["options"]
    assert options["reported_tool_status"] == "COMPLETE"
    assert options["capability_gaps"] == ["multiplier", "underlying", "moneyness"]
