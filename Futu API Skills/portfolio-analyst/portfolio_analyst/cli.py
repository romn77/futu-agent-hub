"""Unified command-line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from .doctor import run_doctor
from .account_alias import AccountAliasError, AccountAliasStore, AccountAliaser
from .futu_adapter import FutuAdapter, FutuReadError
from .persistence import SnapshotAlreadyExists, SnapshotStore, bundle_manifest
from .private_home import (
    PrivateHomeError,
    ensure_private_home,
    require_secure_platform_mutation,
)
from .reporting import build_portfolio_report
from .rendering import terminal_text
from .repository import SourceDataError
from .rebalance import RebalanceError, load_rebalance_request, simulate_rebalance
from .risk import (
    PolicyConfigError,
    RiskConfigError,
    RiskInputError,
    UnsupportedRiskError,
    analyze_risk,
    load_portfolio_policy,
)
from .schema import (
    ReportSemanticError,
    validate_lifecycle_document,
    validate_portfolio_report_document,
    validate_rebalance_result_document,
    validate_risk_document,
    validate_option_risk_document,
    validate_technical_document,
)
from .option_quote import FutuOptionQuoteProvider
from .option_metadata import FutuOptionMetadataProvider
from .option_risk import OptionRiskInputError, analyze_option_risk
from .skill_trust import validate_option_quote_capability
from .journal_repository import JournalError, JournalRepository
from .lifecycle import (
    MAX_HISTORY_SNAPSHOTS,
    LifecycleInputError,
    analyze_lifecycle,
    unavailable_lifecycle_report,
)
from .lifecycle_repository import LifecycleConfigError, LifecycleRepository, record_from_document
from .snapshot_repository import (
    MAX_HISTORY_TOTAL_BYTES,
    SnapshotRepository,
    SnapshotRepositoryError,
)
from .service import ConfigError, generate_snapshot
from .settings import Settings, SettingsError, discover_futuapi_skill, resolve_output_path
from .snapshot import UnsafeAggregationError
from .snapshot_io import InvalidSnapshot
from .trade_history import FutuTradeHistoryService, HistoryBudgetLimit
from .trade_review import TradePolicyConfigError, TradeReviewInputError, load_trade_policy
from .performance import (
    AttributionInputError,
    ExternalFlowEvidence,
    MAX_ATTRIBUTION_SNAPSHOTS,
    analyze_performance,
)
from .technical import (
    FutuTechnicalHistoryProvider,
    TechnicalInputError,
    analyze_technical,
)


class _JsonParseError(ValueError):
    pass


class FutureSnapshotError(AttributionInputError):
    pass


def _load_selected_snapshot(data_home: Path, *, latest: bool, relative_path: str | Path | None):
    repository = SnapshotRepository(data_home)
    if latest:
        return repository.latest(
            limit=MAX_HISTORY_SNAPSHOTS, max_total_bytes=MAX_HISTORY_TOTAL_BYTES
        )
    if relative_path is None:
        raise SnapshotRepositoryError("explicit snapshot path is required")
    return repository.read_path(relative_path)


class _RaisingArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _JsonParseError("invalid command-line arguments")


def _parser(*, json_errors: bool = False) -> argparse.ArgumentParser:
    parser_class = _RaisingArgumentParser if json_errors else argparse.ArgumentParser
    parser = parser_class(prog="portfolio.py", description="Read-only portfolio analyst")
    parser.add_argument("--json", action="store_true", dest="as_json", help="print JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="check Skill, SDK, and OpenD availability")
    doctor.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
        help="print JSON",
    )
    doctor.add_argument("--output", help="save under PORTFOLIO_AGENT_HOME")
    doctor.add_argument("--no-save", action="store_true", help="do not save output")
    doctor.add_argument("--verbose", action="store_true", help="show check details")
    snapshot = subparsers.add_parser("snapshot", help="build a normalized portfolio snapshot")
    snapshot.add_argument("--trd-env", choices=["REAL", "SIMULATE"], default="REAL")
    snapshot.add_argument(
        "--base-currency",
        choices=["HKD", "USD", "CNH", "JPY", "AUD", "CAD", "MYR", "SGD"],
        default="USD",
    )
    snapshot.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
        help="print JSON",
    )
    snapshot.add_argument(
        "--output",
        help="unsupported for snapshots; use --save for the canonical content-addressed path",
    )
    persistence = snapshot.add_mutually_exclusive_group()
    persistence.add_argument(
        "--save", action="store_true", help="save immutably (default path uses snapshot ID)"
    )
    persistence.add_argument(
        "--no-save", action="store_true", help="do not save, even when --output is supplied"
    )
    snapshot.add_argument("--verbose", action="store_true", help="show non-sensitive details")
    for command, help_text in (
        ("risk", "analyze risk from one immutable snapshot"),
        ("analyze", "build comprehensive JSON and Markdown portfolio reports"),
    ):
        analysis = subparsers.add_parser(command, help=help_text)
        source = analysis.add_mutually_exclusive_group(required=True)
        source.add_argument("--latest", action="store_true", help="use latest valid saved snapshot")
        source.add_argument("--snapshot", help="relative immutable snapshot path under data home")
        analysis.add_argument("--lookback", type=int, default=120)
        analysis.add_argument(
            "--json", action="store_true", dest="as_json", default=argparse.SUPPRESS
        )
        analysis.add_argument("--output", help="relative output path under data home")
        analysis.add_argument("--no-save", action="store_true")
        analysis.add_argument("--verbose", action="store_true")
    options = subparsers.add_parser(
        "options", help="inventory raw option Greeks from one immutable snapshot"
    )
    option_source = options.add_mutually_exclusive_group(required=True)
    option_source.add_argument("--latest", action="store_true", help="use latest valid snapshot")
    option_source.add_argument(
        "--snapshot", help="relative immutable snapshot path under data home"
    )
    options.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    options.add_argument("--output", help="relative output directory under data home")
    options.add_argument("--no-save", action="store_true")
    options.add_argument("--verbose", action="store_true")
    technical = subparsers.add_parser(
        "technical", help="derive bounded support/resistance context for selected holdings"
    )
    technical_source = technical.add_mutually_exclusive_group(required=True)
    technical_source.add_argument("--latest", action="store_true", help="use latest valid snapshot")
    technical_source.add_argument(
        "--snapshot", help="relative immutable snapshot path under data home"
    )
    technical.add_argument(
        "--risk-report", help="optional relative validated risk report under data home"
    )
    technical.add_argument(
        "--symbol", action="append", default=[], help="additional held US stock/ETF"
    )
    technical.add_argument(
        "--json", action="store_true", dest="as_json", default=argparse.SUPPRESS
    )
    technical.add_argument("--output", help="relative output directory under data home")
    technical.add_argument("--no-save", action="store_true")
    technical.add_argument("--verbose", action="store_true")
    simulate = subparsers.add_parser("simulate", help="run a non-executable rebalance simulation")
    source = simulate.add_mutually_exclusive_group(required=True)
    source.add_argument("--latest", action="store_true", help="use latest valid saved snapshot")
    source.add_argument("--snapshot", help="relative immutable snapshot path under data home")
    simulate.add_argument("--request", required=True, help="relative request JSON under data home")
    simulate.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    simulate.add_argument("--output", help="relative output directory under data home")
    simulate.add_argument("--no-save", action="store_true")
    simulate.add_argument("--verbose", action="store_true")
    trades = subparsers.add_parser("trades", help="review historical orders and fills read-only")
    trades.add_argument("--from", required=True, dest="start", help="inclusive YYYY-MM-DD")
    trades.add_argument("--to", required=True, dest="end", help="inclusive YYYY-MM-DD")
    trades.add_argument("--trd-env", choices=["REAL", "SIMULATE"], default="REAL")
    trade_snapshot = trades.add_mutually_exclusive_group()
    trade_snapshot.add_argument("--latest", action="store_true", help="use latest NAV evidence")
    trade_snapshot.add_argument("--snapshot", help="relative immutable snapshot for NAV evidence")
    trades.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    trades.add_argument("--output", help="relative output directory under data home")
    trades.add_argument("--no-save", action="store_true")
    trades.add_argument("--verbose", action="store_true")

    performance = subparsers.add_parser(
        "performance", help="attribute P&L across immutable snapshot history"
    )
    performance.add_argument("--from", required=True, dest="start", help="inclusive UTC date")
    performance.add_argument("--to", required=True, dest="end", help="inclusive UTC date")
    performance.add_argument(
        "--json", action="store_true", dest="as_json", default=argparse.SUPPRESS
    )
    performance.add_argument("--output", help="relative bundle directory under data home")
    performance.add_argument("--no-save", action="store_true")
    performance.add_argument("--verbose", action="store_true")

    lifecycle = subparsers.add_parser("lifecycle", help="inspect or validate local lifecycle facts")
    lifecycle_actions = lifecycle.add_subparsers(dest="lifecycle_action", required=True)
    lifecycle_init = lifecycle_actions.add_parser("init", help="explicitly create one user record")
    lifecycle_init.add_argument("symbol")
    lifecycle_init.add_argument(
        "--role",
        required=True,
        choices=[
            "CORE",
            "SATELLITE",
            "TRADING",
            "DEFENSIVE",
            "HEDGE",
            "CASH_ALTERNATIVE",
            "UNCLASSIFIED",
        ],
    )
    lifecycle_init.add_argument("--status", default="ACTIVE", choices=["ACTIVE", "EXITED"])
    lifecycle_init.add_argument("--opened-at", required=True)
    lifecycle_init.add_argument("--holding-horizon", required=True)
    lifecycle_init.add_argument("--target-min", required=True)
    lifecycle_init.add_argument("--target-max", required=True)
    lifecycle_init.add_argument("--original-thesis", required=True)
    lifecycle_init.add_argument("--invalidation-condition", action="append", required=True)
    lifecycle_init.add_argument("--review-trigger", action="append", required=True)
    lifecycle_init.add_argument("--note", action="append", default=[])
    lifecycle_init.add_argument("--last-reviewed-at", required=True)
    lifecycle_init.add_argument(
        "--json", action="store_true", dest="as_json", default=argparse.SUPPRESS
    )
    lifecycle_list = lifecycle_actions.add_parser("list", help="list configured lifecycle records")
    lifecycle_list.add_argument(
        "--json", action="store_true", dest="as_json", default=argparse.SUPPRESS
    )
    lifecycle_inspect = lifecycle_actions.add_parser(
        "inspect", help="inspect one configured record"
    )
    lifecycle_inspect.add_argument("symbol")
    lifecycle_inspect.add_argument(
        "--json", action="store_true", dest="as_json", default=argparse.SUPPRESS
    )
    lifecycle_validate = lifecycle_actions.add_parser(
        "validate", help="compare records with a snapshot"
    )
    lifecycle_source = lifecycle_validate.add_mutually_exclusive_group(required=True)
    lifecycle_source.add_argument("--latest", action="store_true")
    lifecycle_source.add_argument("--snapshot")
    lifecycle_validate.add_argument(
        "--json", action="store_true", dest="as_json", default=argparse.SUPPRESS
    )
    lifecycle_validate.add_argument("--output", help="relative bundle directory under data home")
    lifecycle_validate.add_argument("--no-save", action="store_true")

    journal = subparsers.add_parser("journal", help="explicit append-only journal operations")
    journal_actions = journal.add_subparsers(dest="journal_action", required=True)
    journal_list = journal_actions.add_parser("list", help="list one journal stream")
    journal_list.add_argument("stream", choices=["decisions", "analyses"])
    journal_list.add_argument(
        "--json", action="store_true", dest="as_json", default=argparse.SUPPRESS
    )
    journal_decision = journal_actions.add_parser("record-decision", help="append a user decision")
    journal_decision.add_argument("--source-snapshot-id", required=True)
    journal_decision.add_argument(
        "--decision", required=True, choices=["ACCEPTED", "REJECTED", "DEFERRED", "SUPERSEDED"]
    )
    journal_decision.add_argument("--summary", required=True)
    journal_decision.add_argument(
        "--json", action="store_true", dest="as_json", default=argparse.SUPPRESS
    )
    journal_analysis = journal_actions.add_parser(
        "record-analysis", help="append an analysis reference"
    )
    journal_analysis.add_argument("--source-snapshot-id", required=True)
    journal_analysis.add_argument("--artifact-path", required=True)
    journal_analysis.add_argument("--summary", required=True)
    journal_analysis.add_argument(
        "--json", action="store_true", dest="as_json", default=argparse.SUPPRESS
    )
    return parser


def _requires_secure_platform_mutation(args: argparse.Namespace) -> bool:
    """Return whether this parsed command explicitly requests persistence."""

    if args.command == "snapshot":
        return bool(args.save and not args.no_save)
    if args.command in {
        "doctor",
        "risk",
        "analyze",
        "options",
        "simulate",
        "trades",
        "performance",
    }:
        return bool(args.output and not args.no_save)
    if args.command == "lifecycle":
        return args.lifecycle_action == "init" or bool(
            args.lifecycle_action == "validate" and args.output and not args.no_save
        )
    if args.command == "journal":
        return args.journal_action in {"record-decision", "record-analysis"}
    return False


def _human_report(report: dict[str, object], verbose: bool) -> str:
    checks = report["checks"]
    lines = ["Portfolio analyst doctor", f"Overall: {'ready' if report['ok'] else 'not ready'}"]
    for name, check in checks.items():
        state = "available" if check["available"] else "unavailable"
        line = f"- {name}: {state}"
        if verbose:
            line += f" ({check['detail']})"
        lines.append(line)
    return "\n".join(lines)


def _human_risk_report(payload: dict[str, Any]) -> str:
    safe = terminal_text
    coverage = payload["coverage"]
    price_coverage = coverage["price_histories"]
    matrix_digest = hashlib.sha256(
        json.dumps(
            payload["correlation"]["matrix"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    lines = [
        "Portfolio risk",
        f"source_snapshot_id: {payload['source_snapshot_id']}",
        f"As of: {payload['as_of']}",
        f"Overall risk: {payload['overall_risk']}",
        f"Source holdings: {coverage['source_holdings']}",
        f"Economic positions: {coverage['positions']}",
        f"Total current positions: {coverage['total_positions']}",
        f"Omitted positions: {coverage['omitted_positions']}",
        f"Budget status: {coverage['budget_status']}",
        "Budget reason: "
        + (safe(coverage["budget_reason"]) if coverage["budget_reason"] else "None"),
        f"Classified sector: {coverage['classified_sector']}",
        f"Classified region: {coverage['classified_region']}",
        f"P&L evidence: {coverage['pnl_evidence_status']}",
        f"Price histories: {price_coverage['available']}/{price_coverage['requested']}",
        f"Data quality: {payload['data_quality_status']}",
        "Data quality notes:",
        *([f"- {safe(note)}" for note in payload["data_quality_notes"]] or ["- None"]),
        f"Top 1: {payload['concentration']['top_1']}",
        f"Top 3: {payload['concentration']['top_3']}",
        f"Top 5: {payload['concentration']['top_5']}",
        f"HHI: {payload['concentration']['hhi']}",
        "Sector exposure: "
        + safe(json.dumps(payload["exposures"]["sector"]["weights"], sort_keys=True)),
        "Region exposure: "
        + safe(json.dumps(payload["exposures"]["region"]["weights"], sort_keys=True)),
        "Theme full exposure: "
        + safe(json.dumps(payload["exposures"]["theme_full"]["weights"], sort_keys=True)),
        "Theme split exposure: "
        + safe(json.dumps(payload["exposures"]["theme_split"]["weights"], sort_keys=True)),
        f"Correlation status: {payload['correlation']['status']}",
        f"Correlation thresholds: medium={payload['correlation']['medium_threshold']}, high={payload['correlation']['high_threshold']}",
        "Market close cutoffs: "
        + safe(json.dumps(payload["correlation"]["date_cutoffs"], sort_keys=True)),
        "Positive risk pairs: " + safe(json.dumps(payload["correlation"]["pairs"], sort_keys=True)),
        "Inverse diversification pairs: "
        + safe(json.dumps(payload["correlation"]["inverse_pairs"], sort_keys=True)),
        "Correlation clusters: "
        + safe(json.dumps(payload["correlation"]["clusters"], sort_keys=True)),
        f"Matrix JSON digest: {matrix_digest} (SHA-256; authoritative matrix is in JSON output)",
        f"Policy status: {payload['policy_status']}",
        "Policy checks: " + safe(json.dumps(payload["policy_checks"], sort_keys=True)),
        "Largest three risks:",
    ]
    for index, finding in enumerate(payload["top_risks"], 1):
        lines.extend(
            [
                f"Risk {index}",
                f"Type: {safe(finding['type'])}",
                f"Severity: {safe(finding['severity'])}",
                f"Summary: {safe(finding['summary'])}",
                "Evidence: "
                + safe(json.dumps(finding["evidence"], ensure_ascii=False, sort_keys=True)),
                "Affected positions: "
                + (", ".join(safe(item) for item in finding["affected_positions"]) or "None"),
                f"Review priority: {finding['review_priority']}",
                "Simulation direction: "
                + (
                    safe(finding["simulation_direction"])
                    if finding["simulation_direction"]
                    else "Not available"
                ),
            ]
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, trusted_now: datetime | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    json_requested = "--json" in raw_argv
    try:
        args = _parser(json_errors=json_requested).parse_args(raw_argv)
    except _JsonParseError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "CLI_USAGE",
                        "message": "invalid command-line arguments",
                    },
                }
            )
        )
        return 2
    try:
        frozen_now = trusted_now or datetime.now(timezone.utc)
        if frozen_now.tzinfo is None or frozen_now.utcoffset() is None:
            raise AttributionInputError("trusted current time must be timezone-aware")
        frozen_now = frozen_now.astimezone(timezone.utc)
        if _requires_secure_platform_mutation(args):
            require_secure_platform_mutation()
        settings = Settings.from_env()
        if args.command == "journal":
            repository = JournalRepository(settings.data_home)
            if args.journal_action == "list":
                records = (
                    repository.list_decisions()
                    if args.stream == "decisions"
                    else repository.list_analyses()
                )
                payload = [item.to_dict() for item in records]
            elif args.journal_action == "record-decision":
                payload = repository.append_decision(
                    timestamp=datetime.now(timezone.utc),
                    source_snapshot_id=args.source_snapshot_id,
                    summary=args.summary,
                    decision=args.decision,
                    metadata={},
                ).to_dict()
            else:
                payload = repository.append_analysis(
                    timestamp=datetime.now(timezone.utc),
                    source_snapshot_id=args.source_snapshot_id,
                    artifact_path=args.artifact_path,
                    summary=args.summary,
                    metadata={},
                ).to_dict()
            rendered = json.dumps(payload, ensure_ascii=False, indent=2)
            print(rendered if args.as_json else terminal_text(rendered))
            return 0

        if args.command == "lifecycle":
            repository = LifecycleRepository(settings.data_home)
            if args.lifecycle_action == "init":
                record = record_from_document(
                    {
                        "schema_version": "1.0",
                        "symbol": args.symbol,
                        "role": args.role,
                        "status": args.status,
                        "opened_at": args.opened_at,
                        "holding_horizon": args.holding_horizon,
                        "target_weight": {"min": args.target_min, "max": args.target_max},
                        "original_thesis": args.original_thesis,
                        "invalidation_conditions": args.invalidation_condition,
                        "review_triggers": args.review_trigger,
                        "notes": args.note,
                        "last_reviewed_at": args.last_reviewed_at,
                    }
                )
                destination = repository.create(record)
                payload = {"created": True, "record": record.to_dict()}
                print(
                    json.dumps(payload, ensure_ascii=False, indent=2)
                    if args.as_json
                    else f"Created {terminal_text(destination.relative_to(settings.data_home))}"
                )
                return 0
            if args.lifecycle_action == "list":
                records = [item.to_dict() for item in repository.list()]
                print(
                    json.dumps(records, ensure_ascii=False, indent=2)
                    if args.as_json
                    else "\n".join(item["symbol"] for item in records)
                )
                return 0
            if args.lifecycle_action == "inspect":
                record = repository.read(args.symbol)
                payload = record.to_dict()
                print(
                    json.dumps(payload, ensure_ascii=False, indent=2)
                    if args.as_json
                    else terminal_text(json.dumps(payload, ensure_ascii=False))
                )
                return 0
            source_snapshot = _load_selected_snapshot(
                settings.data_home, latest=args.latest, relative_path=args.snapshot
            )
            try:
                history = SnapshotRepository(settings.data_home).list(
                    end=source_snapshot.as_of,
                    limit=MAX_HISTORY_SNAPSHOTS,
                    max_total_bytes=MAX_HISTORY_TOTAL_BYTES,
                )
                history_issue = None
            except SnapshotRepositoryError as exc:
                history = ()
                history_issue = {"code": "HISTORY_INVALID", "detail": terminal_text(exc)}
            lifecycle_report = analyze_lifecycle(
                source_snapshot, repository, history=history, history_issue=history_issue
            )
            payload = lifecycle_report.to_dict()
            validate_lifecycle_document(payload)
            rendered = json.dumps(payload, ensure_ascii=False, indent=2)
            if args.output and not args.no_save:
                artifact_files = {
                    "lifecycle_report.json": rendered + "\n",
                    "lifecycle_report.md": lifecycle_report.to_markdown(
                        json_artifact="lifecycle_report.json"
                    ),
                }
                SnapshotStore(settings.data_home).save_bundle(
                    args.output,
                    {
                        **artifact_files,
                        "manifest.json": bundle_manifest(
                            source_snapshot_id=source_snapshot.snapshot_id,
                            as_of=source_snapshot.to_dict()["as_of"],
                            report_id=lifecycle_report.lifecycle_report_id,
                            files=artifact_files,
                        ),
                    },
                )
            print(rendered if args.as_json else lifecycle_report.to_human())
            return 0

        if args.command == "doctor":
            report = run_doctor(host=settings.opend_host, port=settings.opend_port)
            rendered = (
                json.dumps(report, ensure_ascii=False, indent=2)
                if args.as_json
                else _human_report(report, args.verbose)
            )
            if args.output and not args.no_save:
                private_home = ensure_private_home(settings.data_home, create=True)
                resolve_output_path(private_home, Path(args.output))
                SnapshotStore(private_home).save(args.output, rendered + "\n")
            print(rendered)
            return 0 if report["ok"] else 1

        if args.command == "performance":
            try:
                start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
            except ValueError:
                raise AttributionInputError("performance dates must use YYYY-MM-DD") from None
            if start > end or end > frozen_now.date() or (end - start).days > 366:
                raise AttributionInputError(
                    "performance range is invalid, future-dated, or exceeds 366 days"
                )
            range_start = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
            range_end = datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc)
            snapshots = SnapshotRepository(settings.data_home).list_range(
                start=range_start,
                end=range_end,
                limit=MAX_ATTRIBUTION_SNAPSHOTS,
                max_bytes=MAX_HISTORY_TOTAL_BYTES,
            )
            if any(item.as_of.astimezone(timezone.utc) > frozen_now for item in snapshots):
                raise FutureSnapshotError("selected snapshot is later than trusted analysis time")
            price_provider = None
            trade_history = None
            trade_unavailable_reason = "audited normalized trade history source is unavailable"
            skill_dir = discover_futuapi_skill()
            if skill_dir is not None and snapshots:
                from .price_history import FutuAdjustedPriceProvider

                adapter = FutuAdapter(skill_dir)
                price_provider = FutuAdjustedPriceProvider(adapter)
                try:
                    aliaser = AccountAliasStore(settings.data_home).load()
                    trade_history = FutuTradeHistoryService(adapter, aliaser=aliaser).review(
                        start=start,
                        end=end,
                        trd_env=snapshots[-1].trd_env,
                        portfolio_snapshot=snapshots[-1],
                    )
                    trade_unavailable_reason = None
                except TimeoutError:
                    trade_unavailable_reason = "TRADE_SOURCE_TIMEOUT"
                except HistoryBudgetLimit:
                    trade_unavailable_reason = "TRADE_SOURCE_BUDGET_EXCEEDED"
                except FutuReadError:
                    trade_unavailable_reason = "TRADE_SOURCE_READ_FAILED"
                except TradeReviewInputError:
                    trade_unavailable_reason = "TRADE_SOURCE_CONTRACT_INVALID"
                except AccountAliasError:
                    trade_unavailable_reason = "TRADE_ALIAS_AUTHORITY_UNAVAILABLE"
            report = analyze_performance(
                snapshots,
                start=start,
                end=end,
                flow_evidence=ExternalFlowEvidence.unavailable(
                    "authoritative deposits, withdrawals and transfers are not configured"
                ),
                price_provider=price_provider,
                trade_history=trade_history,
                trade_unavailable_reason=trade_unavailable_reason,
                analysis_now=frozen_now,
            )
            payload = report.to_dict()
            from .schema import validate_performance_document

            validate_performance_document(payload, source_snapshots=snapshots)
            rendered = json.dumps(payload, ensure_ascii=False, indent=2)
            if args.output and not args.no_save:
                json_content = rendered + "\n"
                json_digest = hashlib.sha256(json_content.encode("utf-8")).hexdigest()
                resolve_output_path(
                    settings.data_home, Path(args.output) / "performance_attribution.json"
                )
                artifact_files = {
                    "performance_attribution.json": json_content,
                    "performance_attribution.md": report.to_markdown(
                        json_artifact="performance_attribution.json",
                        json_digest=json_digest,
                    ),
                }
                SnapshotStore(settings.data_home).save_bundle(
                    args.output,
                    {
                        **artifact_files,
                        "manifest.json": bundle_manifest(
                            source_snapshot_id=report.source_snapshot_ids[-1],
                            as_of=payload["as_of"],
                            report_id=report.attribution_id,
                            files=artifact_files,
                        ),
                    },
                )
            print(rendered if args.as_json else report.to_human())
            return 0

        if args.command == "options":
            source_snapshot = _load_selected_snapshot(
                settings.data_home, latest=args.latest, relative_path=args.snapshot
            )
            if source_snapshot.as_of > frozen_now:
                raise OptionRiskInputError("FUTURE_SNAPSHOT")
            option_provider = None
            metadata_provider = None
            skill_dir = discover_futuapi_skill()
            if skill_dir is not None:
                metadata_provider = FutuOptionMetadataProvider(
                    host=settings.opend_host, port=settings.opend_port
                )
                if validate_option_quote_capability(skill_dir).valid:
                    option_provider = FutuOptionQuoteProvider(FutuAdapter(skill_dir))
            option_report = analyze_option_risk(
                source_snapshot,
                quote_provider=option_provider,
                metadata_provider=metadata_provider,
                analysis_now=frozen_now,
            )
            payload = option_report.to_dict()
            validate_option_risk_document(payload)
            rendered = json.dumps(payload, ensure_ascii=False, indent=2)
            if args.output and not args.no_save:
                output = Path(args.output)
                resolve_output_path(settings.data_home, output / "option_risk.json")
                artifact_files = {
                    "portfolio_snapshot.json": json.dumps(
                        source_snapshot.to_dict(), ensure_ascii=False, indent=2
                    )
                    + "\n",
                    "option_risk.json": rendered + "\n",
                    "option_risk.md": option_report.to_markdown(),
                }
                SnapshotStore(settings.data_home).save_bundle(
                    output,
                    {
                        **artifact_files,
                        "manifest.json": bundle_manifest(
                            source_snapshot_id=source_snapshot.snapshot_id,
                            as_of=source_snapshot.to_dict()["as_of"],
                            report_id=option_report.to_dict()["option_risk_id"],
                            files=artifact_files,
                        ),
                    },
                )
            print(
                rendered if args.as_json else option_report.to_markdown(),
                end="\n" if args.as_json else "",
            )
            return 0

        if args.command == "technical":
            source_snapshot = _load_selected_snapshot(
                settings.data_home, latest=args.latest, relative_path=args.snapshot
            )
            if source_snapshot.as_of > frozen_now:
                raise FutureSnapshotError("selected snapshot is later than trusted analysis time")
            risk_document = None
            if args.risk_report:
                _relative, raw = SnapshotRepository(settings.data_home).read_private_bytes(
                    args.risk_report
                )
                try:
                    risk_document = json.loads(
                        raw,
                        object_pairs_hook=SnapshotRepository._unique_object,
                    )
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
                    raise TechnicalInputError("risk report JSON is invalid") from exc
                if not isinstance(risk_document, dict):
                    raise TechnicalInputError("risk report must be a JSON object")
                try:
                    validate_risk_document(risk_document)
                except (JsonSchemaValidationError, ReportSemanticError) as exc:
                    raise TechnicalInputError("risk report is invalid") from exc
            provider = None
            skill_dir = discover_futuapi_skill()
            if skill_dir is not None:
                provider = FutuTechnicalHistoryProvider(FutuAdapter(skill_dir))
            report = analyze_technical(
                source_snapshot,
                provider=provider,
                risk_document=risk_document,
                explicit_symbols=tuple(args.symbol),
            )
            payload = report.to_dict()
            validate_technical_document(payload)
            rendered = json.dumps(payload, ensure_ascii=False, indent=2)
            if args.output and not args.no_save:
                output = Path(args.output)
                resolve_output_path(settings.data_home, output / "technical_report.json")
                artifacts = {
                    "portfolio_snapshot.json": json.dumps(
                        source_snapshot.to_dict(), ensure_ascii=False, indent=2
                    )
                    + "\n",
                    "technical_report.json": rendered + "\n",
                    "technical_report.md": report.to_markdown(),
                }
                SnapshotStore(settings.data_home).save_bundle(
                    output,
                    {
                        **artifacts,
                        "manifest.json": bundle_manifest(
                            source_snapshot_id=source_snapshot.snapshot_id,
                            as_of=payload["as_of"],
                            report_id=payload["technical_report_id"],
                            files=artifacts,
                        ),
                    },
                )
            print(rendered if args.as_json else report.to_markdown(), end="\n" if args.as_json else "")
            return 0

        if args.command in {"risk", "analyze"}:
            source_snapshot = _load_selected_snapshot(
                settings.data_home, latest=args.latest, relative_path=args.snapshot
            )
            if args.command == "analyze" and source_snapshot.as_of > frozen_now:
                raise OptionRiskInputError("FUTURE_SNAPSHOT")
            history_provider = None
            option_provider = None
            metadata_provider = None
            skill_dir = discover_futuapi_skill()
            if skill_dir is not None:
                from .price_history import FutuAdjustedPriceProvider

                adapter = FutuAdapter(skill_dir)
                history_provider = FutuAdjustedPriceProvider(adapter)
                metadata_provider = FutuOptionMetadataProvider(
                    host=settings.opend_host, port=settings.opend_port
                )
                if validate_option_quote_capability(skill_dir).valid:
                    option_provider = FutuOptionQuoteProvider(adapter)
            risk = analyze_risk(
                source_snapshot,
                lookback=args.lookback,
                price_provider=history_provider,
                policy=load_portfolio_policy(settings.data_home),
            )
            validate_risk_document(risk.to_dict())
            if args.command == "risk":
                payload = risk.to_dict()
                rendered = json.dumps(payload, ensure_ascii=False, indent=2)
                if args.output and not args.no_save:
                    SnapshotStore(settings.data_home).save(args.output, rendered + "\n")
                if args.as_json:
                    print(rendered)
                else:
                    print(_human_risk_report(payload))
                return 0
            try:
                lifecycle_repository = LifecycleRepository(settings.data_home)
                try:
                    lifecycle_history = SnapshotRepository(settings.data_home).list(
                        end=source_snapshot.as_of,
                        limit=MAX_HISTORY_SNAPSHOTS,
                        max_total_bytes=MAX_HISTORY_TOTAL_BYTES,
                    )
                    lifecycle_history_issue = None
                except SnapshotRepositoryError as exc:
                    lifecycle_history = ()
                    lifecycle_history_issue = {
                        "code": "HISTORY_INVALID",
                        "detail": terminal_text(exc),
                    }
                lifecycle_report = analyze_lifecycle(
                    source_snapshot,
                    lifecycle_repository,
                    history=lifecycle_history,
                    history_issue=lifecycle_history_issue,
                )
            except (LifecycleConfigError, LifecycleInputError):
                lifecycle_report = unavailable_lifecycle_report(
                    source_snapshot, "lifecycle configuration unavailable or invalid"
                )
            validate_lifecycle_document(lifecycle_report.to_dict())
            option_report = analyze_option_risk(
                source_snapshot,
                quote_provider=option_provider,
                metadata_provider=metadata_provider,
                analysis_now=frozen_now,
            )
            validate_option_risk_document(option_report.to_dict())
            report = build_portfolio_report(
                source_snapshot, risk, lifecycle_report, options=option_report
            )
            payload = report.to_dict()
            validate_portfolio_report_document(payload)
            rendered = json.dumps(payload, ensure_ascii=False, indent=2)
            if args.output and not args.no_save:
                output = Path(args.output)
                # Validate before writing either sibling so unsafe paths are side-effect free.
                resolve_output_path(settings.data_home, output / "portfolio_report.json")
                artifact_files = {
                    "portfolio_snapshot.json": json.dumps(
                        source_snapshot.to_dict(), ensure_ascii=False, indent=2
                    )
                    + "\n",
                    "risk_report.json": json.dumps(risk.to_dict(), ensure_ascii=False, indent=2)
                    + "\n",
                    "lifecycle_report.json": json.dumps(
                        lifecycle_report.to_dict(), ensure_ascii=False, indent=2
                    )
                    + "\n",
                    "lifecycle_report.md": lifecycle_report.to_markdown(
                        json_artifact="lifecycle_report.json"
                    ),
                    "option_risk.json": json.dumps(
                        option_report.to_dict(), ensure_ascii=False, indent=2
                    )
                    + "\n",
                    "option_risk.md": option_report.to_markdown(),
                    "portfolio_report.json": rendered + "\n",
                    "portfolio_report.md": report.to_markdown(
                        json_artifact="portfolio_report.json"
                    ),
                }
                SnapshotStore(settings.data_home).save_bundle(
                    output,
                    {
                        **artifact_files,
                        "manifest.json": bundle_manifest(
                            source_snapshot_id=source_snapshot.snapshot_id,
                            as_of=source_snapshot.to_dict()["as_of"],
                            report_id=payload["portfolio_report_id"],
                            files=artifact_files,
                        ),
                    },
                )
            print(
                rendered if args.as_json else report.to_markdown(), end="\n" if args.as_json else ""
            )
            return 0

        if args.command == "simulate":
            source_snapshot = _load_selected_snapshot(
                settings.data_home, latest=args.latest, relative_path=args.snapshot
            )
            request, request_payload = load_rebalance_request(settings.data_home, args.request)
            result = simulate_rebalance(source_snapshot, request)
            payload = result.to_dict()
            validate_rebalance_result_document(payload)
            rendered = json.dumps(payload, ensure_ascii=False, indent=2)
            if args.output and not args.no_save:
                result_bytes = (rendered + "\n").encode("utf-8")
                result_digest = hashlib.sha256(result_bytes).hexdigest()
                resolve_output_path(settings.data_home, Path(args.output) / "result.json")
                artifact_files = {
                    "request.json": json.dumps(
                        request_payload, ensure_ascii=False, indent=2, sort_keys=True
                    )
                    + "\n",
                    "result.json": rendered + "\n",
                    "summary.md": result.to_markdown(
                        json_artifact="result.json", json_digest=result_digest
                    ),
                }
                SnapshotStore(settings.data_home).save_bundle(
                    args.output,
                    {
                        **artifact_files,
                        "manifest.json": bundle_manifest(
                            source_snapshot_id=source_snapshot.snapshot_id,
                            as_of=source_snapshot.to_dict()["as_of"],
                            report_id=result.simulation_id,
                            files=artifact_files,
                        ),
                    },
                )
            print(rendered if args.as_json else result.to_human())
            return 0 if result.feasible else 1

        if args.command == "trades":
            try:
                start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
            except ValueError:
                raise TradeReviewInputError("trade review dates must use YYYY-MM-DD") from None
            if start > end or end > date.today() or (end - start).days > 90:
                raise TradeReviewInputError(
                    "trade review range is outside the bounded 90-day policy"
                )
            skill_dir = discover_futuapi_skill()
            if skill_dir is None:
                raise ConfigError("audited-compatible futuapi Skill is unavailable")
            should_save = bool(args.output and not args.no_save)
            aliaser = (
                AccountAliasStore(settings.data_home).load_or_create()
                if should_save
                else AccountAliaser.ephemeral()
            )
            nav_snapshot = None
            if args.latest:
                nav_snapshot = _load_selected_snapshot(
                    settings.data_home, latest=True, relative_path=None
                )
            elif args.snapshot:
                nav_snapshot = _load_selected_snapshot(
                    settings.data_home, latest=False, relative_path=args.snapshot
                )
            if should_save and nav_snapshot is None:
                raise TradeReviewInputError(
                    "saved multi-file trade review requires a source portfolio snapshot"
                )
            if nav_snapshot is not None and nav_snapshot.trd_env != args.trd_env:
                raise TradeReviewInputError(
                    "turnover snapshot environment must match the trade review environment"
                )
            review = FutuTradeHistoryService(FutuAdapter(skill_dir), aliaser=aliaser).review(
                start=start,
                end=end,
                trd_env=args.trd_env,
                policy=load_trade_policy(settings.data_home),
                portfolio_snapshot=nav_snapshot,
            )
            payload = review.to_dict()
            from .schema import validate_trade_review_document

            validate_trade_review_document(payload)
            rendered = json.dumps(payload, ensure_ascii=False, indent=2)
            if should_save:
                review_bytes = (rendered + "\n").encode("utf-8")
                review_digest = hashlib.sha256(review_bytes).hexdigest()
                resolve_output_path(settings.data_home, Path(args.output) / "trade_review.json")
                artifact_files = {
                    "normalized_trades.json": json.dumps(
                        {
                            "normalized_orders": payload["normalized_orders"],
                            "normalized_fills": payload["normalized_fills"],
                            "order_groups": payload["order_groups"],
                            "round_trips": payload["round_trips"],
                            "open_lots": payload["open_lots"],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    "trade_metrics.json": json.dumps(
                        payload["metrics"], ensure_ascii=False, indent=2
                    )
                    + "\n",
                    "trade_review.json": rendered + "\n",
                    "trade_review.md": review.to_markdown(
                        json_artifact="trade_review.json", json_digest=review_digest
                    ),
                }
                SnapshotStore(settings.data_home).save_bundle(
                    args.output,
                    {
                        **artifact_files,
                        "manifest.json": bundle_manifest(
                            source_snapshot_id=nav_snapshot.snapshot_id,
                            as_of=nav_snapshot.to_dict()["as_of"],
                            report_id=review.review_id,
                            files=artifact_files,
                        ),
                    },
                )
            print(rendered if args.as_json else review.to_human())
            return 0

        if args.output is not None:
            raise ValueError(
                "custom snapshot output is forbidden; use --save for canonical snapshot history"
            )
        should_save = not args.no_save and args.save
        if should_save:
            ensure_private_home(settings.data_home, create=True)
        snapshot = generate_snapshot(
            settings,
            trd_env=args.trd_env,
            base_currency=args.base_currency,
            persist_aliases=should_save,
        )
        payload = snapshot.to_dict()
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if should_save:
            destination = SnapshotRepository(settings.data_home).save(snapshot)
            if args.verbose:
                print(f"saved snapshot: {destination}", file=sys.stderr)
        if args.as_json:
            print(rendered)
        else:
            print(
                f"Portfolio snapshot {snapshot.snapshot_id}\n"
                f"As of: {snapshot.as_of.isoformat()}\n"
                f"Accounts: {len(snapshot.accounts)}\n"
                f"NAV ({snapshot.base_currency}): {snapshot.nav_base}\n"
                f"Data quality: {snapshot.data_quality.status}"
            )
        return 0
    except SettingsError as exc:
        return _emit_error(args, "CONFIG_ERROR", str(exc))
    except PrivateHomeError as exc:
        return _emit_error(args, "CONFIG_ERROR", str(exc))
    except InvalidSnapshot as exc:
        return _emit_error(args, "INVALID_SNAPSHOT", str(exc))
    except UnsupportedRiskError as exc:
        return _emit_error(args, "UNSUPPORTED_PORTFOLIO", str(exc))
    except RiskInputError as exc:
        return _emit_error(args, "INVALID_SNAPSHOT", str(exc))
    except OptionRiskInputError as exc:
        if str(exc) == "FUTURE_SNAPSHOT":
            return _emit_error(args, "FUTURE_SNAPSHOT", "selected snapshot is future-dated")
        return _emit_error(args, "INVALID_SNAPSHOT", str(exc))
    except SnapshotAlreadyExists as exc:
        return _emit_error(args, "SNAPSHOT_EXISTS", str(exc))
    except ConfigError as exc:
        return _emit_error(args, "CONFIG_ERROR", str(exc))
    except AccountAliasError as exc:
        return _emit_error(args, "CONFIG_ERROR", str(exc))
    except PolicyConfigError as exc:
        return _emit_error(args, "CONFIG_ERROR", str(exc))
    except RiskConfigError as exc:
        return _emit_error(args, "CONFIG_ERROR", str(exc))
    except TechnicalInputError as exc:
        return _emit_error(args, "INVALID_TECHNICAL", str(exc))
    except RebalanceError as exc:
        return _emit_error(args, "INVALID_REQUEST", str(exc))
    except TradePolicyConfigError as exc:
        return _emit_error(args, "CONFIG_ERROR", str(exc))
    except TradeReviewInputError as exc:
        return _emit_error(args, "INVALID_TRADE_HISTORY", str(exc))
    except LifecycleConfigError as exc:
        return _emit_error(args, "CONFIG_ERROR", str(exc))
    except LifecycleInputError as exc:
        return _emit_error(args, "INVALID_LIFECYCLE", str(exc))
    except JournalError as exc:
        return _emit_error(args, "INVALID_JOURNAL", str(exc))
    except FutureSnapshotError:
        return _emit_error(args, "FUTURE_SNAPSHOT", "selected snapshot is future-dated")
    except AttributionInputError as exc:
        return _emit_error(args, "INVALID_PERFORMANCE", str(exc))
    except SnapshotRepositoryError as exc:
        return _emit_error(args, "INVALID_SNAPSHOT", str(exc))
    except UnsafeAggregationError as exc:
        return _emit_error(args, "UNSAFE_AGGREGATION", str(exc))
    except (FutuReadError, SourceDataError) as exc:
        return _emit_error(args, "SOURCE_UNAVAILABLE", str(exc))
    except ValueError as exc:
        return _emit_error(args, "INVALID_OUTPUT", str(exc))
    except Exception as exc:
        if getattr(args, "verbose", False):
            print(f"snapshot internal failure type: {type(exc).__name__}", file=sys.stderr)
        return _emit_error(
            args,
            "INTERNAL_ERROR",
            "snapshot failed without exposing source details",
        )


def _emit_error(args: argparse.Namespace, code: str, message: str) -> int:
    payload = {"ok": False, "error": {"code": code, "message": message}}
    if getattr(args, "as_json", False):
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"Error [{code}]: {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
