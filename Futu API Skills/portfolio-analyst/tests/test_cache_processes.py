from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from portfolio_analyst.cache import CacheError, CacheStore
from test_cache_core import NOW, QUERY


def test_changed_evidence_invalidates_dependent_summary(tmp_path):
    store = CacheStore(tmp_path / "cache", clock=lambda: NOW)
    fact = store.put(QUERY, {"value": 1})
    store.put({"summary": "AAA"}, {"claim": "one"}, dependencies=[fact["ref"]])
    assert store.lookup({"summary": "AAA"})["status"] == "HIT"
    store.put(QUERY, {"value": 2})
    assert store.lookup({"summary": "AAA"})["reason"] == "DEPENDENCY_CHANGED"


def test_unchanged_discovery_can_revalidate_summary_without_rewriting_it(tmp_path):
    moment = [NOW]
    store = CacheStore(tmp_path / "cache", clock=lambda: moment[0])
    directory = store.put(QUERY, ["F1"], ttl=3600)
    guard = store.discovery_ref(directory)
    summary = store.put({"summary": "AAA"}, "unchanged thesis", dependencies=[guard])
    moment[0] += timedelta(seconds=3600)
    assert store.lookup({"summary": "AAA"})["status"] == "MISS"
    store.put(QUERY, ["F1"], ttl=3600)
    after = store.lookup({"summary": "AAA"})
    assert after["status"] == "HIT"
    assert after["ref"] == summary["ref"]
    assert after["record"]["created_at"] == NOW.isoformat()
    store.put(QUERY, ["F1", "F2"], ttl=3600)
    assert store.lookup({"summary": "AAA"})["reason"] == "DEPENDENCY_CHANGED"


def test_missing_dependency_is_not_a_hit(tmp_path):
    store = CacheStore(tmp_path / "cache", clock=lambda: NOW)
    evidence = store.put(QUERY, "body")
    store.put({"summary": 1}, "result", dependencies=[evidence["ref"]])
    (store.root / "objects" / (evidence["ref"]["content_hash"] + ".json")).unlink()
    assert store.lookup({"summary": 1})["reason"] == "DEPENDENCY_MISSING"


def test_guard_invalidates_private_result_without_deleting_history(tmp_path):
    store = CacheStore(tmp_path / "cache", scope="private", clock=lambda: NOW)
    guard = store.guard("account:synthetic")
    store.put(QUERY, "account result", guards=[guard], ttl=300)
    store.invalidate("account:synthetic")
    assert store.lookup(QUERY)["reason"] == "DEPENDENCY_CHANGED"
    assert store.lookup(QUERY, historical=True)["status"] == "HIT"


def test_expired_owner_cannot_publish_after_takeover(tmp_path):
    moment = [NOW]
    store = CacheStore(tmp_path / "cache", clock=lambda: moment[0])
    old = store.reserve(QUERY, seconds=1)["lease"]
    assert store.reserve(QUERY)["status"] == "BUSY"
    moment[0] += timedelta(seconds=1)
    new = store.reserve(QUERY)["lease"]
    with pytest.raises(CacheError, match="STALE_LEASE"):
        store.put(QUERY, "old", lease=old)
    store.put(QUERY, "new", lease=new)
    store.release(old)
    assert store.lookup(QUERY)["payload"] == "new"


def test_guard_change_during_generation_rejects_publication(tmp_path):
    store = CacheStore(tmp_path / "cache", clock=lambda: NOW)
    guard = store.guard("event")
    store.invalidate("event")
    with pytest.raises(CacheError, match="DEPENDENCY_CHANGED"):
        store.put(QUERY, "stale", guards=[guard])


def test_prune_preserves_unrelated_history_and_removes_expired_dependencies(tmp_path):
    moment = [NOW]
    store = CacheStore(tmp_path / "cache", scope="private", clock=lambda: moment[0])
    history = tmp_path / "snapshots"
    history.mkdir()
    (history / "user.json").write_text("preserve")
    store.put(QUERY, "snapshot", retention_seconds=10)
    moment[0] += timedelta(seconds=10)
    result = store.prune()
    assert result["removed_records"] == 1
    assert store.lookup(QUERY)["status"] == "MISS"
    assert (history / "user.json").read_text() == "preserve"


def test_quota_eviction_leaves_no_broken_success_receipt(tmp_path):
    moment = [NOW]
    store = CacheStore(tmp_path / "cache", max_bytes=12000, clock=lambda: moment[0])
    for i in range(8):
        moment[0] += timedelta(seconds=1)
        latest = store.put({"i": i}, "x" * 2000 + str(i))
    assert store.status()["bytes"] <= 12000
    assert store.lookup({"i": 7})["ref"] == latest["ref"]
    assert store.lookup({"i": 0})["status"] == "MISS"


def test_two_processes_share_a_single_live_acquisition(tmp_path):
    root = str(tmp_path / "cache")
    counter = str(tmp_path / "source_calls")
    code = '''
import sys,time
from pathlib import Path
from portfolio_analyst.cache import CacheStore
s=CacheStore(sys.argv[1])
def source():
    with open(sys.argv[2],"a") as f:f.write("fetch\\n")
    time.sleep(0.2)
    return {"value":42}
r=s.get_or_create({"test":"concurrent"},source,ttl=60)
assert r["payload"]=={"value":42}
'''
    children = [subprocess.Popen([sys.executable, "-B", "-c", code, root, counter],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    results = [(child, child.communicate(timeout=15)) for child in children]
    for child, (out, err) in results:
        assert child.returncode == 0, err.decode()
    assert Path(counter).read_text().splitlines() == ["fetch"]


def test_bounded_wait_returns_busy(tmp_path):
    store = CacheStore(tmp_path / "cache")
    store.reserve(QUERY)
    with pytest.raises(CacheError, match="LEASE_BUSY"):
        store.get_or_create(QUERY, lambda: "must not run", wait_seconds=0.01)


def test_two_sec_processes_share_request_slots(tmp_path):
    code = '''
import json,sys,time
from portfolio_analyst.cache import CacheStore
s=CacheStore(sys.argv[1])
s.rate_limit('SEC', interval=0.2)
print(json.dumps({'at':time.time()}))
'''
    children = [subprocess.Popen([sys.executable, '-B', '-c', code, str(tmp_path / 'cache')],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    times = []
    for child in children:
        out, err = child.communicate(timeout=15)
        assert child.returncode == 0, err.decode()
        times.append(json.loads(out)['at'])
    assert abs(times[1] - times[0]) >= 0.15


def test_cleanup_failure_cannot_replace_loader_exception(tmp_path, monkeypatch):
    from portfolio_analyst.cache import coordination
    store = CacheStore(tmp_path / 'cache')
    def fail():
        raise RuntimeError('source denied')
    monkeypatch.setattr(coordination, 'release', lambda *a: (_ for _ in ()).throw(CacheError('CORRUPT')))
    with pytest.raises(RuntimeError, match='source denied'):
        store.get_or_create(QUERY, fail)
