import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys

from .calendar import default_report_period, filing_deadline
from .evidence import ResearchError, available_by, fields, json_value, require, timestamp
from .reader import EdgarReader
from .rendering import render_institutional
from .signals import analyze, periods_ending
from .transport import Budget, sdk_session
from .universe import Universe


def _json_file(path, limit=200 * 1024 * 1024):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, "INPUT_TOO_LARGE")
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise ResearchError("INVALID_JSON") from None


def parser():
    root = argparse.ArgumentParser(description="Read-only SEC research; no brokerage commands.")
    commands = root.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-universe")
    validate.add_argument("--managers", required=True)
    for name in ("filing-index", "holdings-13f", "filing-content", "financial-facts",
                 "collect", "analyze"):
        command = commands.add_parser(name)
        command.add_argument("--cutoff", default=None, help="Timezone-qualified knowledge cutoff")
        command.add_argument("--output", help="Explicit new output file; never overwrite")
        command.add_argument("--cache-mode", choices=("off", "prefer", "refresh_all"), default="off",
                             help="Explicit opt-in local public-evidence cache")
        if name in {"collect", "analyze"}:
            command.add_argument("--managers", required=True)
            command.add_argument("--target-period")
            command.add_argument("--quarters", type=int, default=4)
        else:
            command.add_argument("--cik", required=True)
        if name != "analyze":
            command.add_argument("--seconds", type=float, default=600)
            command.add_argument("--max-bytes", type=int, default=200 * 1024 * 1024)
        if name in {"filing-index", "collect"}:
            command.add_argument("--max-filings", type=int, default=100)
        if name == "filing-index":
            command.add_argument("--form", action="append", required=True)
            command.add_argument("--start", required=True)
        if name in {"holdings-13f", "filing-content", "financial-facts"}:
            command.add_argument("--accession", required=True,
                                 action="append" if name == "financial-facts" else "store")
        if name == "filing-content":
            command.add_argument("--section", default="full_text")
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--limit", type=int, default=12000)
        if name == "financial-facts":
            command.add_argument("--concept", action="append", required=True)
            command.add_argument("--limit", type=int, default=2000)
        if name == "analyze":
            command.add_argument("--bundle", required=True)
            command.add_argument("--enrichment")
            command.add_argument("--json", action="store_true")
    return root


def collect(reader, universe, target, cutoff, quarters, limit):
    periods = periods_ending(target, quarters)
    documents, diagnostics = [], []
    for manager in universe.managers:
        collected, complete = [], False
        try:
            index = reader.index(manager.cik, ["13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A"],
                                 periods[0], cutoff, limit=limit)
            complete = index["complete"]
            for source in index["filings"]:
                if source.get("report_period") and source["report_period"] not in periods:
                    continue
                if source["form"] not in {"13F-HR", "13F-HR/A"}:
                    complete = False
                    diagnostics.append({"manager_cik": manager.cik, "code": "NOTICE_REPORT",
                                        "accession": source["accession"]})
                    continue
                try:
                    document = reader.holdings(manager.cik, source["accession"], cutoff)
                    if document["report_period"] in periods:
                        collected.append(document)
                except Exception as error:
                    complete = False
                    diagnostics.append({"manager_cik": manager.cik,
                                        "accession": source["accession"],
                                        "code": _error_code(error, reader.budget)})
        except Exception as error:
            diagnostics.append({"manager_cik": manager.cik, "code": _error_code(error, reader.budget)})
        for document in collected:
            document["discovery_complete"] = complete
        documents.extend(collected)
    return {"schema_version": 1, "data_kind": "sec-sdk", "knowledge_cutoff": cutoff,
            "universe_version": universe.version, "target_period": target,
            "filings": documents, "diagnostics": diagnostics,
            "downloaded_bytes": reader.budget.used_bytes}


def _error_code(error, budget=None):
    return (budget.blocked if budget is not None and budget.blocked else
            error.code if isinstance(error, ResearchError) else "SDK_READ_FAILED")


def _live_universe(universe):
    if universe is not None:
        require(not any("fixture://" in (source or "").lower()
                        for manager in universe.managers
                        for source in (manager.relationship_source, manager.strategy_source)),
                "SYNTHETIC_EVIDENCE_IN_LIVE_RUN")


def _run_sdk(args, cutoff, universe):
    _live_universe(universe)
    require(0 < args.seconds <= 3600 and 0 < args.max_bytes <= 1024 * 1024 * 1024,
            "INVALID_BUDGET")
    budget = Budget(args.max_bytes, args.seconds)
    previous_handler = None
    if hasattr(signal, "SIGALRM"):
        def expired(_signum, _frame):
            budget.blocked = "TIME_BUDGET"
            raise ResearchError("TIME_BUDGET")
        previous_handler = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, args.seconds)
    try:
        with sdk_session(budget) as sdk:
            reader = EdgarReader(sdk, budget)
            if getattr(args, "cache_mode", "off") != "off":
                from .cache import BridgeError, CacheBridge, CachedReader
                try:
                    bridge = CacheBridge()
                    reader = CachedReader(reader, bridge, force=args.cache_mode == "refresh_all",
                                          strict_index=args.command == "collect")
                    args._cache_reader = reader
                    def shared_limit():
                        try:
                            bridge.rate_limit()
                        except (BridgeError, OSError):
                            reader.diagnostics.append("SHARED_RATE_LIMIT_UNAVAILABLE")
                    budget.shared_rate_limiter = shared_limit
                except BridgeError:
                    args._cache_unavailable = True
            if args.command == "collect":
                return collect(reader, universe, args.target_period or default_report_period(cutoff),
                               cutoff, args.quarters, args.max_filings)
            if args.command == "filing-index":
                return reader.index(args.cik, args.form, args.start, cutoff, limit=args.max_filings)
            if args.command == "holdings-13f":
                return reader.holdings(args.cik, args.accession, cutoff)
            if args.command == "filing-content":
                return reader.content(args.cik, args.accession, cutoff, section=args.section,
                                      offset=args.offset, limit=args.limit)
            return reader.facts(args.cik, args.accession, args.concept, cutoff, limit=args.limit)
    finally:
        if previous_handler is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)


def execute(args):
    universe = Universe.load(args.managers) if hasattr(args, "managers") else None
    if args.command == "validate-universe":
        return universe.public_summary()
    cutoff = args.cutoff or datetime.now(timezone.utc).isoformat()
    timestamp(cutoff)
    if args.command != "analyze":
        return _run_sdk(args, cutoff, universe)
    bundle = _json_file(args.bundle)
    # Accept an unmodified collect receipt as well as its data object.
    if isinstance(bundle, dict) and bundle.get("success") is True and "data" in bundle:
        bundle = bundle["data"]
    fields(bundle, {"schema_version", "data_kind", "knowledge_cutoff", "universe_version",
                    "target_period", "filings", "enrichment", "diagnostics", "downloaded_bytes"},
           {"schema_version", "data_kind", "knowledge_cutoff", "filings"})
    require(type(bundle["schema_version"]) is int and bundle["schema_version"] == 1,
            "UNSUPPORTED_SCHEMA")
    require(bundle["data_kind"] in {"synthetic", "sec-sdk"}, "UNKNOWN_DATA_KIND")
    cutoff = args.cutoff or bundle["knowledge_cutoff"]
    require(cutoff == bundle["knowledge_cutoff"], "BUNDLE_CUTOFF_MISMATCH",
            "Reuse at the collected cutoff or refresh the filing index")
    require(bundle.get("universe_version", universe.version) == universe.version,
            "UNIVERSE_VERSION_MISMATCH")
    enrichment = _json_file(args.enrichment) if args.enrichment else bundle.get("enrichment", {})
    if bundle["data_kind"] != "synthetic":
        _live_universe(universe)
        require("fixture://" not in json.dumps(enrichment), "SYNTHETIC_EVIDENCE_IN_LIVE_RUN")
    target = args.target_period or bundle.get("target_period") or default_report_period(cutoff)
    result = analyze(universe, bundle["filings"], target, cutoff, quarters=args.quarters,
                     enrichment=enrichment)
    result["data_kind"] = bundle["data_kind"]
    result["preliminary"] = not available_by(filing_deadline(target).isoformat(), cutoff)
    result["diagnostics"].extend(bundle.get("diagnostics", []))
    if result["diagnostics"]:
        result["status"] = "PARTIAL"
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args)
        receipt = {"success": True, "data": json_value(result)}
        cached_reader = getattr(args, "_cache_reader", None)
        if cached_reader is not None:
            def evidence_ref(item):
                if item["record"]["metadata"].get("query", {}).get("dataset") == "sec.index":
                    return {"scope": "public", "query_key": item["record"]["query_key"],
                            "generation": item["record"]["generation"], "epoch": item["record"]["query_epoch"]}
                return item["ref"]
            receipt["cache"] = {"refs": [evidence_ref(item) for item in cached_reader.receipts if item.get("ref")],
                                "diagnostics": cached_reader.diagnostics}
        elif getattr(args, "_cache_unavailable", False):
            receipt["cache"] = {"refs": [], "diagnostics": ["UNSUPPORTED_BACKEND"]}
        output = (render_institutional(result) if args.command == "analyze" and not args.json
                  else json.dumps(receipt,
                                  ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        if getattr(args, "output", None):
            # The user explicitly chose this destination. Existing content is never replaced.
            with Path(args.output).open("x", encoding="utf-8") as stream:
                stream.write(output)
        else:
            sys.stdout.write(output)
        return 0
    except (ResearchError, OSError, ValueError, TypeError) as error:
        code = error.code if isinstance(error, ResearchError) else type(error).__name__
        print(json.dumps({"success": False, "error": {"code": code,
              "message": error.message if isinstance(error, ResearchError) else
              "Input, filesystem or SDK operation failed; no account action was performed."}},
              ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"success": False, "error": {"code": "SDK_READ_FAILED",
              "message": "Public SDK read failed; original context remains unavailable."}}))
        return 2
