from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from portfolio_analyst.cache import CacheError, CacheStore
from portfolio_analyst.cache.public import PublicCache
from test_cache_core import NOW


QUERY = {"provider": "issuer", "interface": "filing", "version": "1",
         "subject": "US.AAA", "dataset": "sec.content", "parameters": {"accession": "F1"}}
URL = "https://www.sec.gov/Archives/example"


def test_public_evidence_preserves_provenance_across_lookup(tmp_path):
    cache = PublicCache(CacheStore(tmp_path / "public", clock=lambda: NOW))
    result = cache.publish(QUERY, {"text": "public operating facts"}, source_url=URL,
                           source_as_of=NOW, available_at=NOW)
    hit = cache.lookup(QUERY, cutoff=NOW)
    assert hit["status"] == "HIT"
    assert hit["ref"] == result["ref"]
    assert hit["record"]["metadata"]["source_url"] == URL
    assert cache.lookup(QUERY, cutoff=NOW - timedelta(seconds=1))["reason"] == "AFTER_CUTOFF"


@pytest.mark.parametrize("payload", [
    {"account_ref": "acct-secret"}, {"nested": {"acc_id": "12345"}},
    {"text": "我的账户净值是100万美元"}, {"source_snapshot_id": "ps-private"},
])
def test_private_content_is_rejected_from_public_records(tmp_path, payload):
    cache = PublicCache(CacheStore(tmp_path / "public", clock=lambda: NOW))
    with pytest.raises(CacheError, match="PRIVATE_CONTENT"):
        cache.publish(QUERY, payload, source_url=URL, source_as_of=NOW)


def test_source_failures_and_unknown_dynamic_times_are_not_successes(tmp_path):
    cache = PublicCache(CacheStore(tmp_path / "public", clock=lambda: NOW))
    with pytest.raises(CacheError, match="SOURCE_UNAVAILABLE"):
        cache.publish(QUERY, {"success": False, "error": "denied"}, source_url=URL, source_as_of=NOW)
    with pytest.raises(CacheError, match="SOURCE_TIME_REQUIRED"):
        cache.publish({**QUERY, "dataset": "estimates"}, {"eps": 1}, source_url=URL)


def test_partial_evidence_never_becomes_complete(tmp_path):
    cache = PublicCache(CacheStore(tmp_path / "public", clock=lambda: NOW))
    cache.publish(QUERY, {"rows": [], "truncated": True}, source_url=URL,
                  source_as_of=NOW, complete=False)
    assert cache.lookup(QUERY)["status"] == "HIT_PARTIAL"


def test_summary_requires_real_dependencies_and_retains_counterevidence(tmp_path):
    cache = PublicCache(CacheStore(tmp_path / "public", clock=lambda: NOW))
    fact = cache.publish(QUERY, {"facts": [1, 2]}, source_url=URL, source_as_of=NOW)
    summary = {"question": "cash conversion", "subject": "US.AAA", "period": "2026Q2",
               "facts": ["sales increased"], "inferences": ["growth may persist"],
               "scenarios": [], "supporting_refs": [fact["ref"]], "contrary_refs": [fact["ref"]],
               "limitations": ["one segment not disclosed"], "follow_up": ["next filing"],
               "method_version": "cash-v1", "model": "test", "calculations": []}
    query = {**QUERY, "dataset": "summary", "parameters": {"question": "cash conversion", "method": "cash-v1"}}
    cache.publish_summary(query, summary, dependencies=[fact["ref"]])
    assert cache.lookup(query)["payload"] == summary
    cache.store.put(QUERY, {"facts": [3]}, source_as_of=NOW)
    assert cache.lookup(query)["status"] == "MISS"


def test_public_cli_lookup_is_available_in_a_new_process(tmp_path):
    root = tmp_path / "cache"
    cache = PublicCache(CacheStore(root))
    cache.publish(QUERY, {"text": "fact"}, source_url=URL)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"query": QUERY}))
    script = Path(__file__).parents[1] / "scripts/cache.py"
    result = subprocess.run([sys.executable, "-B", str(script), "lookup", "--request", str(request), "--json"],
                            env={**os.environ, "RESEARCH_CACHE_HOME": str(root)},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "HIT"
