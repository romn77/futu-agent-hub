"""Typed cache records with original-time expiry and verifiable payloads."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from .storage import Disk
from .types import CacheError, canonical, digest, now_utc, utc
from . import coordination


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
            availability="COMPLETE", metadata=None, retention_seconds=None,
            dependencies=(), guards=(), semantic=None, lease=None):
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
        self._dependencies(dependencies, historical=False, visited=set())
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
            "dependencies": list(dependencies), "guards": list(guards),
        }
        with Disk(self.root, self.scope).locked(create=True) as disk:
            if lease is not None:
                coordination.verify(disk, lease, record["query_key"], now)
            self._guards(disk, guards)
            generation_key = "query:" + record["query_key"]
            state = self._state(disk, generation_key)
            semantic_hash = digest(payload if semantic is None else semantic)
            generation = state["generation"] + int(state.get("semantic") != semantic_hash)
            disk.write("state", digest(generation_key),
                       {"generation": generation, "semantic": semantic_hash})
            record["generation"] = generation
            record_id = digest(record)
            disk.write("objects", record["content_hash"], payload)
            disk.write("entries", record_id, record)
            disk.write("refs", record["query_key"], {"record_id": record_id})
            disk.write("access", record_id, {"at": now.isoformat()})
            self._prune(disk, now, keep=record_id)
        return self._result("HIT" if availability == "COMPLETE" else "HIT_PARTIAL",
                            record=record, payload=payload, ref=self._ref(record_id, record))

    def _ref(self, record_id, record):
        return {"scope": self.scope, "record_id": record_id, "content_hash": record["content_hash"]}

    def discovery_ref(self, receipt):
        return {"scope": self.scope, "query_key": receipt["record"]["query_key"],
                "generation": receipt["record"]["generation"]}

    @staticmethod
    def _state(disk, tag):
        try:
            state = disk.read("state", digest(tag))
            if not isinstance(state, dict) or type(state.get("generation")) is not int:
                raise CacheError("CORRUPT")
            return state
        except FileNotFoundError:
            return {"generation": 0}

    def guard(self, tag):
        try:
            with Disk(self.root, self.scope).locked() as disk:
                generation = self._state(disk, tag)["generation"]
        except FileNotFoundError:
            generation = 0
        return {"scope": self.scope, "tag": tag, "generation": generation}

    def invalidate(self, tag):
        if not self.enabled:
            return self._result("BYPASS", "CACHE_DISABLED")
        with Disk(self.root, self.scope).locked(create=True) as disk:
            value = self._state(disk, tag)["generation"] + 1
            disk.write("state", digest(tag), {"generation": value})
        return {"scope": self.scope, "tag": tag, "generation": value}

    def _guards(self, disk, guards):
        if not isinstance(guards, (list, tuple)) or len(guards) > 256:
            raise CacheError("INVALID_INPUT")
        for guard in guards:
            if (guard.get("scope") != self.scope
                    or self._state(disk, guard["tag"])["generation"] != guard["generation"]):
                raise CacheError("DEPENDENCY_CHANGED")

    def _dependencies(self, dependencies, *, historical, visited):
        if not isinstance(dependencies, (list, tuple)) or len(dependencies) > 256:
            raise CacheError("INVALID_INPUT")
        for ref in dependencies:
            scope = ref.get("scope")
            if self.scope == "public" and scope != "public":
                raise CacheError("SCOPE_CHANGED")
            peer = self.peers.get(scope)
            if peer is None:
                raise CacheError("DEPENDENCY_MISSING")
            result = peer.read_ref(ref, historical=historical, _visited=visited)
            if result["status"] != "HIT":
                missing = result.get("reason") in {"NOT_FOUND", "CORRUPT", "UNSAFE_PATH", "DEPENDENCY_MISSING"}
                raise CacheError("DEPENDENCY_MISSING" if missing else "DEPENDENCY_CHANGED")

    def _read(self, disk, record_id, now, historical):
        record = disk.read("entries", record_id)
        if not isinstance(record, dict) or digest(record) != record_id:
            raise CacheError("CORRUPT")
        if record.get("schema_version") != 1 or record.get("scope") != self.scope:
            raise CacheError("CORRUPT")
        if not historical:
            self._guards(disk, record.get("guards", []))
            if self._state(disk, "query:" + record["query_key"])["generation"] != record["generation"]:
                raise CacheError("DEPENDENCY_CHANGED")
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
        return self.read_ref({"scope": self.scope, "query_key": digest(query)}, historical=historical)

    def read_ref(self, requested, *, historical=False, _visited=None):
        if not self.enabled:
            return self._result("BYPASS", "CACHE_DISABLED")
        try:
            if requested.get("scope") != self.scope:
                raise CacheError("SCOPE_CHANGED")
            visited = set(_visited or ())
            identity = (self.scope, requested.get("record_id") or requested.get("query_key"))
            if identity in visited or len(visited) >= 32:
                raise CacheError("CORRUPT")
            visited.add(identity)
            now = utc(self.clock())
            with Disk(self.root, self.scope).locked() as disk:
                ref = (requested if requested.get("record_id")
                       else disk.read("refs", requested["query_key"]))
                record, payload = self._read(disk, ref["record_id"], now, historical)
                if requested.get("content_hash", record["content_hash"]) != record["content_hash"]:
                    raise CacheError("CORRUPT")
                if requested.get("generation", record["generation"]) != record["generation"]:
                    raise CacheError("DEPENDENCY_CHANGED")
                disk.write("access", ref["record_id"], {"at": now.isoformat()})
            self._dependencies(record.get("dependencies", []), historical=historical, visited=visited)
            return self._result("HIT" if record["availability"] == "COMPLETE" else "HIT_PARTIAL",
                                record=record, payload=payload, ref=self._ref(ref["record_id"], record))
        except FileNotFoundError:
            return self._result("MISS", "NOT_FOUND")
        except CacheError as exc:
            return self._result("MISS", exc.code)
        except (KeyError, TypeError, ValueError):
            return self._result("MISS", "CORRUPT")

    def reserve(self, query, *, seconds=60):
        return coordination.reserve(self, query, seconds=seconds)

    def release(self, lease):
        return coordination.release(self, lease)

    def renew(self, lease, *, seconds=60):
        return coordination.renew(self, lease, seconds=seconds)

    def get_or_create(self, query, loader, **options):
        return coordination.get_or_create(self, query, loader, **options)

    def rate_limit(self, source, **options):
        return coordination.rate_limit(self, source, **options)

    def _prune(self, disk, now, *, keep=None):
        records = []
        for key in disk.keys("entries"):
            record = disk.read("entries", key)
            try:
                access = disk.read("access", key)["at"]
            except FileNotFoundError:
                access = record["created_at"]
            expired = bool(record["retention_until"] and now >= utc(record["retention_until"]))
            records.append((not expired, access, key, record))
        removed = 0
        for not_expired, _access, key, record in sorted(records):
            if key == keep:
                continue
            if not_expired and disk.total_bytes() <= self.max_bytes:
                continue
            disk.remove("entries", key)
            try:
                disk.remove("access", key)
            except FileNotFoundError:
                pass
            try:
                if disk.read("refs", record["query_key"])["record_id"] == key:
                    disk.remove("refs", record["query_key"])
            except FileNotFoundError:
                pass
            removed += 1
            live = {disk.read("entries", rid)["content_hash"] for rid in disk.keys("entries")}
            for blob in disk.keys("objects"):
                if blob not in live:
                    disk.remove("objects", blob)
        if disk.total_bytes() > self.max_bytes:
            raise CacheError("CAPACITY_EXCEEDED")
        return {"removed_records": removed, "bytes": disk.total_bytes()}

    def prune(self):
        if not self.enabled:
            return self._result("BYPASS", "CACHE_DISABLED")
        try:
            with Disk(self.root, self.scope).locked() as disk:
                return self._prune(disk, utc(self.clock()))
        except FileNotFoundError:
            return {"removed_records": 0, "bytes": 0}

    def status(self):
        if not self.enabled:
            return self._result("BYPASS", "CACHE_DISABLED")
        try:
            with Disk(self.root, self.scope).locked() as disk:
                return {"scope": self.scope, "bytes": disk.total_bytes(),
                        "records": len(disk.keys("entries")), "max_bytes": self.max_bytes}
        except FileNotFoundError:
            return {"scope": self.scope, "bytes": 0, "records": 0, "max_bytes": self.max_bytes}
