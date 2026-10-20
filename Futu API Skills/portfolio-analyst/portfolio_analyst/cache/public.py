"""Validated public evidence and cited summaries; no brokerage payloads."""

from __future__ import annotations

from urllib.parse import urlsplit, parse_qsl
import re

from .types import CacheError, canonical, utc
from .evidence_shape import validate_evidence


TTL = {"sec.index": 3600, "news": 900, "estimates": 86400, "valuation": 86400,
       "market.ohlcv": 86400, "sec.content": None, "sec.facts": None,
       "sec.holdings": None, "disclosure": None, "summary": None}
PRIVATE_KEYS = {"acc_id", "account_id", "account_ids", "account_ref", "private_id",
                "source_snapshot_id", "snapshot_id", "nav_base", "total_assets",
                "portfolio_snapshot", "account_scope", "portfolio_implications",
                "cash_balance_base", "personal_rules", "credentials", "password", "api_key"}
PRIVATE_TEXT = ("我的账户", "我的仓位", "我的持仓", "账户净值", "my account balance", "my portfolio weight")


def reject_private(value, depth=0):
    if depth > 32:
        raise CacheError("INVALID_INPUT")
    if isinstance(value, dict):
        if any(not isinstance(key, str) or key.lower() in PRIVATE_KEYS for key in value):
            raise CacheError("PRIVATE_CONTENT")
        for child in value.values():
            reject_private(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            reject_private(child, depth + 1)
    elif isinstance(value, str):
        if (any(text in value.lower() for text in PRIVATE_TEXT)
                or re.search(r"\b(?:i|we)\s+(?:hold|own)\s+\d+\s+shares\b|\byour\s+(?:position|holdings)\b", value, re.I)
                or re.search(r"(?:/Users/[^/]+/|/home/[^/]+/)", value)):
            raise CacheError("PRIVATE_CONTENT")


def validate_query(query):
    required = {"provider", "interface", "version", "subject", "dataset", "parameters"}
    if not isinstance(query, dict) or set(query) != required:
        raise CacheError("INVALID_QUERY")
    if any(not isinstance(query[key], str) or not 0 < len(query[key]) <= 512
           for key in required - {"parameters"}):
        raise CacheError("INVALID_QUERY")
    if query["dataset"] not in TTL or not isinstance(query["parameters"], dict):
        raise CacheError("INVALID_QUERY")
    parameter_fields = {
        "sec.index": {"forms", "start", "limit"},
        "sec.content": {"accession", "section"},
        "sec.holdings": {"accession"},
        "sec.facts": {"accessions", "concepts", "limit"},
        "market.ohlcv": {"interval", "rehab", "session", "normalizer"},
    }.get(query["dataset"], {"query", "window", "start", "end", "period", "as_of", "timezone", "limit",
                              "section", "accession", "concepts", "method", "method_version", "question",
                              "universe_version", "config_revision", "enrichment_hash", "target_period", "quarters"})
    if set(query["parameters"]) - parameter_fields:
        raise CacheError("INVALID_QUERY")
    reject_private(query)
    for item in query["parameters"].values():
        values = item if isinstance(item, list) else [item]
        if len(values) > 1000 or any(type(value) not in {str, int, float, bool, type(None)} for value in values):
            raise CacheError("INVALID_QUERY")
    if len(canonical(query)) > 16384:
        raise CacheError("INVALID_QUERY")
    return query


class PublicCache:
    def __init__(self, store):
        if store.scope != "public":
            raise CacheError("SCOPE_CHANGED")
        self.store = store

    def lookup(self, query, *, cutoff=None, historical=False, force=False):
        validate_query(query)
        if type(historical) is not bool or type(force) is not bool:
            raise CacheError("INVALID_INPUT")
        if force:
            return {"status": "BYPASS", "reason": "FORCED_REFRESH"}
        result = self.store.lookup(query, historical=historical)
        if result["status"] in {"HIT", "HIT_PARTIAL"} and cutoff is not None:
            available = result["record"]["metadata"].get("available_at")
            if available is None or utc(available) > utc(cutoff):
                return {"status": "MISS", "reason": "AFTER_CUTOFF"}
        return result

    def publish(self, query, payload, *, source_url, source_as_of=None, available_at=None,
                complete=True, ttl=None, dependencies=(), guards=(), lease=None, semantic=None):
        validate_query(query)
        reject_private(payload)
        parsed = urlsplit(source_url)
        if (parsed.scheme not in {"https", "tool", "fixture"} or not parsed.netloc
                or parsed.username or parsed.password or any(key.lower() in
                   {"token", "api_key", "password", "access_token"} for key, _ in parse_qsl(parsed.query))):
            raise CacheError("INVALID_SOURCE")
        if isinstance(payload, dict) and (payload.get("success") is False or payload.get("ok") is False
                                          or "error" in payload):
            raise CacheError("SOURCE_UNAVAILABLE")
        if type(complete) is not bool:
            raise CacheError("INVALID_INPUT")
        validate_evidence(query, payload, source_url)
        if isinstance(payload, dict) and (payload.get("truncated") is True or payload.get("complete") is False):
            complete = False
        if available_at is not None and utc(available_at) > utc(self.store.clock()):
            raise CacheError("CLOCK_INVALID")
        default_ttl = TTL[query["dataset"]]
        if default_ttl is not None and source_as_of is None:
            raise CacheError("SOURCE_TIME_REQUIRED")
        if ttl is not None and default_ttl is not None:
            ttl = min(ttl, default_ttl)
        else:
            ttl = default_ttl if ttl is None else ttl
        return self.store.put(query, payload, source_as_of=source_as_of, ttl=ttl,
                              kind="public_evidence", availability="COMPLETE" if complete else "PARTIAL",
                              metadata={"query": query, "source_url": source_url,
                                        "available_at": utc(available_at).isoformat() if available_at else None},
                              dependencies=dependencies, guards=guards, lease=lease, semantic=semantic)

    def publish_summary(self, query, payload, *, dependencies, guards=(), lease=None):
        validate_query(query)
        required = {"question", "subject", "period", "facts", "inferences", "scenarios",
                    "supporting_refs", "contrary_refs", "limitations", "follow_up",
                    "method_version", "model", "calculations"}
        if (query["dataset"] != "summary" or not isinstance(payload, dict)
                or set(payload) != required or not dependencies):
            raise CacheError("INVALID_SUMMARY")
        reject_private(payload)
        for field in ("question", "subject", "period", "method_version", "model"):
            if not isinstance(payload[field], str) or not payload[field]:
                raise CacheError("INVALID_SUMMARY")
        for field in required - {"question", "subject", "period", "method_version", "model"}:
            if not isinstance(payload[field], list):
                raise CacheError("INVALID_SUMMARY")
        if payload["subject"] != query["subject"] or not payload["supporting_refs"]:
            raise CacheError("INVALID_SUMMARY")
        if not payload["contrary_refs"] and not payload["limitations"]:
            raise CacheError("INVALID_SUMMARY")
        if any(ref not in dependencies for ref in payload["supporting_refs"] + payload["contrary_refs"]):
            raise CacheError("DEPENDENCY_MISSING")
        return self.store.put(query, payload, kind="public_summary", dependencies=dependencies,
                              guards=guards, lease=lease, metadata={"query": query})
