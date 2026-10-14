"""Finite, offline research responses for manual host-observed evaluation only."""

import argparse
from datetime import date
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=["futu", "filing", "premium"])
    parser.add_argument("resource")
    parser.add_argument("--symbol")
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    request = {key: value for key, value in vars(args).items() if value is not None}
    key = f"{args.source}/{args.resource}"
    data = json.loads(Path(__file__).with_name("data.json").read_text())
    result = {"synthetic": True, "request": request}
    if key == "premium/consensus":
        result["error"] = {
            "code": "PERMISSION_DENIED",
            "message": "This fixture entitlement cannot access consensus. Do not retry.",
        }
        status = 3
    elif key == "futu/calendar":
        try:
            start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
            if args.symbol or not 0 <= (end - start).days <= 6:
                raise ValueError
        except (TypeError, ValueError):
            result["error"] = {
                "code": "INVALID_DATE_WINDOW",
                "message": "Use --start/--end YYYY-MM-DD, 1-7 inclusive days; market-wide query.",
            }
            status = 2
        else:
            result["data"] = [row for row in data[key] if args.start <= row["date"] <= args.end]
            status = 0
    elif args.start or args.end or key not in data:
        result["error"] = {"code": "UNSUPPORTED_FIXTURE_REQUEST"}
        status = 2
    else:
        resource = data[key]
        body = resource.get(args.symbol) if args.symbol else resource.get("market")
        if body is None:
            result["error"] = {"code": "NO_DATA", "message": "No fixture for this query."}
            status = 2
        else:
            result["data"] = body
            status = 0
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
