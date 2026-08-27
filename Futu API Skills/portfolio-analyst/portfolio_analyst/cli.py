"""Unified command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .doctor import run_doctor
from .settings import Settings, resolve_output_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portfolio.py", description="Read-only portfolio analyst")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="check Skill, SDK, and OpenD availability")
    doctor.add_argument("--json", action="store_true", dest="as_json", help="print JSON")
    doctor.add_argument("--output", help="save under PORTFOLIO_AGENT_HOME")
    doctor.add_argument("--no-save", action="store_true", help="do not save output")
    doctor.add_argument("--verbose", action="store_true", help="show check details")
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
    args = _parser().parse_args(argv)
    settings = Settings.from_env()
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


if __name__ == "__main__":
    sys.exit(main())
