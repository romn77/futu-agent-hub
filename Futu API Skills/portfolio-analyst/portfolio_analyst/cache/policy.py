"""Explicit environment overrides, versioned by validity scope."""

import os

from .types import CacheError, digest


DEFAULTS = {
    "ACCOUNT_TTL_SECONDS": 300, "REPORT_TTL_SECONDS": 300,
    "SEC_INDEX_TTL_SECONDS": 3600, "NEWS_TTL_SECONDS": 900,
    "ESTIMATES_TTL_SECONDS": 86400, "VALUATION_TTL_SECONDS": 86400,
    "OHLCV_TTL_SECONDS": 86400, "PRIVATE_RETENTION_SECONDS": 86400,
    "PUBLIC_MAX_MIB": 512, "PRIVATE_MAX_MIB": 128,
}
DATASET_POLICY = {"sec.index": "SEC_INDEX_TTL_SECONDS", "news": "NEWS_TTL_SECONDS",
                  "estimates": "ESTIMATES_TTL_SECONDS", "valuation": "VALUATION_TTL_SECONDS",
                  "market.ohlcv": "OHLCV_TTL_SECONDS"}


class CachePolicy:
    def __init__(self, *, enabled=True):
        self.values = dict(DEFAULTS)
        if enabled:
            for key in DEFAULTS:
                value = os.environ.get("RESEARCH_CACHE_" + key)
                if value is None:
                    continue
                limit = 4096 if key.endswith("_MIB") else 86400
                if not value.isascii() or not value.isdecimal() or not 0 < int(value) <= limit:
                    raise CacheError("INVALID_POLICY")
                self.values[key] = int(value)

    def version(self, scope):
        keys = (DATASET_POLICY.values() if scope == "public" else
                ("ACCOUNT_TTL_SECONDS", "REPORT_TTL_SECONDS", "PRIVATE_RETENTION_SECONDS"))
        return digest({"schema": 1, "scope": scope, "validity": {key: self.values[key] for key in keys}})

    def ttl(self, dataset):
        key = DATASET_POLICY.get(dataset)
        return self.values[key] if key else None
