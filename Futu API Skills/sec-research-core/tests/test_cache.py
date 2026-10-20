from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "portfolio-analyst"))
from portfolio_analyst.cache import CacheStore
from portfolio_analyst.cache.public import PublicCache
from sec_research.cache import CacheBridge, CachedReader
from sec_research.evidence import ResearchError
from sec_research.reader import EdgarReader
from sec_research.transport import Budget

NOW = datetime(2026, 10, 20, 14, tzinfo=timezone.utc)
ACC = "0000000001-26-000001"


class DirectBridge:
    def __init__(self, store):
        self.store, self.public = store, PublicCache(store)
    def lookup(self, query, **kwargs): return self.public.lookup(query, **kwargs)
    def reserve(self, query): return self.store.reserve(query, seconds=600)
    def publish(self, query, payload, **kwargs): return self.public.publish(query, payload, **kwargs)
    def release(self, lease): return self.store.release(lease)
    def rate_limit(self): return self.store.rate_limit("SEC")


def make_reader(counts):
    class Filing:
        cik = 1
        accession_no = ACC
        filing_date = "2026-05-01"
        form = "10-K"
        homepage_url = "https://www.sec.gov/Archives/synthetic"
        report_date = "2026-03-31"
        def text(self):
            counts["text"] += 1
            return "abcdefghij"
    class Company:
        def __init__(self, cik): counts["company"] += 1
        def get_filings(self, **kwargs):
            counts["index"] += 1
            return [Filing()]
    return EdgarReader(SimpleNamespace(Company=Company), Budget())


def test_distinct_readers_reuse_whole_text_for_pagination(tmp_path):
    counts = dict(company=0, index=0, text=0)
    bridge = DirectBridge(CacheStore(tmp_path / "public", clock=lambda: NOW))
    for offset, expected in [(0, "abcd"), (4, "efgh")]:
        reader = CachedReader(make_reader(counts), bridge, clock=lambda: NOW)
        result = reader.content("1", ACC, NOW.isoformat(), offset=offset, limit=4)
        assert result["text"] == expected
    assert counts == {"company": 1, "index": 1, "text": 1}


def test_cached_filing_cannot_leak_across_knowledge_cutoff(tmp_path):
    counts = dict(company=0, index=0, text=0)
    bridge = DirectBridge(CacheStore(tmp_path / "public", clock=lambda: NOW))
    reader = CachedReader(make_reader(counts), bridge, clock=lambda: NOW)
    reader.content("1", ACC, NOW.isoformat())
    with pytest.raises(ResearchError, match="FILING_AFTER_CUTOFF"):
        reader.content("1", ACC, "2026-05-01T12:00:00Z")


def test_index_refresh_keeps_generation_when_accessions_unchanged(tmp_path):
    moment = [NOW]
    counts = dict(company=0, index=0, text=0)
    bridge = DirectBridge(CacheStore(tmp_path / "public", clock=lambda: moment[0]))
    reader = CachedReader(make_reader(counts), bridge, clock=lambda: moment[0])
    reader.index("1", ["10-K"], "2026-01-01", NOW.isoformat())
    first = reader.receipts[-1]["record"]["generation"]
    reader.index("1", ["10-K"], "2026-01-01", NOW.isoformat())
    assert counts["index"] == 1
    moment[0] += timedelta(hours=1)
    reader.index("1", ["10-K"], "2026-01-01", moment[0].isoformat())
    assert counts["index"] == 2
    assert reader.receipts[-1]["record"]["generation"] == first


def test_actual_cli_bridge_reuses_payload_without_source_sdk(tmp_path):
    counts = dict(company=0, index=0, text=0)
    bridge = CacheBridge(root=tmp_path / "public")
    cutoff = datetime.now(timezone.utc).isoformat()
    for offset in (0, 4):
        reader = CachedReader(make_reader(counts), bridge)
        reader.content("1", ACC, cutoff, offset=offset, limit=4)
    assert counts["text"] == 1


def test_cache_off_preserves_reader_behavior(tmp_path):
    from sec_research.cli import parser
    args = parser().parse_args(["filing-content", "--cik", "1", "--accession", ACC])
    assert args.cache_mode == "off"
