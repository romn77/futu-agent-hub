"""Validated public evidence and cited summaries; no brokerage payloads."""

from __future__ import annotations

from urllib.parse import urlsplit

from .types import CacheError, canonical, utc


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
    elif isinstance(value, str) and any(text in value.lower() for text in PRIVATE_TEXT):
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
    reject_private(query)
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
                or parsed.username or parsed.password):
            raise CacheError("INVALID_SOURCE")
        if isinstance(payload, dict) and (payload.get("success") is False or payload.get("ok") is False
                                          or "error" in payload):
            raise CacheError("SOURCE_UNAVAILABLE")
        if type(complete) is not bool:
            raise CacheError("INVALID_INPUT")
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
