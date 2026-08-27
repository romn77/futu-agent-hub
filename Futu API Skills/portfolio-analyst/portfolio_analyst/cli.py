"""Unified command-line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .doctor import run_doctor
from .futu_adapter import FutuAdapter, FutuReadError
from .persistence import SnapshotAlreadyExists, SnapshotStore
from .reporting import build_portfolio_report
from .rendering import terminal_text
from .repository import SourceDataError
from .risk import (
    PolicyConfigError,
    RiskConfigError,
    RiskInputError,
    UnsupportedRiskError,
    analyze_risk,
    load_portfolio_policy,
)
from .schema import validate_portfolio_report_document, validate_risk_document
from .service import ConfigError, generate_snapshot
from .settings import Settings, SettingsError, discover_futuapi_skill, resolve_output_path
from .snapshot import UnsafeAggregationError
from .snapshot_io import InvalidSnapshot, load_latest_snapshot, load_snapshot


class _JsonParseError(ValueError):
    pass


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
    snapshot.add_argument("--output", help="save at a relative path under PORTFOLIO_AGENT_HOME")
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
    return parser


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


def main(argv: Sequence[str] | None = None) -> int:
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
        settings = Settings.from_env()
        if args.command == "doctor":
            report = run_doctor(host=settings.opend_host, port=settings.opend_port)
            rendered = (
                json.dumps(report, ensure_ascii=False, indent=2)
                if args.as_json
                else _human_report(report, args.verbose)
            )
            print(rendered)

            if args.output and not args.no_save:
                destination = resolve_output_path(settings.data_home, Path(args.output))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(rendered + "\n", encoding="utf-8")
            return 0 if report["ok"] else 1

        if args.command in {"risk", "analyze"}:
            source_snapshot = (
                load_latest_snapshot(settings.data_home)
                if args.latest
                else load_snapshot(settings.data_home, args.snapshot)
            )
            history_provider = None
            skill_dir = discover_futuapi_skill()
            if skill_dir is not None:
                from .price_history import FutuAdjustedPriceProvider

                history_provider = FutuAdjustedPriceProvider(FutuAdapter(skill_dir))
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
            report = build_portfolio_report(source_snapshot, risk)
            payload = report.to_dict()
            validate_portfolio_report_document(payload)
            rendered = json.dumps(payload, ensure_ascii=False, indent=2)
            if args.output and not args.no_save:
                output = Path(args.output)
                # Validate before writing either sibling so unsafe paths are side-effect free.
                resolve_output_path(settings.data_home, output / "portfolio_report.json")
                SnapshotStore(settings.data_home).save_bundle(
                    output,
                    {
                        "portfolio_report.json": rendered + "\n",
                        "portfolio_report.md": report.to_markdown(),
                    },
                )
            print(
                rendered if args.as_json else report.to_markdown(), end="\n" if args.as_json else ""
            )
            return 0

        should_save = not args.no_save and (args.save or args.output is not None)
        snapshot = generate_snapshot(
            settings,
            trd_env=args.trd_env,
            base_currency=args.base_currency,
            persist_aliases=should_save,
        )
        payload = snapshot.to_dict()
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if should_save:
            relative_path = args.output or f"snapshots/{snapshot.snapshot_id}.json"
            destination = SnapshotStore(settings.data_home).save(relative_path, rendered + "\n")
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
    except InvalidSnapshot as exc:
        return _emit_error(args, "INVALID_SNAPSHOT", str(exc))
    except UnsupportedRiskError as exc:
        return _emit_error(args, "UNSUPPORTED_PORTFOLIO", str(exc))
    except RiskInputError as exc:
        return _emit_error(args, "INVALID_SNAPSHOT", str(exc))
    except SnapshotAlreadyExists as exc:
        return _emit_error(args, "SNAPSHOT_EXISTS", str(exc))
    except ConfigError as exc:
        return _emit_error(args, "CONFIG_ERROR", str(exc))
    except PolicyConfigError as exc:
        return _emit_error(args, "CONFIG_ERROR", str(exc))
    except RiskConfigError as exc:
        return _emit_error(args, "CONFIG_ERROR", str(exc))
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
