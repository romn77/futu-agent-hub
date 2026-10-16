"""Regressions for observed report omissions; no model self-reported verdicts."""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def checks():
    spec = importlib.util.spec_from_file_location(
        "report_integrity_checks", ROOT / "evals/report_checks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(pnl="-123.456"):
    return {
        "base_currency": "USD",
        "positions": [
            {
                "market": "US",
                "symbol": "AAPL",
                "quantity": "10",
                "weight": "0.125",
                "market_value_base": "4000.125",
                "unrealized_pl_base": pnl,
            },
        ],
    }


TABLE = """| Symbol | Current weight | Market value (USD) | Unrealized P&L (USD) | Sector/theme | Short comment |
|---|---:|---:|---:|---|---|
| US.AAPL | 12.50% | 4,000.13 | -123.46 | Technology | Current research hypothesis. |
"""


def test_complete_rounded_fields_match_bound_snapshot():
    result = checks().check_holdings(TABLE, snapshot())
    assert result["status"] == "VERIFIED"
    assert result["issues"] == []
    assert result["checked_fields"] == 3


@pytest.mark.parametrize(
    "table,code",
    [
        (TABLE.replace(" | -123.46", ""), "COLUMN_COUNT"),
        (TABLE.replace("-123.46", ""), "VALUE_UNVERIFIABLE"),
        (TABLE.replace("-123.46", "+123.46"), "VALUE_MISMATCH"),
        (TABLE.replace("-123.46", "Unknown"), "VALUE_MISMATCH"),
        (TABLE.replace("4,000.13", "4,999.00"), "VALUE_MISMATCH"),
        (TABLE.replace("12.50%", "100%"), "VALUE_MISMATCH"),
        (TABLE.replace("12.50%", "0.125"), "VALUE_UNVERIFIABLE"),
        (TABLE.replace("(USD)", "(HKD)"), "CURRENCY_MISMATCH"),
        (TABLE.replace("(USD)", "（港元）"), "CURRENCY_MISMATCH"),
        (TABLE.replace("(USD)", "（人民币）"), "CURRENCY_MISMATCH"),
        (TABLE.replace("Unrealized P&L (USD)", "Unrealized P&L (%)"), "VALUE_UNVERIFIABLE"),
        (TABLE.replace("-123.46", "NaN"), "VALUE_UNVERIFIABLE"),
        (TABLE + TABLE.splitlines()[-1] + "\n", "DUPLICATE_HOLDING"),
        ("\n".join(TABLE.splitlines()[:2]), "MISSING_HOLDING"),
        ("```markdown\n" + TABLE + "```", "HEADER_UNVERIFIABLE"),
    ],
)
def test_incomplete_or_incorrect_report_cannot_pass(table, code):
    result = checks().check_holdings(table, snapshot())
    assert result["status"] != "VERIFIED"
    assert code in {item["code"] for item in result["issues"]}


def test_chinese_reordered_columns_and_explicit_scaled_units():
    table = """| 标的 | 市值（万美元） | 未实现盈亏（美元） | 仓位 | 行业/主题 | 简评 |
|---|---:|---:|---:|---|---|
| **AAPL** | 0.400013 | ($123.46) | 12.50% | 科技 | 有来源的判断。 |
"""
    assert checks().check_holdings(table, snapshot())["status"] == "VERIFIED"


@pytest.mark.parametrize("display,verified", [("未知", True), ("0", False), ("", False)])
def test_missing_source_pnl_is_unknown_not_zero(display, verified):
    result = checks().check_holdings(TABLE.replace("-123.46", display), snapshot(None))
    assert (result["status"] == "VERIFIED") is verified


def test_actual_zero_is_not_an_unknown_placeholder():
    assert (
        checks().check_holdings(TABLE.replace("-123.46", "0"), snapshot("0"))["status"]
        == "VERIFIED"
    )
    assert (
        checks().check_holdings(TABLE.replace("-123.46", "未知"), snapshot("0"))["status"]
        != "VERIFIED"
    )


def test_zero_quantity_is_excluded_and_same_security_aggregation_preserves_unknown():
    source = snapshot(None)
    source["positions"].append({**source["positions"][0], "quantity": "0", "symbol": "MSFT"})
    source["positions"].append(
        {
            **source["positions"][0],
            "market_value_base": "1000",
            "weight": "0.03125",
            "unrealized_pl_base": "200",
        }
    )
    table = TABLE.replace("4,000.13", "5,000.13").replace("12.50%", "15.625%")
    table = table.replace("-123.46", "Unknown")
    assert checks().check_holdings(table, source)["status"] == "VERIFIED"


def test_extra_unheld_table_row_cannot_pass():
    table = TABLE + "| US.MSFT | 10% | 1000 | 0 | Software | Candidate. |\n"
    result = checks().check_holdings(table, snapshot())
    assert result["status"] != "VERIFIED"
    assert "UNEXPECTED_HOLDING" in {item["code"] for item in result["issues"]}


def test_real_audit_rejects_correct_symbols_with_missing_pnl(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "integrity_producer", ROOT / "evals/run_codex_evals.py"
    )
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    case = producer.load_cases()["explicit-rebalance-request"]
    workspace = tmp_path / "workspace"
    fixture = producer.prepare_workspace(workspace, case["id"])
    source = fixture["snapshot"]
    events = []
    for path in [workspace / "SKILL.md", *(workspace / "references").rglob("*.md")]:
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "cat " + str(path.relative_to(workspace)),
                    "aggregated_output": path.read_text(),
                    "status": "completed",
                    "exit_code": 0,
                },
            }
        )
    import json

    events.append(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "python scripts/current_analysis.py",
                "aggregated_output": json.dumps(fixture),
                "status": "completed",
                "exit_code": 0,
            },
        }
    )
    response = (
        f"## Scope and data time\n{source['snapshot_id']} {source['as_of']}\n"
        "## One-line conclusion\nFixture.\n## Overview\nFixture.\n"
        "## Three principal risks\nFixture.\n## Complete holdings table\n"
    )
    response += "\n".join(TABLE.splitlines()[:2]) + "\n"
    for row in source["positions"]:
        response += f"| US.{row['symbol']} | {float(row['weight']) * 100}% | {row['market_value_base']} | Technology | Omitted P&L. |\n"
    response += (
        "## Selected deep research\nFixture.\n## Macro impact\nFixture.\n"
        "## Observations\nFixture.\n## Data gaps and sources\nFixture.\n"
    )
    events.append({"type": "item.completed", "item": {"type": "agent_message", "text": response}})
    audit = producer.build_audit(case, events, fixture, workspace)
    assert (
        audit["trace"]["observations"]["execution"]["checks"]["required_section_headings"] is True
    )
    assert audit["trace"]["observations"]["holdings"]["field_integrity"]["status"] != "VERIFIED"
    assert audit["trace"]["observations"]["execution"]["evidence_pass"] is False
