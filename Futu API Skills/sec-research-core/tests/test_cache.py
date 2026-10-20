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
    def guard(self, tag): return self.store.guard(tag)


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


def test_normalized_13f_payload_is_reused_without_losing_units_or_rows(tmp_path):
    from sec_research.reader import normalize_13f
    from test_reader import obj, metadata
    calls = []
    document = normalize_13f(obj(), metadata(), "<edgarSubmission/>")
    def holdings(*args):
        calls.append(args)
        return document
    raw = SimpleNamespace(budget=Budget(), holdings=holdings)
    bridge = DirectBridge(CacheStore(tmp_path / "public", clock=lambda: NOW))
    for _ in range(2):
        result = CachedReader(raw, bridge, clock=lambda: NOW).holdings("1", ACC, NOW.isoformat())
        assert result["value_unit"] == "USD"
        assert result["rows"] == document["rows"]
    assert len(calls) == 1


def test_historical_index_cache_does_not_cover_a_later_requested_period(tmp_path):
    calls = []
    def index(identifier, forms, start, cutoff, **kwargs):
        calls.append(cutoff)
        return {"filings": [], "complete": True, "issues": [], "time_precision": "date",
                "knowledge_cutoff": cutoff}
    reader = SimpleNamespace(budget=Budget(), index=index)
    bridge = DirectBridge(CacheStore(tmp_path / "public", clock=lambda: NOW))
    cached = CachedReader(reader, bridge, clock=lambda: NOW)
    cached.index("1", ["10-K"], "2026-01-01", "2026-06-01T00:00:00Z")
    result = cached.index("1", ["10-K"], "2026-01-01", NOW.isoformat())
    assert len(calls) == 2
    assert result["knowledge_cutoff"] == NOW.isoformat()


def test_index_coverage_uses_disclosure_timezone_across_eastern_midnight(tmp_path):
    calls = []
    def index(identifier, forms, start, cutoff, **kwargs):
        calls.append(cutoff)
        return {'filings': [], 'complete': True, 'knowledge_cutoff': cutoff}
    reader = CachedReader(SimpleNamespace(budget=Budget(), index=index),
                          DirectBridge(CacheStore(tmp_path / 'public', clock=lambda: NOW)), clock=lambda: NOW)
    reader.index('1', ['10-K'], '2026-01-01', '2026-10-20T02:00:00Z')
    reader.index('1', ['10-K'], '2026-01-01', '2026-10-20T14:00:00Z')
    assert len(calls) == 2


def test_collect_expansion_fetches_only_missing_manager_period_filings(tmp_path):
    from copy import deepcopy
    from sec_research.cli import collect
    from sec_research.universe import Universe
    from test_signals import scenario
    universe, documents, enrichment = scenario(2)
    calls = []
    for doc in documents:
        doc['manager_cik'] = doc['manager_cik'].zfill(10)
        doc['retrieved_at'] = NOW.isoformat()
    def index(identifier, forms, start, cutoff, **kwargs):
        return {'filings': [{key: doc[key] for key in
                            ('manager_cik','accession','form','filed','source_url','retrieved_at','report_period')}
                           for doc in documents if doc['manager_cik'] == identifier.zfill(10)],
                'complete': True, 'knowledge_cutoff': cutoff}
    def holdings(identifier, accession, cutoff):
        calls.append((identifier, accession))
        return deepcopy(next(doc for doc in documents if doc['accession'] == accession))
    raw = SimpleNamespace(budget=Budget(), index=index, holdings=holdings)
    reader = CachedReader(raw, DirectBridge(CacheStore(tmp_path / 'public', clock=lambda: NOW)),
                          clock=lambda: NOW, strict_index=True)
    one = Universe.from_mapping({'schema_version': 1, 'universe_id': 'one', 'managers':[{'cik':'1'}]})
    first = collect(reader, one, '2026-06-30', NOW.isoformat(), 1, 100)
    second = collect(reader, one, '2026-06-30', NOW.isoformat(), 2, 100)
    expanded = collect(reader, universe, '2026-06-30', NOW.isoformat(), 2, 100)
    assert len(first['filings']) == 1 and len(second['filings']) == 2 and len(expanded['filings']) == 4
    assert len(calls) == len(set(calls)) == 4
    assert not reader.diagnostics


def test_same_universe_recomputes_changed_classification_without_mutating_filings():
    from copy import deepcopy
    from test_signals import scenario, run
    universe, documents, enrichment = scenario()
    original = deepcopy(documents)
    before = run(universe, documents, enrichment)
    for item in enrichment['classifications'].values():
        if item['industry'] == 'Utilities':
            item['industry'] = 'Energy'
    enrichment['classification_version'] = 'revised-v2'
    after = run(universe, documents, enrichment)
    assert 'Utilities' in {s['label'] for s in before['signals']}
    assert 'Utilities' not in {s['label'] for s in after['signals']}
    assert 'Energy' in {s['label'] for s in after['signals']}
    assert documents == original


def test_two_sec_client_processes_reuse_cached_filing_pagination(tmp_path):
    import json
    import os
    import subprocess
    code = '''
import json,sys
from pathlib import Path
from sec_research.cache import CacheBridge,CachedReader
from test_cache import make_reader,ACC,NOW
counts=dict(company=0,index=0,text=0)
r=CachedReader(make_reader(counts),CacheBridge(root=Path(sys.argv[1])))
result=r.content('1',ACC,NOW.isoformat(),offset=int(sys.argv[2]),limit=4)
print(json.dumps({'counts':counts,'text':result['text'],'diagnostics':r.diagnostics}))
'''
    root = Path(__file__).parents[1]
    env = {**os.environ,'PYTHONPATH':os.pathsep.join((str(root),str(root/'tests'),str(root.parent/'portfolio-analyst')))}
    receipts = []
    for offset in ('0','4'):
        value = subprocess.run([sys.executable,'-B','-c',code,str(tmp_path/'public'),offset],
                               capture_output=True,text=True,env=env,check=True)
        receipts.append(json.loads(value.stdout))
    assert receipts[0]['counts']['text'] == 1 and receipts[0]['text'] == 'abcd'
    assert receipts[1]['counts'] == {'company':0,'index':0,'text':0}
    assert receipts[1]['text'] == 'efgh' and receipts[1]['diagnostics'] == []
