"""Synthetic-only data boundary copied into an isolated model evaluation workspace."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    envelope = json.loads((ROOT / ".fixture/envelope.json").read_text())
    if Path(__file__).name == "current_analysis.py":
        if len(sys.argv) != 1:
            raise ValueError("fixture account is already selected")
        print(json.dumps(envelope))
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["doctor", "snapshot", "risk", "technical", "options", "simulate"]
    )
    parser.add_argument("--snapshot")
    parser.add_argument("--request")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()
    if args.command == "doctor":
        body = {"ok": True, "synthetic_fixture": True, "live_opend": False}
    elif args.command == "simulate":
        from portfolio_analyst.rebalance import request_from_document, simulate_rebalance
        from portfolio_analyst.snapshot_io import snapshot_from_document

        request_path = (ROOT / (args.request or "")).resolve()
        snapshot_path = (ROOT / (args.snapshot or "")).resolve()
        if (
            request_path != ROOT / ".fixture/request.json"
            or snapshot_path != ROOT / ".fixture/snapshot.json"
        ):
            raise ValueError("use the prepared bound fixture paths")
        request = request_from_document(json.loads(request_path.read_text()))
        body = simulate_rebalance(snapshot_from_document(envelope["snapshot"]), request).to_dict()
    else:
        body = envelope[args.command]
    print(json.dumps(body))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError):
        print(json.dumps({"error": {"code": "FIXTURE_COMMAND_INVALID"}}))
        raise SystemExit(2)
