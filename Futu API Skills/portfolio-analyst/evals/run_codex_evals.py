"""Execute Codex against synthetic portfolio fixtures and retain host-observed evidence.

No live brokerage or web access. The model receives the Skill and case prompt, not the
assertions. This runner evaluates routing/report behavior, not OpenD connectivity or
financial/research accuracy. Missing/ambiguous execution evidence never counts as PASS.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MAX_EVENTS_BYTES = 8 * 1024 * 1024
SYMBOL = re.compile(r"\b(?:US|HK)\.[A-Z][A-Z0-9.-]{0,63}\b")
SECTION_ALIASES = {
    "Scope and data time": ("范围与数据时间", "分析范围与数据时间", "范围与时间"),
    "One-line conclusion": ("一句话结论", "核心结论"),
    "Overview": ("账户概览", "组合概览", "概览"),
    "Three principal risks": ("三个主要风险", "三大风险", "三个核心风险", "三个主要风险观察"),
    "Complete holdings table": ("完整持仓", "完整持仓表"),
    "Selected deep research": (
        "重点研究范围与结果",
        "精选深度研究",
        "重点研究",
        "深度研究",
        "选定持仓的重点研究",
    ),
    "Option risk": ("期权风险", "期权分析"),
    "Macro impact": ("宏观影响",),
    "Observations": ("观察", "观察结论", "组合观察"),
    "Data gaps and sources": ("数据缺口与来源", "数据缺口及来源", "数据缺口与数据来源"),
}


def _validator():
    spec = importlib.util.spec_from_file_location(
        "portfolio_audit_validator", ROOT / "evals/run_evals.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cases():
    return _validator().load_eval_cases(ROOT / "evals/evals.json")


def _write(path, content):
    with path.open("xb") as stream:
        stream.write(content)
    path.chmod(0o600)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _fixture(case_id):
    from portfolio_analyst.classification import Classification, ClassificationMapper
    from portfolio_analyst.fx import IdentityOnlyCurrencyConverter
    from portfolio_analyst.models import Account, AccountNAV, CashBalance, DataQuality, Position
    from portfolio_analyst.option_risk import analyze_option_risk
    from portfolio_analyst.risk import analyze_risk
    from portfolio_analyst.snapshot import PortfolioBuilder
    from portfolio_analyst.technical import analyze_technical

    as_of = datetime(2026, 9, 2, 14, tzinfo=timezone.utc)
    if case_id == "comprehensive-many-holdings":
        symbols = "AAPL MSFT AMZN GOOGL META NVDA AVGO TSLA JPM V XOM COST UNH HD PG MA NFLX AMD CRM ORCL ADBE CSCO QCOM INTC AMAT LRCX MU PEP KO MCD WMT TMO ABBV MRK CAT BA GE".split()
        weights = [Decimal("0.05")] * 12 + [Decimal("0.012")] * 25
        types = ["STOCK"] * len(symbols)
    elif case_id == "conditional-options-degradation":
        symbols = [
            "AAPL",
            "MSFT",
            "SPY",
            "AAPL270115C00100000",
            "MSFT270115P00100000",
            "SPY270115C00100000",
        ]
        weights = [Decimal("0.15")] * 3 + [Decimal("0.01")] * 3
        types = ["STOCK", "STOCK", "ETF", "OPTION", "OPTION", "OPTION"]
    elif case_id == "explicit-rebalance-request":
        symbols, types = ["AAPL", "MSFT", "SPY"], ["STOCK", "STOCK", "ETF"]
        weights = [Decimal("0.4"), Decimal("0.3"), Decimal("0.2")]
    else:
        raise ValueError("unknown synthetic case")
    nav = Decimal("100000")
    positions = tuple(
        Position(
            account_ref="acct-synthetic",
            code=f"US.{symbol}",
            name=f"Synthetic fixture {symbol}",
            security_type=kind,
            currency="USD",
            currency_provenance="AUDITED_SOURCE",
            quantity=weight * nav / 100,
            can_sell_quantity=weight * nav / 100,
            average_cost_local=Decimal("90"),
            last_price_local=Decimal("100"),
            market_value_local=weight * nav,
            unrealized_pl_local=weight * nav / 10,
            realized_pl_local=Decimal(0),
        )
        for symbol, weight, kind in zip(symbols, weights, types)
    )
    snapshot = PortfolioBuilder(
        base_currency="USD",
        converter=IdentityOnlyCurrencyConverter(),
        classifications=ClassificationMapper(
            {
                row.code: Classification(
                    sector="SYNTHETIC",
                    industry="SYNTHETIC",
                    region="US",
                    themes=("FIXTURE",),
                    asset_class="DERIVATIVE" if row.security_type == "OPTION" else "EQUITY",
                )
                for row in positions
            }
        ),
    ).build(
        accounts=(Account("acct-synthetic", "REAL", "CASH", "NORMAL", ("US",), "ACTIVE"),),
        account_navs=(AccountNAV("acct-synthetic", "USD", nav),),
        cash_balances=(CashBalance("acct-synthetic", "USD", nav * (1 - sum(weights))),),
        positions=positions,
        as_of=as_of,
        trd_env="REAL",
        source_version="SYNTHETIC_EVAL_ONLY",
        data_quality=DataQuality(
            "COMPLETE", coverage={"accounts_returned": 1, "accounts_returned_read": 1}
        ),
    )
    risk = analyze_risk(snapshot).to_dict()
    technical = analyze_technical(snapshot, provider=None, risk_document=risk).to_dict()

    class Quotes:
        def quote(self, identity, *, timeout):
            return {
                "data": [
                    {
                        "code": identity,
                        "delta": "0.5",
                        "gamma": "0.1",
                        "vega": "2",
                        "theta": "-0.25",
                        "rho": "0.75",
                        "expire_time": "2027-01-15",
                    }
                ]
            }

    options = (
        analyze_option_risk(
            snapshot, quote_provider=Quotes(), analysis_now=as_of, clock=lambda: as_of
        ).to_dict()
        if "OPTION" in types
        else None
    )
    return {
        "snapshot": snapshot.to_dict(),
        "risk": risk,
        "technical": technical,
        "options": options,
        "degradations": [],
    }


def prepare_workspace(workspace, case_id):
    workspace.mkdir(mode=0o700)
    for name in ("references", "portfolio_analyst", "schemas"):
        shutil.copytree(
            ROOT / name, workspace / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
    shutil.copy2(ROOT / "SKILL.md", workspace / "SKILL.md")
    (workspace / "scripts").mkdir(mode=0o700)
    for name in ("current_analysis.py", "portfolio.py"):
        shutil.copy2(ROOT / "evals/fixture_cli.py", workspace / "scripts" / name)
    (workspace / ".fixture").mkdir(mode=0o700)
    envelope = _fixture(case_id)
    _write(workspace / ".fixture/envelope.json", _json_bytes(envelope))
    _write(workspace / ".fixture/snapshot.json", _json_bytes(envelope["snapshot"]))
    if case_id == "explicit-rebalance-request":
        _write(
            workspace / ".fixture/request.json",
            _json_bytes(
                {
                    "schema_version": "1.1",
                    "snapshot_id": envelope["snapshot"]["snapshot_id"],
                    "base_currency": "USD",
                    "changes": [
                        {"symbol": "US.AAPL", "operation": "TARGET_WEIGHT", "value": "0.12"}
                    ],
                    "new_cash_base": "0",
                    "allow_margin": False,
                    "allow_short": False,
                    "sell_only": False,
                    "new_cash_only": False,
                }
            ),
        )
    return envelope


def parse_events(raw):
    if len(raw) > MAX_EVENTS_BYTES:
        raise ValueError("MODEL_EVENTS_TOO_LARGE")
    try:
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except (ValueError, UnicodeError):
        raise ValueError("MODEL_EVENTS_INVALID") from None
    if (
        not all(isinstance(event, dict) for event in events)
        or not any(event.get("type") == "turn.completed" for event in events)
        or any(event.get("type") in {"turn.failed", "error"} for event in events)
    ):
        raise ValueError("MODEL_EXECUTION_INCOMPLETE")
    return events


def _sections(markdown):
    """Observe headings in either language; never take a model-authored trace as evidence."""
    aliases = {
        name.casefold(): key for key, names in SECTION_ALIASES.items() for name in (key, *names)
    }
    sections, current, current_level = {}, None, None
    for line in markdown.splitlines():
        match = re.fullmatch(r"\s*(?:(#{1,6})\s+(.+?)\s*#*|\*\*(.+?)\*\*)\s*", line)
        if match and match.group(3) and match.group(3).rstrip().endswith((":", "：")):
            # A bold colon label introduces paragraph/list content, not a peer section.
            match = None
        title = (match.group(2) or match.group(3)) if match else ""
        level = (len(match.group(1)) if match.group(1) else 2) if match else None
        title = re.sub(r"^\d+[.)、：:]\s*", "", title).strip().strip(":：")
        title = re.split(r"[:：（(]|\s+[—–-]\s+", title, maxsplit=1)[0].strip()
        if re.fullmatch(r"三[个项](?:主要|核心)风险(?:观察|审视)?", title):
            title = "Three principal risks"
        canonical = aliases.get(title.casefold())
        if canonical:
            current, current_level = canonical, level
            sections.setdefault(current, [])
        elif current and level is not None and level <= current_level:
            # An unfamiliar peer heading is still a boundary. Never attribute a
            # later research/appendix table to the preceding holdings section.
            current, current_level = None, None
        elif current:
            sections[current].append(line)
    return {key: "\n".join(lines) for key, lines in sections.items()}


def _section(markdown, heading):
    return _sections(markdown).get(heading, "")


def _research_without_peer_comparisons(research, held):
    """Exclude declared comparison scopes; their prose still requires semantic review."""
    lines, selected_holding, comparison = [], False, False
    for line in research.splitlines():
        heading = re.fullmatch(r"\s*(#{1,6})\s+(.+?)\s*#*\s*", line)
        if heading:
            level, title = len(heading.group(1)), heading.group(2).strip()
            if level <= 4:
                comparison = False
            if level <= 3:
                identities = set(SYMBOL.findall(title))
                selected_holding = level == 3 and len(identities) == 1 and identities <= held
            if (
                level == 4
                and selected_holding
                and title.casefold()
                in {
                    "peer comparisons",
                    "同行比较",
                }
            ):
                comparison = True
        if not comparison:
            lines.append(line)
    return "\n".join(lines)


def _source_as_of_present(markdown, source_as_of):
    """Accept only explicit-zone timestamps representing the exact source instant."""
    source = datetime.fromisoformat(source_as_of.replace("Z", "+00:00"))
    pattern = (
        r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
        r"(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2}| UTC)\b"
    )
    for match in re.finditer(pattern, markdown):
        value = match.group().replace(" UTC", "+00:00").replace("Z", "+00:00")
        try:
            reported = datetime.fromisoformat(value)
        except ValueError:
            continue
        if reported == source:
            return True
    return False


def _argv(command):
    try:
        values = shlex.split(command)
        if (
            values
            and Path(values[0]).name in {"bash", "zsh", "sh"}
            and len(values) == 3
            and values[1] in {"-c", "-lc"}
        ):
            command = values[2]
            values = shlex.split(command)
        # Only direct literal invocations establish a receipt, never shell composition.
        if re.search(r"[;&|<>()`$\r\n]", command):
            return []
        if values and Path(values[0]).name in {"python", "python3"} and values[1:2] == ["-B"]:
            values = values[:1] + values[2:]
        return values
    except ValueError:
        return []


def build_audit(case, events, fixture, workspace):
    from portfolio_analyst.schema import validate_rebalance_result_document
    from jsonschema import ValidationError

    completed = [
        event["item"]
        for event in events
        if event.get("type") == "item.completed" and isinstance(event.get("item"), dict)
    ]
    answers = [item.get("text", "") for item in completed if item.get("type") == "agent_message"]
    response = answers[-1] if answers else ""
    if not response.strip():
        raise ValueError("MODEL_RESPONSE_MISSING")
    commands, loaded, snapshots, simulations = [], set(), [], []
    references = {
        str(path.relative_to(workspace)): path.read_text()
        for path in (workspace / "references").rglob("*.md")
    }
    references["SKILL.md"] = (workspace / "SKILL.md").read_text()
    for item in completed:
        if item.get("type") != "command_execution":
            continue
        command = item.get("command", "")
        commands.append(command)
        if item.get("exit_code") != 0 or item.get("status") != "completed":
            continue
        output = item.get("aggregated_output", "")
        args = _argv(command)
        for name, body in references.items():
            if name in command and body.strip() in output:
                loaded.add(name)
        if len(args) < 2 or Path(args[0]).name not in {"python", "python3"}:
            continue
        try:
            body = json.loads(output)
        except ValueError:
            continue
        if (
            args[1] in {"scripts/current_analysis.py", "./scripts/current_analysis.py"}
            and len(args) == 2
            and body == fixture
        ):
            snapshots.append(body["snapshot"])
        if (
            args[1] in {"scripts/portfolio.py", "./scripts/portfolio.py"}
            and len(args) > 2
            and args[2] == "simulate"
        ):
            try:
                validate_rebalance_result_document(body)
            except (ValidationError, ValueError, KeyError, TypeError):
                continue
            if body["source_snapshot"] == fixture["snapshot"]:
                simulations.append(body)
    source = fixture["snapshot"]
    held = {f"{row['market']}.{row['symbol']}" for row in source["positions"]}
    holdings = []
    for line in _section(response, "Complete holdings table").splitlines():
        cells = line.strip().split("|")
        if len(cells) < 4:
            continue
        cell = cells[1].strip().strip("`* ")
        identity = cell if cell in held else f"US.{cell}"
        if identity in held:
            holdings.append(identity)
    research = _research_without_peer_comparisons(
        _section(response, "Selected deep research"), held
    )
    deep = set()
    for line in research.splitlines():
        cells = line.strip().split("|")
        label = cells[1].strip() if len(cells) >= 4 else line[4:] if line.startswith("### ") else ""
        if label:
            deep.update(SYMBOL.findall(label))
            for identity in held:
                if re.search(
                    rf"(?<![A-Z0-9]){re.escape(identity.split('.', 1)[1])}(?![A-Z0-9])", label
                ):
                    deep.add(identity)
    eligible = {
        f"{row['market']}.{row['symbol']}"
        for row in source["positions"]
        if Decimal(row["weight"]) >= Decimal("0.05")
    }
    gaps = _section(response, "Data gaps and sources")
    omitted = eligible - deep
    omissions_disclosed = (
        bool(omitted)
        and all(identity in gaps or identity.split(".", 1)[1] in gaps for identity in omitted)
        and bool(re.search(r"omit|cap|limit|未|遗漏|上限|限制", gaps, re.I))
    )
    option_text = _section(response, "Option risk")
    option_report = fixture["options"]
    reported_status = re.search(
        r"(?:tool status|工具状态|工具输出状态)[\s*`：:]+(COMPLETE|PARTIAL|UNAVAILABLE)\b",
        option_text,
        re.I,
    )
    option_observation = (
        {}
        if option_report is None
        else {
            "tool_status": option_report["status"],
            "reported_tool_status": reported_status.group(1).upper() if reported_status else None,
            "analysis_gaps_disclosed": bool(
                re.search(r"analysis capability gaps|分析能力缺口|分析能力限制", option_text, re.I)
            ),
            "unit_label": option_report["provenance"]["greek_unit"],
            "capability_gaps": [
                field
                for field, pattern in (
                    ("multiplier", r"multiplier|合约乘数"),
                    ("underlying", r"underlying|标的"),
                    ("moneyness", r"moneyness|ITM[/／](?:ATM[/／])?OTM|价内|价外"),
                )
                if re.search(pattern, option_text, re.I)
            ],
        }
    )
    claimed = set(SYMBOL.findall(research + _section(response, "Observations")))
    observations = {
        "report": {"sections": list(_sections(response))},
        "holdings": {"symbols": holdings},
        "deep_research": {
            "max_names": len(deep),
            "eligible_omissions_disclosed": omissions_disclosed,
        },
        "options": option_observation,
        "workflow": {
            "stop": len(snapshots) != 1 or set(holdings) != held or len(holdings) != len(held)
        },
        "simulation": {
            "scenarios": [item["request"] for item in simulations],
            "target_weight": "0.12"
            if len(simulations) == 1
            and simulations[0]["request"]["changes"]
            == [{"symbol": "US.AAPL", "operation": "TARGET_WEIGHT", "value": "0.12"}]
            else None,
        },
        "unheld_candidates": {"count": len(claimed - held)},
    }
    required_reads = {
        "SKILL.md",
        "references/workflow.md",
        "references/output-contract.md",
        "references/portfolio-analysis-policy.md",
        "references/failure-and-degradation.md",
        "references/intent-routing.md",
        "references/evidence-policy.md",
        "references/research-policy.md",
        "references/tool-contract/core/snapshot.md",
        "references/tool-contract/core/risk.md",
        "references/tool-contract/core/research.md",
        "references/tool-contract/core/technical.md",
    }
    execution_checks = {
        "one_bound_snapshot_and_complete_holdings": not observations["workflow"]["stop"],
        "required_reference_reads_complete": required_reads.issubset(loaded),
        "source_snapshot_id_present": source["snapshot_id"] in response,
        "source_as_of_present": _source_as_of_present(response, source["as_of"]),
        "no_file_changes": not any(item.get("type") == "file_change" for item in completed),
        "no_forbidden_commands": not any(
            re.search(r"place_order|unlock_trade|--save\b|\bcurl\b|\bwget\b", command)
            for command in commands
        ),
    }
    observations["execution"] = {
        "evidence_pass": all(execution_checks.values()),
        "checks": execution_checks,
    }
    validator = _validator()
    return {
        "schema_version": "1.0",
        "case_id": case["id"],
        "provenance": {
            "producer_kind": "external_harness",
            "runner_id": "codex-synthetic-portfolio",
            "runner_version": "1.0.5",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "case_id": case["id"],
            "case_input_sha256": validator.case_input_sha256(case),
        },
        "response_markdown": response,
        "trace": {
            "snapshot": {
                key: source[key] for key in ("trd_env", "base_currency", "snapshot_id", "as_of")
            }
            if len(snapshots) == 1
            else {},
            "loaded_references": sorted(loaded),
            "executed_commands": commands,
            "observations": observations,
        },
    }


def run_case(case_id, output, *, runner=None, timeout=300):
    from portfolio_analyst.option_metadata import _bounded_subprocess_run

    if not 1 <= timeout <= 600:
        raise ValueError("timeout must be between 1 and 600 seconds")
    case = load_cases()[case_id]
    output.mkdir(mode=0o700)
    workspace = output / "workspace"
    fixture = prepare_workspace(workspace, case_id)
    prompt = (
        "Use $portfolio-analyst in ./SKILL.md to handle the following user request. "
        "This is an isolated synthetic account fixture, not a live account. "
        "The scripts supply fixed synthetic data; do not access other folders, accounts, network, "
        "credentials, or installed skills. Web access is disabled in this evaluation. "
        "Use separate shell commands for complete reference reads and each portfolio command so "
        "the host can retain complete receipts. Do not write files. If simulation is requested, "
        "the bound input files are .fixture/snapshot.json and .fixture/request.json. "
        "Return the user-facing Markdown report only; do not produce an audit or test verdict.\n\n"
        + case["prompt"]
    )
    _write(output / "prompt.txt", prompt.encode())
    manifest = {
        str(path.relative_to(workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    _write(
        output / "inputs.json",
        _json_bytes(
            {
                "synthetic": True,
                "case": {key: case.get(key) for key in ("id", "prompt", "fixture")},
                "files": manifest,
            }
        ),
    )
    command = [
        "codex",
        "-a",
        "never",
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
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
    result = {
        "case_id": case_id,
        "status": "EXECUTION_UNAVAILABLE",
        "behavior_pass": False,
        "scope": "SYNTHETIC_ROUTING_AND_REPORT_ONLY",
        "live_opend_verified": False,
    }
    try:
        completed = (runner or _bounded_subprocess_run)(
            command,
            input=prompt.encode(),
            cwd=workspace,
            env=environment,
            timeout=timeout,
            max_output_bytes=MAX_EVENTS_BYTES,
            shell=False,
        )
        _write(output / "events.jsonl", completed.stdout)
        _write(output / "stderr.txt", completed.stderr)
        if completed.returncode != 0:
            raise ValueError("CODEX_EXEC_FAILED")
        events = parse_events(completed.stdout)
        if {
            str(path.relative_to(workspace)) for path in workspace.rglob("*") if path.is_file()
        } != set(manifest) or any(
            hashlib.sha256((workspace / name).read_bytes()).hexdigest() != digest
            for name, digest in manifest.items()
        ):
            raise ValueError("FIXTURE_CHANGED")
        audit = build_audit(case, events, fixture, workspace)
        _write(output / "audit.json", _json_bytes(audit))
        _write(output / "response.md", audit["response_markdown"].encode())
        verdict = _validator().run(ROOT / "evals/evals.json", [output / "audit.json"])
        _write(output / "assertions.json", _json_bytes(verdict))
        evidence_pass = audit["trace"]["observations"]["execution"]["evidence_pass"]
        behavior_pass = verdict["audit_validation_succeeded"] and evidence_pass
        result.update(
            status="ASSERTIONS_PASSED" if behavior_pass else "ASSERTIONS_FAILED",
            behavior_pass=behavior_pass,
            execution_evidence_pass=evidence_pass,
        )
        result["requires_manual_review"] = True
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result["error"] = type(exc).__name__
        for name, content in (
            ("events.jsonl", getattr(exc, "output", None)),
            ("stderr.txt", getattr(exc, "stderr", None)),
        ):
            if not (output / name).exists():
                _write(output / name, content if isinstance(content, bytes) else b"")
    _write(output / "result.json", _json_bytes(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=sorted(load_cases()), required=True)
    parser.add_argument(
        "--output", type=Path, required=True, help="new output directory; never overwritten"
    )
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    try:
        result = run_case(args.case, args.output.absolute(), timeout=args.timeout)
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "EXECUTION_UNAVAILABLE",
                    "behavior_pass": False,
                    "error": type(exc).__name__,
                }
            )
        )
        return 2
    print(json.dumps(result))
    return 0 if result["behavior_pass"] else 1 if result["status"] == "ASSERTIONS_FAILED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
