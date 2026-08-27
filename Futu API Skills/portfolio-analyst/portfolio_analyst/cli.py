"""Unified command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .doctor import run_doctor
from .futu_adapter import FutuReadError
from .persistence import SnapshotAlreadyExists, SnapshotStore
from .repository import SourceDataError
from .service import ConfigError, generate_snapshot
from .settings import Settings, SettingsError, resolve_output_path
from .snapshot import UnsafeAggregationError


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

        snapshot = generate_snapshot(
            settings, trd_env=args.trd_env, base_currency=args.base_currency
        )
        payload = snapshot.to_dict()
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        should_save = not args.no_save and (args.save or args.output is not None)
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
    except SnapshotAlreadyExists as exc:
        return _emit_error(args, "SNAPSHOT_EXISTS", str(exc))
    except ConfigError as exc:
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
