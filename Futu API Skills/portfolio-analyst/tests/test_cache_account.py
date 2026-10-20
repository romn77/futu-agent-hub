from dataclasses import replace
from datetime import timedelta
import json

import pytest

from portfolio_analyst.account_alias import AccountAliaser
from portfolio_analyst.cache import CacheStore
from portfolio_analyst.cache.account import AccountCache, AccountScope, resolve_scope
from portfolio_analyst.cache.market import MarketCache
from portfolio_analyst.cache.types import utc
from portfolio_analyst.current_analysis import CliResult, SnapshotBindingError
from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.option_risk import analyze_option_risk
from portfolio_analyst.repository import AccountSelectionRequired, FutuPortfolioRepository
from portfolio_analyst.risk import analyze_risk
from portfolio_analyst.settings import Settings
from portfolio_analyst.snapshot_io import snapshot_from_document
from portfolio_analyst.technical import analyze_technical
from test_cache_core import NOW
from test_current_analysis_helper import _snapshot


class Source:
    def __init__(self, moment, *, options=False):
        self.moment, self.options = moment, options
        self.calls = []
        self.snapshot = None
        self.risk = None

    def __call__(self, args):
        command = args[0]
        self.calls.append(command)
        if command != "snapshot":
            path = self.home / args[args.index("--snapshot") + 1]
            self.snapshot = snapshot_from_document(json.loads(path.read_text()))
            if "--risk-report" in args:
                self.risk = json.loads((self.home / args[args.index("--risk-report") + 1]).read_text())
        if command == "snapshot":
            source = replace(snapshot_from_document(_snapshot(held_options=self.options)),
                             as_of=self.moment[0], snapshot_id="")
            self.snapshot = replace(source, snapshot_id=snapshot_identity(source))
            body = self.snapshot.to_dict()
        elif command == "risk":
            self.risk = analyze_risk(self.snapshot).to_dict()
            body = self.risk
        elif command == "technical":
            body = analyze_technical(self.snapshot, provider=None, risk_document=self.risk).to_dict()
        elif command == "options":
            body = analyze_option_risk(self.snapshot, analysis_now=self.moment[0], clock=lambda: self.moment[0]).to_dict()
        else:
            raise AssertionError(command)
        return CliResult(0, json.dumps(body), "")


def setup(tmp_path, *, options=False):
    moment = [NOW]
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    private = CacheStore(home / "cache/v1", scope="private", clock=lambda: moment[0])
    public = CacheStore(tmp_path / "public", clock=lambda: moment[0])
    private.peers = public.peers = {"public": public, "private": private}
    source = Source(moment, options=options)
    source.home = home
    cache = AccountCache(Settings(home), private, MarketCache(public), invoke=source, version="test-v1")
    scope = AccountScope("a" * 64, ("90000001",))
    return cache, scope, source, moment


def test_same_scope_reuses_snapshot_and_components_with_original_binding(tmp_path):
    cache, scope, source, moment = setup(tmp_path)
    first = cache.run(scope)
    moment[0] += timedelta(seconds=180)
    second = cache.run(scope)
    assert source.calls == ["snapshot", "risk", "technical"]
    assert first["envelope"] == second["envelope"]
    assert utc(second["envelope"]["snapshot"]["as_of"]) == NOW


def test_exact_expiry_refreshes_and_rebinds_all_components(tmp_path):
    cache, scope, source, moment = setup(tmp_path)
    first = cache.run(scope)
    moment[0] += timedelta(seconds=300)
    second = cache.run(scope)
    assert source.calls.count("snapshot") == 2
    assert source.calls.count("risk") == 2
    assert first["envelope"]["snapshot"]["snapshot_id"] != second["envelope"]["snapshot"]["snapshot_id"]
    assert second["envelope"]["risk"]["source_snapshot_id"] == second["envelope"]["snapshot"]["snapshot_id"]


def test_known_trade_invalidates_before_ttl_and_scope_change_does_not_hit(tmp_path):
    cache, scope, source, moment = setup(tmp_path)
    cache.run(scope)
    moment[0] += timedelta(seconds=10)
    cache.run(scope, refresh=True)
    assert source.calls.count("snapshot") == 2
    cache.run(AccountScope("b" * 64, ("90000002",)))
    assert source.calls.count("snapshot") == 3


def test_unverified_option_quote_is_not_replayed_as_current(tmp_path):
    cache, scope, source, moment = setup(tmp_path, options=True)
    first = cache.run(scope)
    second = cache.run(scope)
    assert source.calls.count("snapshot") == 1
    assert source.calls.count("risk") == 1
    assert source.calls.count("options") == 2
    assert not first["report_eligible"] and not second["report_eligible"]


def test_component_version_change_never_reuses_calculations(tmp_path):
    cache, scope, source, moment = setup(tmp_path)
    cache.run(scope)
    cache.version = "test-v2"
    cache.run(scope)
    assert source.calls.count("risk") == 2


def test_account_discovery_is_separate_from_holdings_reads():
    class Reader:
        def __init__(self): self.calls = []
        def protect_sensitive_values(self, values): pass
        def read_private_json(self, name, args, **kwargs):
            self.calls.append(name)
            assert name == "get_accounts"
            return {"accounts": [{"acc_id": "90000001", "trd_env": "REAL", "acc_status": "ACTIVE",
                                   "security_firm": "FUTUSECURITIES", "trdmarket_auth": ["US"]}]}
    reader = Reader()
    repository = FutuPortfolioRepository(reader, aliaser=AccountAliaser.ephemeral(),
                                         account_currencies={"90000001": "USD"})
    selected = repository.select_accounts(trd_env="REAL")
    assert [item.private_id for item in selected] == ["90000001"]
    assert reader.calls == ["get_accounts"]


def test_verified_scope_changes_with_actual_members_even_on_same_endpoint(tmp_path):
    class Reader:
        def __init__(self): self.ids = ['90000001']; self.calls = []
        def protect_sensitive_values(self, values): pass
        def read_private_json(self, name, args, **kwargs):
            self.calls.append(name)
            assert name == 'get_accounts'
            return {'accounts': [{'acc_id': identifier, 'trd_env': 'REAL', 'acc_status': 'ACTIVE',
                                  'security_firm': 'FUTUSECURITIES', 'trdmarket_auth': ['US']}
                                 for identifier in self.ids]}
    home = tmp_path / 'private'
    (home / 'config').mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    config = home / 'config/account_currencies.json'
    config.write_text(json.dumps({'provenance':'USER_CONFIG','accounts':{'90000001':'USD','90000002':'USD'}}))
    config.chmod(0o600)
    reader = Reader()
    first = resolve_scope(Settings(home), adapter=reader)
    again = resolve_scope(Settings(home), adapter=reader)
    assert first == again and '90000001' not in first.key
    reader.ids = ['90000002']
    other = resolve_scope(Settings(home), adapter=reader)
    assert first.key != other.key
    reader.ids = ['90000001', '90000002']
    with pytest.raises(AccountSelectionRequired):
        resolve_scope(Settings(home), adapter=reader)
    both = resolve_scope(Settings(home), reader.ids, adapter=reader)
    assert both.key not in {first.key, other.key}
    assert reader.calls == ['get_accounts'] * 5
