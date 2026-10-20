"""Reuse audited raw OHLCV across risk and technical providers when explicitly enabled."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .types import CacheError, digest, utc


_ACTIVE = ContextVar("portfolio_market_cache", default=None)


def active_market_cache():
    return _ACTIVE.get()


@contextmanager
def use_market_cache(cache):
    token = _ACTIVE.set(cache)
    try:
        yield cache
    finally:
        _ACTIVE.reset(token)


class _Uncacheable(Exception):
    def __init__(self, payload):
        self.payload = payload


def _valid_response(payload, code, interval):
    if (not isinstance(payload, dict) or payload.get("code") != code
            or payload.get("ktype") != interval or payload.get("source") != "history"
            or not isinstance(payload.get("data"), list) or not payload["data"]):
        return False
    previous = None
    try:
        for row in payload["data"]:
            day = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").date()
            if previous is not None and day <= previous:
                return False
            numbers = {key: Decimal(str(row[key])) for key in ("open", "high", "low", "close", "volume")}
            if any(not n.is_finite() or n < 0 for n in numbers.values()):
                return False
            if not numbers["low"] <= min(numbers["open"], numbers["close"]) <= max(numbers["open"], numbers["close"]) <= numbers["high"]:
                return False
            previous = day
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False
    return True


class MarketCache:
    def __init__(self, store, *, force=False):
        self.store, self.force = store, force
        self.references = []
        self.diagnostics = []
        self.source_calls = 0

    def read_private_json(self, adapter, name, arguments, *, timeout=5):
        if name != "get_kline":
            return adapter.read_private_json(name, arguments, timeout=timeout)
        def arg(flag):
            return arguments[arguments.index(flag) + 1]
        code, interval = arguments[0], arg("--ktype")
        start, end = date.fromisoformat(arg("--start")), date.fromisoformat(arg("--end"))
        from ..repository import _source_version
        query = {"provider": "futu", "interface": "get_kline", "version": _source_version(),
                 "subject": code, "dataset": "market.ohlcv",
                 "parameters": {"interval": interval, "rehab": arg("--rehab"),
                                "session": arg("--session"), "normalizer": "ohlcv-v1"}}

        loaded = []

        def load():
            self.source_calls += 1
            response = adapter.read_private_json(name, arguments, timeout=timeout)
            loaded.append(response)
            if not _valid_response(response, code, interval):
                raise _Uncacheable(response)
            if any(not start <= date.fromisoformat(row["time"][:10]) <= end for row in response["data"]):
                raise _Uncacheable(response)
            return {"start": start.isoformat(), "end": end.isoformat(), "response": response,
                    "revision_checked_at": utc(self.store.clock()).isoformat()}

        def covers(receipt):
            value = receipt["payload"]
            try:
                return (date.fromisoformat(value["start"]) <= start and end <= date.fromisoformat(value["end"])
                        and _valid_response(value["response"], code, interval))
            except (ValueError, TypeError, KeyError):
                return False

        try:
            ttl = 300 if interval == "1w" and end.weekday() != 4 else 86400
            result = self.store.get_or_create(query, load, force=self.force, accept=covers, ttl=ttl,
                                               kind="public_evidence", metadata={"query": query,
                                               "source_url": "tool://futu/get_kline", "data_end": end.isoformat()})
            if result.get("ref"):
                self.references.append(result["ref"])
            value = result["payload"]["response"]
        except _Uncacheable as exc:
            self.diagnostics.append("INCOMPLETE_OHLCV")
            return exc.payload
        except CacheError as exc:
            self.diagnostics.append(exc.code)
            if loaded:
                return loaded[-1]
            # Source exceptions are never caught here. Cache failure only bypasses storage.
            self.source_calls += 1
            return adapter.read_private_json(name, arguments, timeout=timeout)
        return {**value, "data": [row for row in value["data"]
                                   if start <= date.fromisoformat(row["time"][:10]) <= end]}
