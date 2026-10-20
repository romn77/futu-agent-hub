import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from portfolio_analyst.cache import CacheError, CacheStore


NOW = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
QUERY = {"source": "synthetic", "object": "AAA", "dataset": "filing"}


def test_second_process_reads_original_evidence(tmp_path):
    root = tmp_path / "cache"
    store = CacheStore(root, clock=lambda: NOW)
    receipt = store.put(QUERY, {"fact": "100", "unknown": None}, source_as_of=NOW, ttl=300)
    script = """
import json,sys
from datetime import datetime
from portfolio_analyst.cache import CacheStore
s=CacheStore(sys.argv[1],clock=lambda:datetime.fromisoformat(sys.argv[2]))
print(json.dumps(s.lookup(json.loads(sys.argv[3]))))
"""
    child = subprocess.run(
        [sys.executable, "-B", "-c", script, str(root), NOW.isoformat(), json.dumps(QUERY)],
        capture_output=True, text=True, check=True,
    )
    result = json.loads(child.stdout)
    assert result["status"] == "HIT"
    assert result["payload"] == {"fact": "100", "unknown": None}
    assert result["ref"] == receipt["ref"]
    assert result["record"]["source_as_of"] == NOW.isoformat()


def test_expiry_is_exact_and_reads_do_not_renew(tmp_path):
    moment = [NOW]
    store = CacheStore(tmp_path / "cache", clock=lambda: moment[0])
    store.put(QUERY, "body", source_as_of=NOW, ttl=300)
    for offset in [1, 100, 299]:
        moment[0] = NOW + timedelta(seconds=offset)
        assert store.lookup(QUERY)["status"] == "HIT"
    moment[0] = NOW + timedelta(seconds=300)
    assert store.lookup(QUERY)["reason"] == "EXPIRED"
    assert store.lookup(QUERY, historical=True)["status"] == "HIT"


def test_disabled_cache_never_creates_directory(tmp_path):
    root = tmp_path / "missing"
    store = CacheStore(root, enabled=False, clock=lambda: NOW)
    assert store.lookup(QUERY)["status"] == "BYPASS"
    assert store.put(QUERY, "body", source_as_of=NOW)["status"] == "BYPASS"
    assert not root.exists()


def test_future_and_rollback_cannot_be_current_hits(tmp_path):
    moment = [NOW]
    store = CacheStore(tmp_path / "cache", clock=lambda: moment[0])
    with pytest.raises(CacheError, match="CLOCK_INVALID"):
        store.put(QUERY, "body", source_as_of=NOW + timedelta(seconds=1))
    store.put(QUERY, "body", source_as_of=NOW)
    moment[0] = NOW - timedelta(seconds=1)
    assert store.lookup(QUERY)["reason"] == "CLOCK_INVALID"


def test_rejects_symlink_root_and_non_private_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(CacheError, match="UNSAFE_PATH"):
        CacheStore(link).put(QUERY, "body")
    target.chmod(0o755)
    with pytest.raises(CacheError, match="UNSAFE_PATH"):
        CacheStore(target).put(QUERY, "body")


def test_corrupt_payload_and_symlink_payload_are_never_hits(tmp_path):
    store = CacheStore(tmp_path / "cache", clock=lambda: NOW)
    receipt = store.put(QUERY, "body", source_as_of=NOW)
    path = store.root / "objects" / (receipt["record"]["content_hash"] + ".json")
    path.write_text('"changed"')
    assert store.lookup(QUERY)["reason"] == "CORRUPT"
    path.unlink()
    unrelated = tmp_path / "unrelated"
    unrelated.write_text('"body"')
    path.symlink_to(unrelated)
    assert store.lookup(QUERY)["reason"] == "UNSAFE_PATH"
    assert unrelated.read_text() == '"body"'


def test_oversized_payload_is_not_truncated_or_published(tmp_path):
    store = CacheStore(tmp_path / "cache", max_item_bytes=10)
    with pytest.raises(CacheError, match="TOO_LARGE"):
        store.put(QUERY, "x" * 11)
    assert store.lookup(QUERY)["status"] == "MISS"


def test_private_modes_and_no_unknown_value_coercion(tmp_path):
    store = CacheStore(tmp_path / "cache", clock=lambda: NOW)
    store.put(QUERY, {"zero": 0, "unknown": None, "empty": []}, source_as_of=NOW)
    assert store.lookup(QUERY)["payload"] == {"zero": 0, "unknown": None, "empty": []}
    for path in store.root.rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


def test_rollback_after_a_successful_read_does_not_rewind_last_access(tmp_path):
    moment = [NOW]
    store = CacheStore(tmp_path / 'cache', clock=lambda: moment[0])
    store.put(QUERY, 'body', ttl=300)
    moment[0] += timedelta(seconds=100)
    assert store.lookup(QUERY)['status'] == 'HIT'
    moment[0] -= timedelta(seconds=50)
    assert store.lookup(QUERY)['reason'] == 'CLOCK_INVALID'
    assert store.lookup(QUERY, historical=True)['status'] == 'HIT'
    assert store.lookup(QUERY)['reason'] == 'CLOCK_INVALID'
