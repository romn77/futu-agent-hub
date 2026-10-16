"""A numerically correct report still needs the required navigable sections."""

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TITLES = [
    "范围与数据时间",
    "一句话结论",
    "组合概览",
    "三个主要风险",
    "完整持仓表",
    "重点研究",
    "宏观影响",
    "观察",
    "数据缺口与来源",
]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "evals" / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def report(titles=TITLES):
    return "\n\n".join(f"## {title}\n内容。" for title in titles)


@pytest.mark.parametrize(
    "change", ["bold", "colon", "missing", "reordered", "duplicate", "renamed"]
)
def test_real_audit_rejects_invalid_sections_even_with_correct_account_fields(tmp_path, change):
    producer = module("run_codex_evals")
    case = producer.load_cases()["explicit-rebalance-request"]
    workspace = tmp_path / "workspace"
    fixture = producer.prepare_workspace(workspace, case["id"])
    source = fixture["snapshot"]
    text = report()
    table = "| Symbol | Weight | Market value | Unrealized P&L |\n|---|---:|---:|---:|\n"
    for row in source["positions"]:
        table += f"| US.{row['symbol']} | {float(row['weight']) * 100}% | {row['market_value_base']} | {row['unrealized_pl_base']} |\n"
    text = text.replace("## 完整持仓表\n内容。", "## 完整持仓表\n" + table)
    text = text.replace(
        "## 范围与数据时间\n内容。", f"## 范围与数据时间\n{source['snapshot_id']} {source['as_of']}"
    )
    text = {
        "bold": text.replace("## 宏观影响", "**宏观影响**"),
        "colon": text.replace("## 宏观影响", "**宏观影响：**"),
        "missing": text.replace("## 宏观影响\n内容。", ""),
        "reordered": text.replace("## 宏观影响", "## 临时")
        .replace("## 观察", "## 宏观影响")
        .replace("## 临时", "## 观察"),
        "duplicate": text + "\n## 观察\n重复章节。",
        "renamed": text.replace("## 一句话结论", "## 诊断结论"),
    }[change]
    events = []
    for path in [workspace / "SKILL.md", *(workspace / "references").rglob("*.md")]:
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": str(path),
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
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        ]
    )
    observed = producer.build_audit(case, events, fixture, workspace)["trace"]["observations"]
    assert observed["holdings"]["field_integrity"]["status"] == "VERIFIED"
    assert observed["execution"]["evidence_pass"] is False


def test_canonical_sections_allow_paragraph_labels_and_research_subsections():
    text = report().replace(
        "## 重点研究\n内容。", "## 重点研究\n### US.AAPL\n**现金流：**\n有依据的分析。"
    )
    assert module("report_checks").check_sections(text, has_options=False)["status"] == "VERIFIED"


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_fenced_heading_examples_cannot_supply_a_missing_section(fence):
    text = report().replace("## 宏观影响", f"{fence}markdown\n## 宏观影响\n{fence}")
    assert module("report_checks").check_sections(text, has_options=False)["status"] != "VERIFIED"


def test_options_heading_is_required_only_for_held_options():
    checks = module("report_checks")
    text = report()
    assert checks.check_sections(text, has_options=True)["status"] != "VERIFIED"
    text = text.replace("## 宏观影响", "## 期权风险\n状态与缺口。\n\n## 宏观影响")
    assert checks.check_sections(text, has_options=True)["status"] == "VERIFIED"
    assert checks.check_sections(text, has_options=False)["status"] != "VERIFIED"


def test_english_titles_are_equally_valid():
    titles = [
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
    assert (
        module("report_checks").check_sections(report(titles), has_options=False)["status"]
        == "VERIFIED"
    )
