import json
import re
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
REFERENCES = SKILL_ROOT / "references"

CORE_REFERENCES = {
    "workflow.md",
    "intent-routing.md",
    "portfolio-analysis-policy.md",
    "evidence-policy.md",
    "research-policy.md",
    "options-policy.md",
    "recommendation-policy.md",
    "output-contract.md",
    "failure-and-degradation.md",
}
OPTIONAL_WORKFLOWS = {
    "rebalance-simulation.md",
    "trade-review.md",
    "performance-attribution.md",
    "position-lifecycle.md",
}
CORE_TOOL_CONTRACTS = {
    "snapshot.md",
    "analyze.md",
    "risk.md",
    "options.md",
    "research.md",
    "technical.md",
}
OPTIONAL_TOOL_CONTRACTS = {
    "simulate.md",
    "trades.md",
    "performance.md",
    "lifecycle.md",
}


def _read(relative: str) -> str:
    return (SKILL_ROOT / relative).read_text(encoding="utf-8")


def _section(document: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)", document
    )
    assert match, f"missing section: {heading}"
    return match.group(1)


def test_progressive_disclosure_tree_is_complete():
    assert CORE_REFERENCES <= {path.name for path in REFERENCES.glob("*.md")}
    assert OPTIONAL_WORKFLOWS == {
        path.name for path in (REFERENCES / "optional-workflows").glob("*.md")
    }
    assert CORE_TOOL_CONTRACTS == {
        path.name for path in (REFERENCES / "tool-contract" / "core").glob("*.md")
    }
    assert OPTIONAL_TOOL_CONTRACTS == {
        path.name for path in (REFERENCES / "tool-contract" / "optional").glob("*.md")
    }


def test_skill_is_a_lean_read_only_router_with_a_minimal_default_path():
    skill = _read("SKILL.md")
    assert len(skill.splitlines()) <= 250
    assert re.search(r"(?m)^name: portfolio-analyst$", skill)
    assert re.search(r"(?m)^allowed-tools: Bash Read$", skill)
    assert "current USD US-stock account holdings" in skill
    assert "REAL" in skill and "USD" in skill
    assert "one bound snapshot" in skill
    assert "no default persistence" in skill
    assert "never unlock" in skill.lower()
    assert "place order" in skill.lower()

    default_route = _section(skill, "Default comprehensive route")
    for required in (
        "references/workflow.md",
        "references/portfolio-analysis-policy.md",
        "references/evidence-policy.md",
        "references/research-policy.md",
        "references/output-contract.md",
        "references/failure-and-degradation.md",
        "tool-contract/core/snapshot.md",
        "tool-contract/core/analyze.md",
        "tool-contract/core/risk.md",
        "tool-contract/core/research.md",
        "tool-contract/core/technical.md",
    ):
        assert required in default_route
    assert "optional-workflows/" not in default_route
    assert "options-policy.md" not in default_route
    assert "recommendation-policy.md" not in default_route

    conditional_route = _section(skill, "Conditional routes")
    assert "options-policy.md" in conditional_route
    assert "recommendation-policy.md" in conditional_route
    for optional in OPTIONAL_WORKFLOWS:
        assert f"optional-workflows/{optional}" in conditional_route


def test_default_analysis_policy_matches_the_narrow_product_contract():
    policy = _read("references/portfolio-analysis-policy.md")
    for rule in (
        "every current filled holding",
        "Pending/open orders are excluded",
        "Cash is portfolio-level",
        "ETF is one position",
        "US regional concentration",
        "gain/loss alone cannot drive advice",
        "single holding >10%",
        ">15%",
        "Top 3 >40%",
        "Top 5 >60%",
        "sector/theme >35%",
        "0.55",
        "0.75",
        "analysis priority only",
    ):
        assert rule in policy

    output = _read("references/output-contract.md")
    for column in (
        "Symbol",
        "Current weight",
        "Market value",
        "Unrealized P&L",
        "Sector/theme",
        "Short comment",
    ):
        assert column in output
    for forbidden in ("Inferred role", "Score", "Ranking", "Attention status"):
        assert forbidden in output
    assert "ADD/HOLD/TRIM/EXIT" in output
    for section in (
        "Scope and data time",
        "One-line conclusion",
        "Overview",
        "Three principal risks",
        "Complete holdings table",
        "Selected deep research",
        "Option risk",
        "Macro impact",
        "Observations",
        "Data gaps and sources",
    ):
        assert section in output


def test_research_options_recommendation_and_degradation_are_explicit():
    research = _read("references/research-policy.md")
    for requirement in (
        ">=5% of NAV",
        "top five weights",
        "three principal risks",
        "user-named",
        "maximum of eight",
        "at least four recent quarters",
        "30 days",
        "thesis invalidation",
        "industry cycle",
        "macro sensitivity",
        "daily: six months",
        "weekly: two years",
        "support/resistance zones",
        "never the sole basis",
    ):
        assert requirement in research

    options = _read("references/options-policy.md")
    for requirement in (
        "long/short Calls/Puts",
        "multiplier",
        "notional Delta",
        "expiry",
        "ITM/OTM",
        "covered/protected",
        "unavailable/degraded",
    ):
        assert requirement in options
    assert "does not currently expose all" in options

    recommendations = _read("references/recommendation-policy.md")
    assert "explicitly requests actionable change" in recommendations
    assert "quantifiable target weight, amount, or shares" in recommendations
    assert "one scenario" in recommendations
    assert "unheld new stocks" in recommendations

    degradation = _read("references/failure-and-degradation.md")
    assert "STOP" in degradation
    assert "holdings, NAV, or USD basis" in degradation
    for local_failure in ("sector", "correlation", "options", "research", "technical"):
        assert local_failure in degradation
    assert "Missing facts are unknown" in degradation


def test_tool_contracts_distinguish_current_cli_from_unavailable_capabilities():
    snapshot = _read("references/tool-contract/core/snapshot.md")
    analyze = _read("references/tool-contract/core/analyze.md")
    risk = _read("references/tool-contract/core/risk.md")
    options = _read("references/tool-contract/core/options.md")
    research = _read("references/tool-contract/core/research.md")
    technical = _read("references/tool-contract/core/technical.md")

    assert "snapshot --trd-env REAL --base-currency USD --json --no-save" in snapshot
    assert "analyze --snapshot" in analyze and "--no-save" in analyze
    assert "risk --snapshot" in risk and "--lookback" in risk
    assert "options --snapshot" in options and "RAW_CONTRACT_UNITS_NO_MULTIPLIER" in options
    assert "No dedicated research CLI is currently exposed" in research
    assert "No dedicated technical CLI is currently exposed" in technical
    assert "unavailable/degraded" in research
    assert "unavailable/degraded" in technical

    current_cli = {"snapshot", "analyze", "risk", "options"}
    for name in current_cli:
        assert "## Current CLI" in _read(f"references/tool-contract/core/{name}.md")
    for name in ("research", "technical"):
        assert "## Capability status" in _read(f"references/tool-contract/core/{name}.md")


def test_three_behavior_evals_have_machine_checkable_assertions():
    document = json.loads(_read("evals/evals.json"))
    assert document["skill_name"] == "portfolio-analyst"
    assert len(document["evals"]) == 3
    assert {case["id"] for case in document["evals"]} == {
        "comprehensive-many-holdings",
        "conditional-options-degradation",
        "explicit-rebalance-request",
    }
    for case in document["evals"]:
        assert case["prompt"].strip()
        assert case["expected_behavior"]
        assert len(case["assertions"]) >= 4
        for assertion in case["assertions"]:
            assert assertion["type"] in {
                "contains",
                "not_contains",
                "equals",
                "count_equals",
            }
            assert assertion["target"].strip()
            assert "value" in assertion
