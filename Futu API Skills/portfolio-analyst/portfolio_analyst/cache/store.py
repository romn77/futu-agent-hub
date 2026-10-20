"""Typed cache records with original-time expiry and verifiable payloads."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from .storage import Disk
from .types import CacheError, canonical, digest, now_utc, utc


class CacheStore:
    def __init__(self, root, *, scope="public", clock=now_utc, enabled=True,
                 max_item_bytes=None, max_bytes=None):
        if scope not in {"public", "private"}:
            raise CacheError("INVALID_INPUT")
        self.root = Path(root).expanduser().absolute()
        self.scope, self.clock, self.enabled = scope, clock, enabled
        self.max_item_bytes = max_item_bytes or (64 if scope == "public" else 16) * 1024**2
        self.max_bytes = max_bytes or (512 if scope == "public" else 128) * 1024**2
        self.peers = {scope: self}
        self.events = []

    def _result(self, status, reason=None, **extra):
        self.events.append({"status": status, "reason": reason, "scope": self.scope})
        return {"status": status, "reason": reason, **extra}

    def put(self, query, payload, *, source_as_of=None, ttl=None, kind="evidence",
            availability="COMPLETE", metadata=None, retention_seconds=None):
        if not self.enabled:
            return self._result("BYPASS", "CACHE_DISABLED")
        now = utc(self.clock())
        source = utc(source_as_of or now)
        if source > now:
            raise CacheError("CLOCK_INVALID")
        if ttl is not None and (isinstance(ttl, bool) or not 0 < ttl <= 365 * 86400):
            raise CacheError("INVALID_INPUT")
        if availability not in {"COMPLETE", "PARTIAL"}:
            raise CacheError("INCOMPLETE")
        if len(canonical(payload)) > self.max_item_bytes:
            raise CacheError("TOO_LARGE")
        retention = retention_seconds if retention_seconds is not None else (
            86400 if self.scope == "private" else None)
        record = {
            "schema_version": 1, "scope": self.scope, "kind": kind,
            "query_key": digest(query), "content_hash": digest(payload),
            "source_as_of": source.isoformat(), "created_at": now.isoformat(),
            "first_retrieved_at": now.isoformat(),
            "expires_at": (source + timedelta(seconds=ttl)).isoformat() if ttl else None,
            "retention_until": (now + timedelta(seconds=retention)).isoformat() if retention else None,
            "availability": availability, "metadata": metadata or {},
        }
        record_id = digest(record)
        with Disk(self.root, self.scope).locked(create=True) as disk:
            disk.write("objects", record["content_hash"], payload)
            disk.write("entries", record_id, record)
            disk.write("refs", record["query_key"], {"record_id": record_id})
            disk.write("access", record_id, {"at": now.isoformat()})
        return self._result("HIT" if availability == "COMPLETE" else "HIT_PARTIAL",
                            record=record, payload=payload, ref=self._ref(record_id, record))

    def _ref(self, record_id, record):
        return {"scope": self.scope, "record_id": record_id, "content_hash": record["content_hash"]}

    def _read(self, disk, record_id, now, historical):
        record = disk.read("entries", record_id)
        if not isinstance(record, dict) or digest(record) != record_id:
            raise CacheError("CORRUPT")
        if record.get("schema_version") != 1 or record.get("scope") != self.scope:
            raise CacheError("CORRUPT")
        if not historical and (utc(record["source_as_of"]) > now or utc(record["created_at"]) > now):
            raise CacheError("CLOCK_INVALID")
        if record["retention_until"] and now >= utc(record["retention_until"]):
            raise CacheError("EXPIRED")
        if not historical and record["expires_at"] and now >= utc(record["expires_at"]):
            raise CacheError("EXPIRED")
        payload = disk.read("objects", record["content_hash"], limit=self.max_item_bytes)
        if digest(payload) != record["content_hash"]:
            raise CacheError("CORRUPT")
        return record, payload

    def lookup(self, query, *, historical=False):
        if not self.enabled:
            return self._result("BYPASS", "CACHE_DISABLED")
        try:
            now = utc(self.clock())
            with Disk(self.root, self.scope).locked() as disk:
                ref = disk.read("refs", digest(query))
                record, payload = self._read(disk, ref["record_id"], now, historical)
                disk.write("access", ref["record_id"], {"at": now.isoformat()})
            return self._result("HIT" if record["availability"] == "COMPLETE" else "HIT_PARTIAL",
                                record=record, payload=payload, ref=self._ref(ref["record_id"], record))
        except FileNotFoundError:
            return self._result("MISS", "NOT_FOUND")
        except CacheError as exc:
            return self._result("MISS", exc.code)
        except (KeyError, TypeError, ValueError):
            return self._result("MISS", "CORRUPT")
