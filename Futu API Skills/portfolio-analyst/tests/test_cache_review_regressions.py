from datetime import timedelta
import json
import threading

import pytest

from portfolio_analyst.cache import CacheError, CacheStore
from portfolio_analyst.cache.public import PublicCache
from portfolio_analyst.cache.storage import Disk
from portfolio_analyst.cache.types import digest
from test_cache_core import NOW, QUERY
from test_cache_public import QUERY as PUBLIC_QUERY, URL


def test_failed_quota_publication_is_not_visible(tmp_path):
    store = CacheStore(tmp_path / "cache", max_bytes=500, clock=lambda: NOW)
    with pytest.raises(CacheError, match="CAPACITY_EXCEEDED"):
        store.put(QUERY, "x" * 1000)
    assert store.lookup(QUERY)["status"] == "MISS"
    assert store.status()["bytes"] <= 500


def test_wrong_query_reference_cannot_return_other_valid_record(tmp_path):
    store = CacheStore(tmp_path / "cache", clock=lambda: NOW)
    store.put({"a": 1}, "A")
    other = store.put({"b": 1}, "B")
    ref_path = store.root / "refs" / (digest({"a": 1}) + ".json")
    ref_path.write_text(json.dumps({"record_id": other["ref"]["record_id"]}))
    assert store.lookup({"a": 1})["reason"] == "CORRUPT"


def test_corrupt_lease_has_stable_cache_error(tmp_path):
    store = CacheStore(tmp_path / "cache", clock=lambda: NOW)
    store.reserve(QUERY)
    path = store.root / "leases" / (digest(QUERY) + ".json")
    path.write_text("{}")
    with pytest.raises(CacheError, match="CORRUPT"):
        store.reserve(QUERY)


def test_lease_is_rechecked_after_waiting_for_publish_lock(tmp_path):
    moment = [NOW]
    sampled = threading.Event()
    def clock():
        sampled.set()
        return moment[0]
    store = CacheStore(tmp_path / "cache", clock=clock)
    lease = store.reserve(QUERY, seconds=1)["lease"]
    sampled.clear()
    outcome = []
    def publish():
        try:
            store.put(QUERY, "stale", lease=lease)
            outcome.append("published")
        except CacheError as exc:
            outcome.append(exc.code)
    with Disk(store.root, store.scope).locked():
        child = threading.Thread(target=publish)
        child.start()
        assert sampled.wait(2)
        moment[0] += timedelta(seconds=2)
    child.join(5)
    assert outcome == ["STALE_LEASE"]
    assert store.lookup(QUERY)["status"] == "MISS"


def test_dependency_change_between_precheck_and_commit_is_rejected(tmp_path, monkeypatch):
    store = CacheStore(tmp_path / "cache", clock=lambda: NOW)
    evidence = store.put(QUERY, "version-one")
    original = store._dependencies
    changed = []
    def precheck(dependencies, **kwargs):
        result = original(dependencies, **kwargs)
        if dependencies and not changed:
            changed.append(True)
            store.put(QUERY, "version-two")
        return result
    monkeypatch.setattr(store, "_dependencies", precheck)
    with pytest.raises(CacheError, match="DEPENDENCY_CHANGED"):
        store.put({"summary": 1}, "old conclusion", dependencies=[evidence["ref"]])
    assert store.lookup({"summary": 1})["status"] == "MISS"


def test_summary_cannot_bypass_dedicated_public_validation(tmp_path):
    cache = PublicCache(CacheStore(tmp_path / "cache", clock=lambda: NOW))
    with pytest.raises(CacheError, match="INVALID_SUMMARY"):
        cache.publish({**PUBLIC_QUERY, "dataset": "summary"}, {"text": "unsupported"}, source_url=URL)


def test_arbitrary_portfolio_shape_is_not_a_sec_document(tmp_path):
    cache = PublicCache(CacheStore(tmp_path / "cache", clock=lambda: NOW))
    with pytest.raises(CacheError, match="PRIVATE_CONTENT|INVALID_EVIDENCE"):
        cache.publish({**PUBLIC_QUERY, "dataset": "sec.content"}, {"positions": [{"symbol": "US.AAA", "quantity": 150, "cost": 23}],
                                    "text": "I hold 150 shares; sell half of your position"}, source_url=URL)


def test_scalar_observation_fields_cannot_hide_private_objects(tmp_path):
    cache = PublicCache(CacheStore(tmp_path / "cache", clock=lambda: NOW))
    with pytest.raises(CacheError, match="INVALID_EVIDENCE"):
        cache.publish({**PUBLIC_QUERY, "dataset": "news"}, {"observations": [
            {"text": "data", "value": {"positions": [{"symbol": "US.AAA", "quantity": 150, "cost": 23}]}}
        ]}, source_url=URL, source_as_of=NOW)


def test_metadata_reservations_respect_quota_and_expired_leases_are_reclaimed(tmp_path):
    moment = [NOW]
    store = CacheStore(tmp_path / "cache", max_bytes=1000, clock=lambda: moment[0])
    for i in range(10):
        try:
            store.reserve({"i": i}, seconds=1)
        except CacheError as exc:
            assert exc.code == "CAPACITY_EXCEEDED"
        assert store.status()["bytes"] <= 1000
    moment[0] += timedelta(seconds=1)
    store.prune()
    assert store.reserve({"fresh": 1})["status"] == "OWNER"
    assert store.status()["bytes"] <= 1000


def test_reclaimed_generation_cannot_make_old_guard_valid_again(tmp_path):
    store = CacheStore(tmp_path / "cache", clock=lambda: NOW)
    old = store.guard("unused")
    store.invalidate("unused")
    store.prune()
    new = store.guard("unused")
    assert new["generation"] == 0
    assert new != old
    with pytest.raises(CacheError, match="DEPENDENCY_CHANGED"):
        store.put(QUERY, "stale writer", guards=[old])
