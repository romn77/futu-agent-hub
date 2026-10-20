from datetime import timedelta

import pytest

from portfolio_analyst.cache import CacheStore, CacheError
from portfolio_analyst.cache.public import PublicCache
from test_cache_core import NOW, QUERY
from test_cache_public import QUERY as PUBLIC_QUERY, URL


def test_ttl_policy_change_cannot_resurrect_expired_private_record(tmp_path, monkeypatch):
    store = CacheStore(tmp_path / 'private', scope='private', clock=lambda:NOW)
    store.put(QUERY, 'old', ttl=300)
    monkeypatch.setenv('RESEARCH_CACHE_ACCOUNT_TTL_SECONDS','600')
    changed = CacheStore(store.root, scope='private', clock=lambda:NOW+timedelta(seconds=301))
    assert changed.lookup(QUERY)['reason'] == 'VERSION_CHANGED'
    assert changed.lookup(QUERY, historical=True)['payload'] == 'old'


def test_public_policy_ttl_is_applied_and_private_changes_do_not_invalidate_filings(tmp_path, monkeypatch):
    monkeypatch.setenv('RESEARCH_CACHE_NEWS_TTL_SECONDS','60')
    moment = [NOW]
    cache = PublicCache(CacheStore(tmp_path/'public',clock=lambda:moment[0]))
    query = {**PUBLIC_QUERY,'dataset':'news'}
    cache.publish(query,{'observations':[]},source_url=URL,source_as_of=NOW)
    monkeypatch.setenv('RESEARCH_CACHE_ACCOUNT_TTL_SECONDS','600')
    same = PublicCache(CacheStore(cache.store.root,clock=lambda:moment[0]))
    assert same.lookup(query)['status'] == 'HIT'
    moment[0] += timedelta(seconds=60)
    assert same.lookup(query)['reason'] == 'EXPIRED'


def test_invalid_policy_is_explicit_and_off_ignores_unused_cache_config(tmp_path, monkeypatch):
    monkeypatch.setenv('RESEARCH_CACHE_ACCOUNT_TTL_SECONDS','unknown')
    with pytest.raises(CacheError,match='INVALID_POLICY'):
        CacheStore(tmp_path/'cache')
    store = CacheStore(tmp_path/'off',enabled=False)
    assert store.lookup(QUERY)['status'] == 'BYPASS'
    assert not store.root.exists()


def test_account_uses_configured_shorter_snapshot_window(tmp_path, monkeypatch):
    from test_cache_account import setup
    monkeypatch.setenv('RESEARCH_CACHE_ACCOUNT_TTL_SECONDS','60')
    cache,scope,source,moment = setup(tmp_path)
    cache.run(scope)
    moment[0] += timedelta(seconds=60)
    cache.run(scope)
    assert source.calls.count('snapshot') == 2


def test_report_deadline_is_bounded_by_both_snapshot_and_report_policy(tmp_path, monkeypatch):
    from test_cache_report import workflow, REQUEST, report, coverage
    monkeypatch.setenv('RESEARCH_CACHE_ACCOUNT_TTL_SECONDS','600')
    monkeypatch.setenv('RESEARCH_CACHE_REPORT_TTL_SECONDS','60')
    flow,scope,source,moment,ref,identities = workflow(tmp_path)
    first = flow.prepare(REQUEST)
    snap = first['envelope']['snapshot']
    moment[0] += timedelta(seconds=120)
    result = flow.finalize(REQUEST,first['context'],report(snap),coverage(snap),[ref])
    assert result['expires_at'] == (NOW+timedelta(seconds=180)).isoformat()
    moment[0] += timedelta(seconds=59)
    assert flow.prepare(REQUEST)['route'] == 'report_hit'
    moment[0] += timedelta(seconds=1)
    assert flow.prepare(REQUEST)['route'] == 'components_ready'
    assert source.calls.count('snapshot') == 1
