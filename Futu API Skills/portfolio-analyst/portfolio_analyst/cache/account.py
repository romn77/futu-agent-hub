"""Verified account scopes and explicit caching around the legacy one-shot helper."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import time

from ..account_alias import AccountAliaser, AccountAliasStore
from ..current_analysis import (
    CliResult, CurrentAnalysisError, SnapshotBindingError, _component_result, _invoke_cli, _validate_snapshot_scope,
    run_current_analysis,
)
from ..futu_adapter import FutuAdapter
from ..private_home import ensure_private_home
from ..repository import FutuPortfolioRepository, SourceDataError, _source_version
from ..service import load_account_currencies
from ..settings import discover_futuapi_skill
from .market import use_market_cache
from .types import CacheError, canonical, digest, utc


@dataclass(frozen=True)
class AccountScope:
    key: str
    account_ids: tuple[str, ...]


def resolve_scope(settings, account_ids=None, *, adapter=None):
    """Always discover account identity before a current-report lookup in a new request."""
    if adapter is None:
        location = discover_futuapi_skill()
        if location is None:
            raise SourceDataError("audited-compatible futuapi Skill is unavailable")
        adapter = FutuAdapter(location)
    repository = FutuPortfolioRepository(
        adapter, aliaser=AccountAliaser.ephemeral(),
        account_currencies=load_account_currencies(settings.data_home),
    )
    selected = repository.select_accounts(trd_env="REAL", account_ids=account_ids)
    if not selected or any(item.query_market is None or item.security_firm is None
                           or item.query_currency is None for item in selected):
        raise SourceDataError("account query metadata is incomplete")
    ensure_private_home(settings.data_home, create=True)
    aliaser = AccountAliasStore(settings.data_home).load_or_create()
    identity = {"endpoint": [settings.opend_host, settings.opend_port], "trd_env": "REAL",
                "base_currency": "USD", "accounts": sorted(
                    (item.private_id, item.security_firm, item.query_currency, item.public.account_type,
                     item.public.account_role, item.public.context_type) for item in selected)}
    key = aliaser.scoped_digest("cache-account-scope", canonical(identity).decode())
    return AccountScope(key, tuple(sorted(item.private_id for item in selected)))


def implementation_version(settings):
    """Include actual source, schemas and relevant configuration, including uncommitted edits."""
    package = Path(__file__).resolve().parents[1]
    sources = sorted(package.rglob("*.py")) + sorted((package.parent / "schemas").glob("*.json"))
    body = [(str(path.relative_to(package.parent)), hashlib.sha256(path.read_bytes()).hexdigest()) for path in sources]
    for name in ("fx_rates.json", "classifications.json", "security_metadata.json",
                 "account_currencies.json", "risk_policy.json"):
        path = settings.data_home / "config" / name
        if path.exists():
            if path.is_symlink() or path.resolve() != path.absolute() or path.stat().st_size > 1024 * 1024:
                raise CacheError("UNSAFE_PATH")
            body.append((name, hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            body.append((name, None))
    return digest(body)


class AccountCache:
    def __init__(self, settings, store, market, *, invoke=None, version=None):
        self.settings, self.store, self.market = settings, store, market
        self.invoke = invoke or _invoke_cli
        self.version = version or implementation_version(settings)
        self.diagnostics = []
        self.counts = {"snapshot": 0, "risk": 0, "technical": 0, "options": 0}

    def snapshot_query(self, scope):
        return {"kind": "account_snapshot", "scope": scope.key, "source": _source_version(),
                "config_version": self.version, "environment": "REAL", "currency": "USD"}

    def _save(self, query, payload, *, source_as_of, guards, kind, dependencies=()):
        try:
            result = self.store.put(query, payload, source_as_of=source_as_of, ttl=300,
                                    guards=guards, dependencies=dependencies, kind=kind,
                                    metadata={"query": query})
            return result
        except CacheError as exc:
            self.diagnostics.append(exc.code)
            return None

    def run(self, scope, *, refresh=False, historical_snapshot=None):
        if not isinstance(scope, AccountScope) or not scope.account_ids:
            raise CacheError("ACCOUNT_UNVERIFIED")
        if refresh:
            self.store.invalidate("account:" + scope.key)
        lease = None
        run_query = {"run": scope.key, "version": self.version}
        deadline = time.monotonic() + 10
        if self.store.enabled:
            try:
                while True:
                    reservation = self.store.reserve(run_query, seconds=600)
                    if reservation["status"] == "OWNER":
                        lease = reservation["lease"]
                        break
                    if time.monotonic() >= deadline:
                        raise CacheError("LEASE_BUSY")
                    time.sleep(0.05)
            except CacheError as exc:
                if exc.code == "LEASE_BUSY":
                    raise
                self.diagnostics.append(exc.code)
        guard = self.store.guard("account:" + scope.key)
        references, snapshot, risk = [], None, None
        snapshot_ref = None
        risk_ref = None
        self.market.references.clear()
        report_eligible = True

        def command(arguments):
            nonlocal snapshot, snapshot_ref, risk, risk_ref, report_eligible
            name = arguments[0]
            if name == "snapshot":
                query = self.snapshot_query(scope)
                receipt = self.store.lookup(query) if historical_snapshot is None else None
                value = historical_snapshot
                if receipt and receipt["status"] == "HIT":
                    try:
                        _validate_snapshot_scope(receipt["payload"])
                        value = receipt["payload"]
                    except CurrentAnalysisError:
                        self.diagnostics.append("CORRUPT_SNAPSHOT")
                if value is None:
                    self.counts[name] += 1
                    original = self.invoke(arguments)
                    if original.returncode:
                        return original
                    value = json.loads(original.stdout)
                    _validate_snapshot_scope(value)
                    receipt = self._save(query, value, source_as_of=value["as_of"], guards=[guard], kind="account_snapshot")
                    self._save({"kind": "snapshot_history", "scope": scope.key, "snapshot_id": value["snapshot_id"]},
                               value, source_as_of=value["as_of"], guards=[], kind="account_snapshot")
                snapshot = value
                if receipt and receipt.get("ref"):
                    snapshot_ref = receipt["ref"]
                    references.append(snapshot_ref)
                else:
                    report_eligible = False
                return CliResult(0, json.dumps(snapshot), "")

            query = {"kind": name, "scope": scope.key, "snapshot": snapshot["snapshot_id"],
                     "version": self.version, "risk": digest(risk) if name == "technical" else None}
            # The current option interface explicitly cannot prove quote freshness.
            # Preserve its output but never recycle it as a fresh option observation.
            receipt = self.store.lookup(query) if name != "options" else {"status": "MISS"}
            result = None
            if receipt["status"] == "HIT":
                candidate = CliResult(0, json.dumps(receipt["payload"]), "")
                try:
                    body, problem = _component_result(candidate, name, snapshot=snapshot, risk=risk)
                except SnapshotBindingError:
                    body, problem = None, {"code": "CORRUPT"}
                if problem is None:
                    result = candidate
            if result is None:
                self.counts[name] += 1
                result = self.invoke(arguments)
                body, problem = _component_result(result, name, snapshot=snapshot, risk=risk)
                if body is not None and name != "options" and snapshot_ref is not None:
                    dependencies = [snapshot_ref, *self.market.references]
                    if name == "technical" and risk_ref is not None:
                        dependencies.append(risk_ref)
                    receipt = self._save(query, body, source_as_of=snapshot["as_of"], guards=[guard],
                                         kind="analysis_artifact", dependencies=dependencies)
            if name == "risk":
                risk = body
                risk_ref = receipt.get("ref") if receipt else None
            if body is None or name == "options":
                report_eligible = False
            if body is not None and (not receipt or not receipt.get("ref")):
                report_eligible = False
            if receipt and receipt.get("ref"):
                references.append(receipt["ref"])
            return result

        try:
            with use_market_cache(self.market):
                envelope = run_current_analysis(settings=self.settings, invoke=command, account_ids=scope.account_ids)
            if self.store.guard("account:" + scope.key) != guard:
                raise CacheError("DEPENDENCY_CHANGED")
            return {"envelope": envelope, "references": references, "guard": guard,
                    "report_eligible": report_eligible, "counts": dict(self.counts),
                    "diagnostics": [*self.diagnostics, *self.market.diagnostics]}
        finally:
            if lease is not None:
                self.store.release(lease)
