"""Dataset-specific receipt shapes; arbitrary objects are not public evidence."""

import re
from datetime import date
from urllib.parse import urlsplit

from .types import CacheError, utc


SEC_META = {"manager_cik", "accession", "form", "filed", "source_url", "retrieved_at"}


def _fields(value, allowed, required):
    if not isinstance(value, dict) or not required <= set(value) or set(value) - allowed:
        raise CacheError("INVALID_EVIDENCE")


def _sec_meta(value, subject):
    if (not re.fullmatch(r"[0-9]{10}", str(value.get("manager_cik", "")))
            or value["manager_cik"] != subject or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", str(value.get("accession", "")))
            or urlsplit(value.get("source_url", "")).hostname not in {"www.sec.gov", "data.sec.gov"}):
        raise CacheError("INVALID_EVIDENCE")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value.get("filed", ""))):
        raise CacheError("INVALID_EVIDENCE")
    try:
        date.fromisoformat(value["filed"])
    except ValueError as exc:
        raise CacheError("INVALID_EVIDENCE") from exc
    utc(value["retrieved_at"])


def validate_evidence(query, payload, source_url):
    dataset = query["dataset"]
    if dataset == "summary":
        raise CacheError("INVALID_SUMMARY")
    if dataset.startswith("sec.") and query["provider"] != "SEC":
        raise CacheError("INVALID_EVIDENCE")
    if isinstance(payload, dict) and any(key in payload and type(payload[key]) is not bool
                                         for key in ("complete", "truncated")):
        raise CacheError("INVALID_EVIDENCE")
    if dataset == "sec.content":
        _fields(payload, SEC_META | {"section", "text"}, SEC_META | {"section", "text"})
        _sec_meta(payload, query["subject"])
        if (not isinstance(payload["text"], str) or payload["source_url"] != source_url
                or payload["accession"] != query["parameters"].get("accession")
                or payload["section"] != query["parameters"].get("section", "full_text")):
            raise CacheError("INVALID_EVIDENCE")
    elif dataset == "sec.index":
        _fields(payload, {"filings", "complete", "issues", "time_precision", "knowledge_cutoff"},
                {"filings", "complete", "knowledge_cutoff"})
        if type(payload["complete"]) is not bool or not isinstance(payload["filings"], list):
            raise CacheError("INVALID_EVIDENCE")
        utc(payload["knowledge_cutoff"])
        for row in payload["filings"]:
            _fields(row, SEC_META | {"report_period"}, SEC_META)
            _sec_meta(row, query["subject"])
    elif dataset == "sec.facts":
        _fields(payload, {"cik", "knowledge_cutoff", "facts", "truncated", "coverage"},
                {"cik", "knowledge_cutoff", "facts", "truncated"})
        if payload["cik"] != query["subject"] or type(payload["truncated"]) is not bool or not isinstance(payload["facts"], list):
            raise CacheError("INVALID_EVIDENCE")
        for row in payload["facts"]:
            fields = {"concept", "value", "unit", "start", "end", "duration_days", "period_kind",
                      "basis", "accession", "filed", "derived"}
            _fields(row, fields, fields)
            if row["accession"] not in query["parameters"].get("accessions", []):
                raise CacheError("INVALID_EVIDENCE")
    elif dataset == "sec.holdings":
        allowed = SEC_META | {"report_period", "report_type", "amendment_type", "amendment_number",
                              "value_unit", "unit_verified", "unit_resolution", "summary_count", "summary_value",
                              "other_included_managers", "rows", "discovery_complete", "confidential"}
        _fields(payload, allowed, SEC_META | {"rows", "report_period", "discovery_complete"})
        _sec_meta(payload, query["subject"])
        if not isinstance(payload["rows"], list):
            raise CacheError("INVALID_EVIDENCE")
        fields = {"cusip", "issuer", "class_title", "put_call", "quantity_type", "quantity", "reported_value",
                  "instrument_type", "investment_discretion", "other_managers", "row_id", "voting_authority"}
        for row in payload["rows"]:
            _fields(row, fields, {"cusip", "reported_value"})
    elif dataset == "market.ohlcv":
        _fields(payload, {"start", "end", "response", "revision_checked_at"},
                {"start", "end", "response", "revision_checked_at"})
        from .market import _valid_response
        if not _valid_response(payload["response"], query["subject"], query["parameters"].get("interval")):
            raise CacheError("INVALID_EVIDENCE")
    else:
        # Host tools must project their verified public receipts to observations.
        # An arbitrary account/report object, even with a claimed URL, is rejected.
        _fields(payload, {"observations", "complete", "truncated"}, {"observations"})
        if not isinstance(payload["observations"], list):
            raise CacheError("INVALID_EVIDENCE")
        for observation in payload["observations"]:
            _fields(observation, {"text", "value", "unit", "period", "source_locator"}, {"text"})
            if not isinstance(observation["text"], str):
                raise CacheError("INVALID_EVIDENCE")
