"""Short ownership leases; only the current owner can publish a result."""

from datetime import timedelta
import secrets
import time
import re

from .storage import Disk
from .types import CacheError, digest, utc


def _validate_lease(value, key):
    if (not isinstance(value, dict) or set(value) != {"query_key", "token", "created_at", "expires_at"}
            or value["query_key"] != key or not isinstance(value["token"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["token"])):
        raise CacheError("CORRUPT")
    if utc(value["expires_at"]) <= utc(value["created_at"]):
        raise CacheError("CORRUPT")
    return value


def reserve(store, query, *, seconds=60):
    if not store.enabled:
        return store._result("BYPASS", "CACHE_DISABLED")
    if isinstance(seconds, bool) or not 0 < seconds <= 600:
        raise CacheError("INVALID_INPUT")
    key = digest(query)
    with Disk(store.root, store.scope).locked(create=True) as disk:
        now = utc(store.clock())
        try:
            old = _validate_lease(disk.read("leases", key), key)
            if utc(old["created_at"]) > now:
                raise CacheError("CLOCK_INVALID")
            if now < utc(old["expires_at"]):
                return store._result("BUSY", "LEASE_BUSY")
        except FileNotFoundError:
            pass
        lease = {"query_key": key, "token": secrets.token_hex(32),
                 "created_at": now.isoformat(), "expires_at": (now + timedelta(seconds=seconds)).isoformat()}
        store._metadata_write(disk, "leases", key, lease)
    return {"status": "OWNER", "lease": lease}


def verify(disk, lease, key, now):
    try:
        current = _validate_lease(disk.read("leases", key), key)
        valid = (lease["query_key"] == key and current["token"] == lease["token"]
                 and utc(current["created_at"]) <= now < utc(current["expires_at"]))
    except (FileNotFoundError, KeyError, TypeError):
        valid = False
    if not valid:
        raise CacheError("STALE_LEASE")


def release(store, lease):
    if not store.enabled:
        return
    try:
        with Disk(store.root, store.scope).locked() as disk:
            current = _validate_lease(disk.read("leases", lease["query_key"]), lease["query_key"])
            if current["token"] == lease["token"]:
                disk.remove("leases", lease["query_key"])
    except FileNotFoundError:
        pass


def renew(store, lease, *, seconds=60):
    if not 0 < seconds <= 600:
        raise CacheError("INVALID_INPUT")
    with Disk(store.root, store.scope).locked() as disk:
        now = utc(store.clock())
        verify(disk, lease, lease["query_key"], now)
        updated = {**lease, "expires_at": (now + timedelta(seconds=seconds)).isoformat()}
        store._metadata_write(disk, "leases", lease["query_key"], updated)
    return updated


def get_or_create(store, query, loader, *, force=False, wait_seconds=10, lease_seconds=60,
                  accept=None, **options):
    if not store.enabled:
        return {"status": "BYPASS", "reason": "CACHE_DISABLED", "payload": loader()}
    deadline = time.monotonic() + min(max(wait_seconds, 0), 600)
    while True:
        if not force:
            cached = store.lookup(query)
            if cached["status"] == "HIT" and (accept is None or accept(cached)):
                return cached
        reservation = reserve(store, query, seconds=lease_seconds)
        if reservation["status"] == "OWNER":
            lease = reservation["lease"]
            try:
                if not force:
                    cached = store.lookup(query)
                    if cached["status"] == "HIT" and (accept is None or accept(cached)):
                        return cached
                value = loader()
                return store.put(query, value, lease=lease, **options)
            finally:
                try:
                    release(store, lease)
                except CacheError as exc:
                    store.events.append({"status": "ERROR", "reason": exc.code, "operation": "release"})
        if time.monotonic() >= deadline:
            raise CacheError("LEASE_BUSY")
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))


def rate_limit(store, source, *, interval=0.5, wait_seconds=30, sleeper=time.sleep):
    """Reserve a source slot across cooperating processes, outside the file lock."""
    if not store.enabled:
        return
    now = utc(store.clock())
    with Disk(store.root, store.scope).locked(create=True) as disk:
        key = digest(["rate", source])
        try:
            state = disk.read("state", key)
            previous = utc(state["reserved_until"])
        except FileNotFoundError:
            previous = now
        delay = max(0, (previous - now).total_seconds())
        if delay > wait_seconds:
            raise CacheError("LEASE_BUSY")
        store._metadata_write(disk, "state", key, {"reserved_until": (max(now, previous) + timedelta(seconds=interval)).isoformat()})
    if delay:
        sleeper(delay)
