"""Optional local cache bridge. SEC remains independently runnable without analyst."""

from datetime import datetime, time as daytime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

from .evidence import ResearchError, available_by, cik, require, timestamp
from .reader import content_slice


class BridgeError(ValueError):
    pass


class CacheBridge:
    def __init__(self, *, root=None):
        self.root = root
        self.script = Path(__file__).resolve().parents[2] / "portfolio-analyst/scripts/cache.py"
        if not self.script.is_file():
            raise BridgeError("UNSUPPORTED_BACKEND")
        self.python = os.environ.get("RESEARCH_CACHE_PYTHON", sys.executable)
        if not Path(self.python).is_absolute() or not os.access(self.python, os.X_OK):
            raise BridgeError("UNSUPPORTED_BACKEND")

    def _call(self, operation, *, request=None, manifest=None, payload=None, lease=None):
        with tempfile.TemporaryDirectory(prefix="sec-cache-bridge-") as directory:
            folder = Path(directory).resolve()
            command = [self.python, "-B", str(self.script), operation, "--json"]
            for name, value in (("request", request), ("manifest", manifest), ("payload", payload), ("lease", lease)):
                if value is None:
                    continue
                path = folder / (name + ".json")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(value, stream, allow_nan=False)
                command.extend(("--" + name, str(path)))
            environment = {key: os.environ[key] for key in
                           ("PATH", "HOME", "TMPDIR", "LANG", "RESEARCH_CACHE_HOME") if key in os.environ}
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            if self.root is not None:
                environment["RESEARCH_CACHE_HOME"] = str(self.root)
            result = subprocess.run(command, capture_output=True, timeout=40, env=environment)
            try:
                if len(result.stdout) > 96 * 1024 * 1024:
                    raise BridgeError("TOO_LARGE")
                receipt = json.loads(result.stdout)
            except (ValueError, UnicodeError):
                raise BridgeError("CACHE_BRIDGE_FAILED") from None
            if result.returncode or receipt.get("status") == "ERROR":
                raise BridgeError(receipt.get("reason", "CACHE_BRIDGE_FAILED"))
            return receipt

    def lookup(self, query, **kwargs):
        return self._call("lookup", request={"query": query, **kwargs})

    def reserve(self, query):
        return self._call("reserve", request={"query": query, "seconds": 600})

    def publish(self, query, payload, *, lease, **metadata):
        return self._call("publish", manifest={"query": query, **metadata}, payload=payload, lease=lease)

    def release(self, lease):
        return self._call("release", lease=lease)

    def rate_limit(self):
        return self._call("rate-limit", request={"source": "SEC"})


def _available_at(value):
    if len(str(value)) == 10:
        date = datetime.fromisoformat(value).date() + timedelta(days=1)
        return datetime.combine(date, daytime(), ZoneInfo("America/New_York")).astimezone(timezone.utc).isoformat()
    return timestamp(value).isoformat()


def _semantic(value):
    if isinstance(value, dict):
        return {key: _semantic(item) for key, item in value.items()
                if key not in {"retrieved_at", "knowledge_cutoff", "downloaded_bytes"}}
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    return value


class CachedReader:
    def __init__(self, reader, bridge, *, force=False, strict_index=False,
                 clock=lambda: datetime.now(timezone.utc)):
        self.reader, self.bridge, self.clock = reader, bridge, clock
        self.budget = reader.budget
        self.force, self.strict_index = force, strict_index
        self.receipts, self.diagnostics = [], []
        sources = ("reader.py", "holdings.py", "evidence.py", "calendar.py", "cache.py")
        self.version = hashlib.sha256(b"".join(Path(__file__).with_name(n).read_bytes() for n in sources)).hexdigest()

    def _query(self, identifier, dataset, parameters):
        return {"provider": "SEC", "interface": "edgartools-5.62.0", "version": self.version,
                "subject": cik(identifier), "dataset": dataset, "parameters": parameters}

    def _resolve(self, query, load, *, cutoff=None, force=False, source_url=None, available=None):
        lease, result = None, None
        try:
            try:
                if not (self.force or force):
                    result = self.bridge.lookup(query, **({"cutoff": cutoff} if cutoff else {}))
                    if result["status"] == "HIT":
                        self.receipts.append(result)
                        return result["payload"]
                deadline = time.monotonic() + 10
                while True:
                    ownership = self.bridge.reserve(query)
                    if ownership["status"] == "OWNER":
                        lease = ownership["lease"]
                        break
                    if not (self.force or force):
                        result = self.bridge.lookup(query, **({"cutoff": cutoff} if cutoff else {}))
                        if result["status"] == "HIT":
                            self.receipts.append(result)
                            return result["payload"]
                    self.budget.check()
                    if time.monotonic() >= deadline:
                        raise BridgeError("LEASE_BUSY")
                    time.sleep(0.05)
                if not (self.force or force):
                    result = self.bridge.lookup(query, **({"cutoff": cutoff} if cutoff else {}))
                    if result["status"] == "HIT":
                        self.receipts.append(result)
                        return result["payload"]
            except (BridgeError, OSError, subprocess.SubprocessError) as error:
                self.diagnostics.append(str(error) if isinstance(error, BridgeError) else "CACHE_BRIDGE_FAILED")
                lease = None
            payload = load()  # Source failures propagate; a cache failure never repeats this call.
            if lease is not None:
                try:
                    source = source_url or payload.get("source_url") or "https://data.sec.gov/"
                    published = available(payload) if available else payload.get("filed")
                    receipt = self.bridge.publish(
                        query, payload, lease=lease, source_url=source,
                        source_as_of=self.clock().isoformat(),
                        available_at=_available_at(published) if published else None,
                        complete=not payload.get("truncated", False) and payload.get("complete", True),
                        semantic=_semantic(payload),
                    )
                    self.receipts.append(receipt)
                except (BridgeError, OSError, subprocess.SubprocessError) as error:
                    self.diagnostics.append(str(error) if isinstance(error, BridgeError) else "CACHE_BRIDGE_FAILED")
            return payload
        finally:
            if lease is not None:
                try:
                    self.bridge.release(lease)
                except (BridgeError, OSError, subprocess.SubprocessError):
                    self.diagnostics.append("CACHE_RELEASE_FAILED")

    def index(self, identifier, forms, start, cutoff, *, limit=100):
        query = self._query(identifier, "sec.index", {"forms": sorted(forms), "start": start,
                             "end_date": timestamp(cutoff).date().isoformat(), "limit": limit})
        result = self._resolve(query, lambda: self.reader.index(identifier, forms, start, cutoff, limit=limit),
                               force=self.strict_index)
        return {**result, "filings": [row for row in result["filings"] if available_by(row["filed"], cutoff)]}

    def content(self, identifier, accession, cutoff, *, section="full_text", offset=0, limit=12000):
        query = self._query(identifier, "sec.content", {"accession": accession, "section": section})
        document = self._resolve(query, lambda: self.reader.document(identifier, accession, cutoff, section=section),
                                 cutoff=cutoff)
        require(available_by(document["filed"], cutoff), "FILING_AFTER_CUTOFF")
        return {**document, **content_slice(document["text"], offset=offset, limit=limit)}

    def holdings(self, identifier, accession, cutoff):
        query = self._query(identifier, "sec.holdings", {"accession": accession})
        document = self._resolve(query, lambda: self.reader.holdings(identifier, accession, cutoff), cutoff=cutoff)
        require(available_by(document["filed"], cutoff), "FILING_AFTER_CUTOFF")
        return document

    def facts(self, identifier, accessions, concepts, cutoff, *, limit=2000):
        query = self._query(identifier, "sec.facts", {"accessions": sorted(accessions),
                            "concepts": sorted(concepts), "limit": limit})
        def published(_payload):
            return max(str(self.reader.filings[cik(identifier), acc].filing_date) for acc in accessions)
        result = self._resolve(query, lambda: self.reader.facts(identifier, accessions, concepts, cutoff, limit=limit),
                               cutoff=cutoff, available=published)
        require(all(available_by(row["filed"], cutoff) for row in result["facts"]), "FILING_AFTER_CUTOFF")
        return result
