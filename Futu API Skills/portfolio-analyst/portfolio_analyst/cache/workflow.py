"""Composition root for cache-aware analysis and a separate, validated report finish."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac
import json
from pathlib import Path
import secrets

from ..account_alias import AccountAliasStore
from ..current_analysis import _validate_snapshot_scope, run_current_analysis
from ..current_analysis import _invoke_cli
from ..snapshot_repository import SnapshotRepository, SnapshotRepositoryError
from .account import AccountCache, implementation_version, resolve_scope
from .market import MarketCache
from .report import fingerprint, request_document, validate_markdown
from .resources import policy_root
from .types import CacheError, digest, utc


class CachedWorkflow:
    def __init__(self, settings, private, public, *, resolver=resolve_scope, invoke=None, version=None):
        if private.scope != "private" or public.scope != "public":
            raise CacheError("SCOPE_CHANGED")
        if private.root.is_relative_to(public.root) or public.root.is_relative_to(private.root):
            raise CacheError("UNSAFE_PATH")
        self.settings, self.private, self.public = settings, private, public
        private.peers = public.peers = {"private": private, "public": public}
        self.resolver, self.invoke = resolver, invoke
        self._source_started = False
        root = policy_root()
        policy = [(str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest())
                  for p in sorted((root / "references").rglob("*.md"))]
        policy.append(("SKILL.md", hashlib.sha256((root / "SKILL.md").read_bytes()).hexdigest()))
        self.version = version or digest([implementation_version(settings), policy])

    def _report_query(self, scope, request):
        return {"kind": "portfolio_report", "scope": scope.key,
                "request": fingerprint(request), "version": self.version}

    def _historical(self, scope, request):
        snapshot_id = request["snapshot_id"]
        hit = (self.private.lookup({"kind": "snapshot_history", "scope": scope.key,
                                   "snapshot_id": snapshot_id}, historical=True)
               if request["cache_mode"] != "off" else {"status": "BYPASS"})
        if hit["status"] == "HIT":
            snapshot = hit["payload"]
        else:
            try:
                snapshot = SnapshotRepository(self.settings.data_home).read(snapshot_id).to_dict()
                aliaser = AccountAliasStore(self.settings.data_home).load()
                expected = {aliaser.alias(value) for value in scope.account_ids}
                if {a["account_ref"] for a in snapshot["accounts"]} != expected:
                    raise CacheError("SCOPE_CHANGED")
            except (SnapshotRepositoryError, ValueError, OSError):
                raise CacheError("HISTORICAL_SNAPSHOT_UNAVAILABLE") from None
        _validate_snapshot_scope(snapshot)
        return {"route": "historical_snapshot", "snapshot": snapshot,
                "reason": "EXPLICIT_HISTORICAL_VIEW", "current": False}

    def prepare(self, value, *, refresh_attempt=0, research_started_at=None):
        self._source_started = False
        try:
            return self._prepare(value, refresh_attempt=refresh_attempt, research_started_at=research_started_at)
        except CacheError as exc:
            if exc.code not in {"UNSAFE_PATH", "UNSUPPORTED_BACKEND", "CAPACITY_EXCEEDED", "CORRUPT"}:
                raise
            if self._source_started:
                raise
            request = request_document(value)
            if request["analysis_mode"] != "current":
                raise
            envelope = run_current_analysis(settings=self.settings, invoke=self.invoke, account_ids=request["account_ids"])
            return {"route": "components_ready", "envelope": envelope, "context": None,
                    "cache": {"reason": exc.code}, "research_cache_mode": "off"}

    def _prepare(self, value, *, refresh_attempt=0, research_started_at=None):
        request = request_document(value)
        now = utc(self.private.clock())
        if request["cache_mode"] == "off":
            if request["analysis_mode"] == "historical":
                scope = (resolve_scope(self.settings, request["account_ids"], persist_identity=False)
                         if self.resolver is resolve_scope else self.resolver(self.settings, request["account_ids"]))
                return self._historical(scope, request)
            envelope = run_current_analysis(settings=self.settings, invoke=self.invoke, account_ids=request["account_ids"])
            return {"route": "components_ready", "envelope": envelope, "context": None,
                    "cache": {"reason": "CACHE_DISABLED"}, "research_cache_mode": "off"}
        scope = self.resolver(self.settings, request["account_ids"])
        if request["analysis_mode"] == "historical":
            return self._historical(scope, request)
        refresh = (request["cache_mode"] in {"refresh_account", "refresh_all"}
                   or request["freshness_requirement"] == "latest"
                   or {"type": "account_changed"} in request["events"])
        if refresh:
            self.private.invalidate("account:" + scope.key)
        for event in request["events"]:
            if event["type"] == "sec_filing":
                self.public.invalidate("sec-discovery:" + event["cik"].zfill(10))
        if any(event["type"] == "sec_filing" for event in request["events"]):
            self.private.invalidate("reports:" + scope.key)
        query = self._report_query(scope, request)
        if not refresh and not request["events"]:
            hit = self.private.lookup(query)
            if hit["status"] == "HIT":
                body = hit["payload"]
                return {"route": "report_hit", "markdown": body["markdown"],
                        "snapshot_id": body["snapshot_id"], "as_of": body["as_of"],
                        "report_created_at": hit["record"]["created_at"], "reused_at": now.isoformat(),
                        "cache": {"ref": hit["ref"], "identity_discoveries": 1}}
        guard = self.private.guard("account:" + scope.key)
        report_guard = self.private.guard("reports:" + scope.key)
        draft_query = {"draft": query, "generation": guard["generation"], "reports": report_guard["generation"]}
        ownership = self.private.reserve(draft_query, seconds=600)
        if ownership["status"] != "OWNER":
            return {"route": "research_pending", "reason": "LEASE_BUSY", "retry_after_seconds": 1}
        lease = ownership["lease"]
        market = MarketCache(self.public, force=request["cache_mode"] == "refresh_all"
                             or request["freshness_requirement"] == "latest")
        def invoke(arguments):
            self._source_started = True
            return (self.invoke or _invoke_cli)(arguments)
        account = AccountCache(self.settings, self.private, market, invoke=invoke, version=self.version)
        try:
            packet = account.run(scope)
            token = secrets.token_hex(32)
            context = {"scope": scope.key, "request": fingerprint(request), "version": self.version,
                       "token_hash": digest(token), "created_at": now.isoformat(),
                       "research_started_at": research_started_at or now.isoformat(),
                       "snapshot": packet["envelope"]["snapshot"], "references": packet["references"],
                       "guard": packet["guard"], "report_guard": report_guard, "eligible": packet["report_eligible"],
                       "refresh_attempt": refresh_attempt, "draft_lease": lease}
            try:
                stored = self.private.put({"context": token}, context, kind="analysis_context", ttl=86400)
                context_receipt = {"ref": stored["ref"], "token": token}
            except CacheError as exc:
                context_receipt = None
                packet["diagnostics"].append(exc.code)
                self._release(lease)
            return {"route": "components_ready", "envelope": packet["envelope"],
                    "context": context_receipt, "refresh_attempt": refresh_attempt,
                    "cache": {"identity_discoveries": 1, "source_counts": packet["counts"],
                              "diagnostics": packet["diagnostics"], "evidence_refs": market.references},
                    "research_cache_mode": ("refresh_all" if request["cache_mode"] == "refresh_all" else
                                            "latest" if request["freshness_requirement"] == "latest" else "prefer")}
        except BaseException:
            self._release(lease)
            raise

    def _release(self, lease):
        try:
            self.private.release(lease)
        except CacheError:
            # Cache housekeeping cannot replace a validated result or a source exception.
            pass

    def _context(self, receipt):
        if not isinstance(receipt, dict) or set(receipt) != {"ref", "token"}:
            raise CacheError("CONTEXT_INVALID")
        value = self.private.read_ref(receipt["ref"])
        if (value["status"] != "HIT" or value["record"]["kind"] != "analysis_context"
                or not hmac.compare_digest(value["payload"]["token_hash"], digest(receipt["token"]))):
            raise CacheError("CONTEXT_INVALID")
        return value["payload"]

    def abort(self, receipt):
        context = self._context(receipt)
        self._release(context["draft_lease"])
        return {"route": "context_released"}

    def _research(self, references, markdown, request, context):
        if not isinstance(references, list) or not 1 <= len(references) <= 128:
            raise CacheError("RESEARCH_INCOMPLETE")
        seen, queue, subjects, discovered = set(), list(references), set(), set()
        while queue:
            ref = queue.pop()
            if ref.get("scope") != "public":
                raise CacheError("SCOPE_CHANGED")
            identity = digest(ref)
            if identity in seen:
                continue
            seen.add(identity)
            if len(seen) > 256:
                raise CacheError("RESEARCH_INCOMPLETE")
            value = self.public.read_ref(ref)
            if value["status"] != "HIT":
                raise CacheError("DEPENDENCY_CHANGED")
            record = value["record"]
            query = record["metadata"].get("query", {})
            dataset, subject = query.get("dataset"), query.get("subject")
            if not isinstance(subject, str) or dataset is None:
                raise CacheError("RESEARCH_INCOMPLETE")
            if dataset in {"sec.index", "news"}:
                discovered.add(subject)
            elif dataset in {"sec.content", "sec.facts", "sec.holdings", "disclosure"}:
                subjects.add(subject)
            source_url = record["metadata"].get("source_url")
            if source_url and dataset not in {"sec.index", "news"} and source_url not in markdown:
                raise CacheError("REPORT_INVALID")
            if request["requested_cutoff"] is not None:
                available = record["metadata"].get("available_at")
                if dataset not in {"summary", "sec.index", "news"} and (
                    available is None or utc(available) > utc(request["requested_cutoff"])):
                    raise CacheError("AFTER_CUTOFF")
            must_refresh = request["cache_mode"] == "refresh_all" or (
                request["freshness_requirement"] == "latest" and record["expires_at"] is not None)
            if must_refresh and utc(record["created_at"]) < utc(context["research_started_at"]):
                raise CacheError("RESEARCH_REFRESH_REQUIRED")
            queue.extend(record.get("dependencies", []))
        if not subjects <= discovered:
            raise CacheError("DISCOVERY_REQUIRED")

    def finalize(self, value, receipt, markdown, coverage, research_refs):
        request = request_document(value)
        context = self._context(receipt)
        scope = self.resolver(self.settings, request["account_ids"])
        if (context["scope"] != scope.key or context["request"] != fingerprint(request)
                or context["version"] != self.version):
            raise CacheError("CONTEXT_INVALID")
        snapshot = context["snapshot"]
        validate_markdown(markdown, snapshot, coverage)
        now = utc(self.private.clock())
        account_ttl = self.private.policy.values["ACCOUNT_TTL_SECONDS"]
        stale = (now >= utc(snapshot["as_of"]) + timedelta(seconds=account_ttl)
                 or self.private.guard("account:" + scope.key) != context["guard"])
        if stale:
            self._release(context["draft_lease"])
            if context["refresh_attempt"] >= 1:
                return {"route": "historical_only", "markdown": markdown, "as_of": snapshot["as_of"],
                        "reason": "SNAPSHOT_EXPIRED", "current": False}
            return self.prepare({**request, "cache_mode": "refresh_account", "events": []},
                                refresh_attempt=1, research_started_at=context["research_started_at"])
        if self.private.guard("reports:" + scope.key) != context["report_guard"]:
            self._release(context["draft_lease"])
            return self.prepare({**request, "cache_mode": "prefer", "events": []},
                                refresh_attempt=context["refresh_attempt"],
                                research_started_at=context["research_started_at"])
        if (not context["eligible"] or not coverage["research_complete"] or coverage["open_required_items"]):
            self._release(context["draft_lease"])
            return {"route": "report_uncached", "markdown": markdown, "reason": "INCOMPLETE"}
        try:
            self._research(research_refs, markdown, request, context)
        except CacheError as exc:
            self._release(context["draft_lease"])
            if exc.code in {"REPORT_INVALID", "SCOPE_CHANGED", "AFTER_CUTOFF", "CONTEXT_INVALID"}:
                raise
            return {"route": "report_uncached", "markdown": markdown, "reason": exc.code}
        query = self._report_query(scope, request)
        ownership = None
        try:
            ownership = self.private.reserve(query)
            if ownership["status"] != "OWNER":
                return {"route": "report_uncached", "markdown": markdown, "reason": "LEASE_BUSY"}
            from .coordination import verify
            from .storage import Disk
            with Disk(self.private.root, "private").locked() as disk:
                verify(disk, context["draft_lease"], context["draft_lease"]["query_key"], now)
            report_ttl = min(account_ttl, (now - utc(snapshot["as_of"])).total_seconds()
                             + self.private.policy.values["REPORT_TTL_SECONDS"])
            result = self.private.put(query, {"markdown": markdown, "snapshot_id": snapshot["snapshot_id"],
                                      "as_of": snapshot["as_of"], "coverage": coverage},
                                      source_as_of=snapshot["as_of"], ttl=report_ttl, kind="portfolio_report",
                                      guards=[context["guard"], context["report_guard"]],
                                      dependencies=[*context["references"], *research_refs], lease=ownership["lease"])
            return {"route": "report_published", "markdown": markdown, "ref": result["ref"],
                    "as_of": snapshot["as_of"], "expires_at": result["record"]["expires_at"]}
        except CacheError as exc:
            return {"route": "report_uncached", "markdown": markdown, "reason": exc.code}
        finally:
            if ownership is not None and ownership["status"] == "OWNER":
                self._release(ownership["lease"])
            self._release(context["draft_lease"])
