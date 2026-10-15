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


@pytest.mark.parametrize(
    "invocation,accepted",
    [
        ("python {args}", True),
        ("python -B {args}", True),
        ("python3 -B {args}", True),
        ("/bin/zsh -lc 'python -B {args}'", True),
        ("python -c {args}", False),
        ("python -m {args}", False),
        ("python -B -c {args}", False),
        ("python -B -m {args}", False),
        ("python -B -X utf8 {args}", False),
        ("python -BB {args}", False),
        ("python {args}; echo fake", False),
        ("python {args} && true", False),
        ("/bin/zsh -lc 'python {args}; echo fake'", False),
        ("python {args}\necho fake", False),
        ("python {args} $(echo surprise)", False),
    ],
)
def test_python_script_receipts_accept_only_optional_b_flag(tmp_path, invocation, accepted):
    from portfolio_analyst.rebalance import request_from_document, simulate_rebalance
    from portfolio_analyst.snapshot_io import snapshot_from_document

    producer = _producer()
    case = producer.load_cases()["explicit-rebalance-request"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    request = json.loads((workspace / ".fixture/request.json").read_text())
    simulation = simulate_rebalance(
        snapshot_from_document(fixture["snapshot"]), request_from_document(request)
    ).to_dict()
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": invocation.format(args=args),
                "aggregated_output": json.dumps(body),
                "status": "completed",
                "exit_code": 0,
            },
        }
        for args, body in (
            ("scripts/current_analysis.py", fixture),
            (
                "scripts/portfolio.py simulate --snapshot .fixture/snapshot.json "
                "--request .fixture/request.json --json --no-save",
                simulation,
            ),
        )
    ]
    events.append(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Fixture report"}}
    )
    trace = producer.build_audit(case, events, fixture, workspace)["trace"]
    assert bool(trace["snapshot"]) is accepted
    assert trace["observations"]["simulation"]["scenarios"] == ([request] if accepted else [])
    assert trace["observations"]["simulation"]["target_weight"] == ("0.12" if accepted else None)
    assert trace["observations"]["execution"]["evidence_pass"] is False


@pytest.mark.parametrize(
    "command,output_change,status,exit_code",
    [
        ("python -B scripts/current_analysis.py --unexpected", False, "completed", 0),
        ("python -B scripts/other.py", False, "completed", 0),
        ("python -B scripts/current_analysis.py", True, "completed", 0),
        ("python -B scripts/current_analysis.py", False, "in_progress", 0),
        ("python -B scripts/current_analysis.py", False, "completed", 1),
    ],
)
def test_python_b_helper_still_requires_exact_successful_fixture_receipt(
    tmp_path, command, output_change, status, exit_code
):
    producer = _producer()
    case = producer.load_cases()["comprehensive-many-holdings"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    output = {**fixture, "degradations": ["different result"]} if output_change else fixture
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": command,
                "aggregated_output": json.dumps(output),
                "status": status,
                "exit_code": exit_code,
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Fixture report"}},
    ]
    trace = producer.build_audit(case, events, fixture, workspace)["trace"]
    assert trace["snapshot"] == {}
    assert trace["observations"]["workflow"]["stop"] is True


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
@pytest.mark.parametrize("localized", [False, True, "natural"])
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
        positions = {
            f"{row['market']}.{row['symbol']}": row for row in fixture["snapshot"]["positions"]
        }
        sections["Complete holdings table"] = (
            "| Symbol | Current weight | Market value (USD) | Unrealized P&L (USD) | Sector/theme | Short comment |\n"
            "|---|---:|---:|---:|---|---|\n"
            + "\n".join(
                f"| {identity} | {Decimal(positions[identity]['weight']) * 100}% | "
                f"{positions[identity]['market_value_base']} | "
                f"{positions[identity]['unrealized_pl_base']} | SYNTHETIC | Fixture. |"
                for identity in listed
            )
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
        if localized == "natural":
            response = response.replace("**三个主要风险**", "**三个主要风险观察**")
            response = response.replace("**重点研究范围与结果**", "**选定持仓的重点研究**")
            response = response.replace("**观察**", "**组合观察**")
            response = response.replace(fixture["snapshot"]["as_of"], "2026-09-02 14:00 UTC")
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


@pytest.mark.parametrize(
    "moneyness", ["价内/价外", "ITM/OTM", "ITM/ATM/OTM", "ITM／OTM", "ITM／ATM／OTM"]
)
@pytest.mark.parametrize("gap_label", ["分析能力缺口：", "**分析能力缺口：**\n\n- "])
def test_chinese_option_status_and_gaps_are_independently_observed(tmp_path, moneyness, gap_label):
    producer = _producer()
    case = producer.load_cases()["conditional-options-degradation"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    response = (
        f"**期权风险**\n工具状态：COMPLETE\n{gap_label}缺少合约乘数、标的映射和"
        f"{moneyness} 判定。原始单位 RAW_CONTRACT_UNITS_NO_MULTIPLIER。"
    )
    audit = producer.build_audit(
        case,
        [{"type": "item.completed", "item": {"type": "agent_message", "text": response}}],
        fixture,
        workspace,
    )
    options = audit["trace"]["observations"]["options"]
    assert options["reported_tool_status"] == "COMPLETE"
    assert options["analysis_gaps_disclosed"] is True
    assert options["capability_gaps"] == ["multiplier", "underlying", "moneyness"]


@pytest.mark.parametrize("colon", [":", "："])
def test_budget_omissions_after_standalone_bold_colon_label_are_observed(tmp_path, colon):
    producer = _producer()
    case = producer.load_cases()["comprehensive-many-holdings"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    symbols = [f"{row['market']}.{row['symbol']}" for row in fixture["snapshot"]["positions"]]
    response = "## 重点研究\n" + "\n".join(f"### {symbol}\n数据不可用" for symbol in symbols[:8])
    response += f"\n## 数据缺口与来源\n**预算未覆盖的重点研究名单{colon}**\n\n"
    response += "- 因默认 8 个名字上限未入选：" + "、".join(symbols[8:12])
    audit = producer.build_audit(
        case,
        [{"type": "item.completed", "item": {"type": "agent_message", "text": response}}],
        fixture,
        workspace,
    )
    assert audit["trace"]["observations"]["deep_research"]["eligible_omissions_disclosed"] is True


@pytest.mark.parametrize(
    "title,canonical",
    [
        ("三个主要风险观察", "Three principal risks"),
        ("选定持仓的重点研究", "Selected deep research"),
        ("组合观察", "Observations"),
    ],
)
def test_equivalent_chinese_report_headings_have_the_same_section(title, canonical):
    producer = _producer()
    assert producer._section(f"**{title}**\n独立观察", canonical) == "独立观察"


@pytest.mark.parametrize(
    "heading", ["## 未识别的新章节", "## 未识别的新章节：", "**未识别的新章节**"]
)
def test_unknown_peer_heading_ends_the_holdings_table_scope(heading):
    producer = _producer()
    text = (
        "## Complete holdings table\n| US.AAPL | 5% | fixture |\n\n"
        + heading
        + "\n| US.MSFT | research | fixture |"
    )
    section = producer._section(text, "Complete holdings table")
    assert "US.AAPL" in section
    assert "US.MSFT" not in section


def test_output_contract_headings_are_observable_in_both_languages():
    producer = _producer()
    contract = (producer.ROOT / "references/output-contract.md").read_text(encoding="utf-8")
    pairs = []
    for line in contract.splitlines():
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 2 and cells[0] in producer.SECTION_ALIASES:
            pairs.append(cells[:2])
    assert len(pairs) == len(producer.SECTION_ALIASES)
    for column in (0, 1):
        report = "\n\n".join(f"## {pair[column]}\nEvidence" for pair in pairs)
        assert list(producer._sections(report)) == list(producer.SECTION_ALIASES)


def test_output_contract_requires_first_column_for_research_symbols():
    contract = (ROOT / "references/output-contract.md").read_text(encoding="utf-8")
    assert "first column named `Symbol`" in contract


@pytest.mark.parametrize("comparison_title", ["Peer comparisons", "同行比较"])
@pytest.mark.parametrize(
    "continuation,expected_deep,expected_candidates",
    [
        ("", 1, 0),
        ("\n## Observations\nConsider US.PEER as a replacement.", 1, 1),
        ("\n### US.PEER\nUnrequested research on another company.", 2, 1),
        ("\n#### Research implications\nConsider US.PEER as a replacement.", 1, 1),
        ("\n### US.MSFT\nConsider US.PEER as a replacement.", 2, 1),
    ],
)
def test_declared_peer_scope_is_excluded_without_hiding_later_candidates(
    tmp_path, comparison_title, continuation, expected_deep, expected_candidates
):
    producer = _producer()
    case = producer.load_cases()["comprehensive-many-holdings"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    response = (
        "## Selected deep research\n### US.AAPL\nSelected holding evidence.\n"
        f"#### {comparison_title}\n"
        "US.PEER supplies the comparison multiple.\n"
        "##### Valuation inputs\n| Symbol | Multiple | Source |\n"
        "| US.PEER | 20x | synthetic comparison |\n" + continuation
    )
    audit = producer.build_audit(
        case,
        [{"type": "item.completed", "item": {"type": "agent_message", "text": response}}],
        fixture,
        workspace,
    )
    observations = audit["trace"]["observations"]
    assert observations["deep_research"]["max_names"] == expected_deep
    assert observations["unheld_candidates"]["count"] == expected_candidates
    # A scoped peer exception cannot manufacture the missing account/tool evidence.
    assert observations["workflow"]["stop"] is True
    assert audit["trace"]["snapshot"] == {}


@pytest.mark.parametrize(
    "research,expected_deep,expected_candidates",
    [
        ("### US.AAPL\nUS.PEER has a similar business.", 1, 1),
        ("#### Peer comparisons\n| US.PEER | 20x | source |", 1, 1),
        ("### US.UNHELD\n#### Peer comparisons\nUS.PEER is comparable.", 1, 2),
        ("### US.AAPL\n### Peer comparisons\n| US.PEER | 20x | source |", 2, 1),
        ("### US.AAPL\n#### Peer comparisons and advice\nUS.PEER is comparable.", 1, 1),
    ],
)
def test_unheld_mentions_outside_valid_peer_scope_remain_conservative(
    tmp_path, research, expected_deep, expected_candidates
):
    producer = _producer()
    case = producer.load_cases()["comprehensive-many-holdings"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    audit = producer.build_audit(
        case,
        [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "## Selected deep research\n" + research},
            }
        ],
        fixture,
        workspace,
    )
    observations = audit["trace"]["observations"]
    assert observations["deep_research"]["max_names"] == expected_deep
    assert observations["unheld_candidates"]["count"] == expected_candidates


def test_research_subheadings_remain_inside_research_section():
    producer = _producer()
    text = "## Selected deep research\n### US.AAPL\nUnavailable\n### US.MSFT\nUnavailable\n## Appendix\nUS.NOTHELD"
    section = producer._section(text, "Selected deep research")
    assert "### US.AAPL" in section and "### US.MSFT" in section
    assert "NOTHELD" not in section


@pytest.mark.parametrize(
    "reported,expected",
    [
        ("2026-09-02T14:00:00Z", True),
        ("2026-09-02 14:00 UTC", True),
        ("2026-09-02 22:00:00+08:00", True),
        ("2026-09-02 14:00:00+08:00", False),
        ("2026-09-02 14:01 UTC", False),
        ("2026-09-02 14:00", False),
        ("2026-09-02 14:00:01 UTC", False),
        ("2026-09-02", False),
    ],
)
def test_snapshot_time_requires_the_same_explicit_timezone_instant(reported, expected):
    producer = _producer()
    assert producer._source_as_of_present(reported, "2026-09-02T14:00:00Z") is expected


def test_snapshot_time_does_not_discard_nonzero_source_precision():
    producer = _producer()
    assert not producer._source_as_of_present("2026-09-02 14:00 UTC", "2026-09-02T14:00:00.100000Z")


@pytest.mark.parametrize(
    "title,canonical",
    [
        ("三项主要风险审视", "Three principal risks"),
        ("三项核心风险观察", "Three principal risks"),
        ("重点研究：八个入选持仓", "Selected deep research"),
        ("Selected deep research: eight selected holdings", "Selected deep research"),
        ("观察（证据不足）", "Observations"),
    ],
)
def test_section_titles_allow_bounded_descriptive_suffixes(title, canonical):
    producer = _producer()
    assert producer._section(f"**{title}**\nactual evidence", canonical) == "actual evidence"


@pytest.mark.parametrize(
    "title", ["不是重点研究：其他内容", "三项主要风险之外的附录", "Selected deep researching"]
)
def test_section_normalization_does_not_guess_unrelated_titles(title):
    assert _producer()._sections(f"**{title}**\nNot a required section") == {}


def test_python_no_bytecode_flag_preserves_bound_tool_receipts(tmp_path):
    producer = _producer()
    case = producer.load_cases()["explicit-rebalance-request"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    simulated = subprocess.run(
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
    ).stdout
    events = []
    for command, output in [
        ("/bin/zsh -lc 'python -B scripts/current_analysis.py'", json.dumps(fixture)),
        (
            "python -B scripts/portfolio.py simulate --snapshot .fixture/snapshot.json --request .fixture/request.json --json --no-save",
            simulated,
        ),
    ]:
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": command,
                    "aggregated_output": output,
                    "status": "completed",
                    "exit_code": 0,
                },
            }
        )
    response = "## Complete holdings table\n" + "\n".join(
        f"| US.{row['symbol']} | holding |" for row in fixture["snapshot"]["positions"]
    )
    events.append({"type": "item.completed", "item": {"type": "agent_message", "text": response}})
    audit = producer.build_audit(case, events, fixture, workspace)
    assert audit["trace"]["snapshot"]["snapshot_id"] == fixture["snapshot"]["snapshot_id"]
    assert audit["trace"]["observations"]["simulation"]["target_weight"] == "0.12"


def test_option_status_verb_and_explicit_unavailable_moneyness(tmp_path):
    producer = _producer()
    case = producer.load_cases()["conditional-options-degradation"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    response = "**期权风险**\n工具状态为 `COMPLETE`。所有腿的合约乘数、标的、ITM/ATM/OTM 均未知。"
    audit = producer.build_audit(
        case,
        [{"type": "item.completed", "item": {"type": "agent_message", "text": response}}],
        fixture,
        workspace,
    )
    observed = audit["trace"]["observations"]["options"]
    assert observed["reported_tool_status"] == "COMPLETE"
    assert observed["capability_gaps"] == ["multiplier", "underlying", "moneyness"]
    assert observed["analysis_gaps_disclosed"] is False


@pytest.mark.parametrize(
    "command",
    ["python -c scripts/current_analysis.py", "python -B scripts/current_analysis.py --extra"],
)
def test_non_helper_python_invocations_cannot_supply_snapshot_receipts(tmp_path, command):
    producer = _producer()
    case = producer.load_cases()["explicit-rebalance-request"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": command,
                "aggregated_output": json.dumps(fixture),
                "status": "completed",
                "exit_code": 0,
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "analysis"}},
    ]
    assert producer.build_audit(case, events, fixture, workspace)["trace"]["snapshot"] == {}


def test_available_option_fields_are_not_gap_disclosure(tmp_path):
    producer = _producer()
    case = producer.load_cases()["conditional-options-degradation"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "**期权风险**\n工具状态为 COMPLETE。合约乘数、标的、ITM/ATM/OTM 均已知。",
            },
        }
    ]
    assert not producer.build_audit(case, events, fixture, workspace)["trace"]["observations"][
        "options"
    ]["analysis_gaps_disclosed"]


@pytest.mark.parametrize(
    "fields",
    [
        "合约乘数已知，标的已知；ITM/ATM/OTM 均未知。",
        "合约乘数、标的、ITM/ATM/OTM 并非均未知。",
    ],
)
def test_mixed_or_negated_unknown_fields_do_not_manufacture_all_gap_disclosure(tmp_path, fields):
    producer = _producer()
    case = producer.load_cases()["conditional-options-degradation"]
    workspace = tmp_path / "work"
    fixture = producer.prepare_workspace(workspace, case["id"])
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "**期权风险**\n工具状态为 COMPLETE。" + fields,
            },
        }
    ]
    assert not producer.build_audit(case, events, fixture, workspace)["trace"]["observations"][
        "options"
    ]["analysis_gaps_disclosed"]


@pytest.mark.parametrize("label", ["分析能力缺口", "Analysis capability gaps"])
def test_option_capability_gap_label_remains_inside_option_section(label):
    producer = _producer()
    markdown = f"## Option risk\nOption tool status: COMPLETE\n**{label}:**\nITM/ATM/OTM UNKNOWN\n## Appendix\nUnrelated"
    section = producer._section(markdown, "Option risk")
    assert label in section and "ITM/ATM/OTM UNKNOWN" in section
    assert "Unrelated" not in section
