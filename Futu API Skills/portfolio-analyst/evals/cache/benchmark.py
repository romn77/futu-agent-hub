"""Reproducible OFFLINE cache-path measurements; no network, account, or model calls."""
import argparse
from collections import Counter
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
SEC = ROOT.parent / 'sec-research-core'
for folder in (ROOT, ROOT / 'tests', SEC, SEC / 'tests'):
    sys.path.insert(0, str(folder))
from portfolio_analyst.cache import CacheStore
from portfolio_analyst.cache.public import PublicCache
from portfolio_analyst.cache.workflow import CachedWorkflow
from portfolio_analyst.cache.types import digest
from test_cache_account import setup
from test_cache_core import NOW
from test_cache_public import QUERY, URL
from test_cache_report import REQUEST, report, coverage
from sec_research.cache import CachedReader
# SEC test module shares its basename with no analyst test.
from test_cache import DirectBridge, make_reader, ACC


def account_pair(base, options):
    cache, scope, source, moment = setup(base, options=options)
    identities = []
    def resolver(settings, ids):
        identities.append(1)
        return scope
    flow = CachedWorkflow(cache.settings, cache.store, cache.market.store,
                          resolver=resolver, invoke=source, version='offline-benchmark-v1')
    public = PublicCache(cache.market.store)
    evidence = None
    records = []
    for phase in ('cold', 'hot'):
        previous, discovery_count = len(source.calls), len(identities)
        started = time.perf_counter()
        packet = flow.prepare(REQUEST)
        prepare_seconds = time.perf_counter() - started
        generated, public_reads = 0, 0
        if packet['route'] == 'components_ready':
            if evidence is None:
                discovery = public.publish({**QUERY, 'dataset': 'news', 'parameters': {'window':'recent'}},
                                           {'observations': []}, source_url=URL, source_as_of=moment[0], available_at=moment[0])
                evidence = public.publish(QUERY, {'observations':[{'text':'synthetic public fact'}]},
                                          source_url=URL, source_as_of=moment[0], available_at=moment[0],
                                          dependencies=[public.store.discovery_ref(discovery)])
                public_reads = 2
            else:
                assert public.lookup(QUERY)['status'] == 'HIT'
            snapshot = packet['envelope']['snapshot']
            generated = 1
            markdown = report(snapshot)
            if options:
                markdown = markdown.replace('## 宏观影响', '## 期权风险\nquote_freshness=UNVERIFIED\n\n## 宏观影响')
            result = flow.finalize(REQUEST, packet['context'], markdown, coverage(snapshot), [evidence['ref']])
        else:
            result = packet
        records.append({'phase':phase, 'route':result['route'], 'seconds':time.perf_counter()-started,
                        'prepare_seconds':prepare_seconds, 'identity_discoveries':len(identities)-discovery_count,
                        'source_methods':dict(Counter(source.calls[previous:])), 'public_fixture_reads':public_reads,
                        'synthetic_report_renders':generated, 'report_digest':digest(result['markdown']),
                        'snapshot_as_of':moment[0].isoformat(), 'extra_refreshes':packet.get('refresh_attempt',0),
                        'http_requests':None, 'model_input_bytes':None, 'model_input_tokens':None,
                        'model_output_tokens':None, 'measurement_scope':'offline production workflow + synthetic source/renderer'})
    assert records[0]['report_digest'] == records[1]['report_digest']
    if not options:
        assert records[1]['route'] == 'report_hit' and records[1]['source_methods'] == {}
    else:
        assert records[1]['source_methods'] == {'options':1} and records[1]['route'] == 'report_uncached'
    return records


def sec_pair(base):
    counts = dict(company=0,index=0,text=0)
    bridge = DirectBridge(CacheStore(base / 'public', clock=lambda: NOW))
    records = []
    for phase in ('cold','hot'):
        previous = dict(counts)
        started = time.perf_counter()
        reader = CachedReader(make_reader(counts), bridge, clock=lambda: NOW)
        payload = reader.content('1', ACC, NOW.isoformat(), offset=0, limit=4)
        records.append({'phase':phase, 'seconds':time.perf_counter()-started,
                        'source_methods':{key:counts[key]-previous[key] for key in counts},
                        'payload_digest':digest(payload), 'returned_text_bytes':len(payload['text'].encode()),
                        'cache_receipts':len(reader.receipts), 'diagnostics':reader.diagnostics,
                        'http_requests':None, 'model_input_bytes':None, 'model_input_tokens':None,
                        'model_output_tokens':None, 'measurement_scope':'offline SEC adapter + synthetic source'})
    assert records[1]['source_methods'] == {'company':0,'index':0,'text':0}
    assert records[0]['payload_digest'] == records[1]['payload_digest']
    return records


def measure(pairs=5):
    cases = {}
    for name in ('stocks','options','sec'):
        rows = []
        for _ in range(pairs):
            with tempfile.TemporaryDirectory(prefix='cache-benchmark-') as temp:
                base = Path(temp).resolve()
                rows.append(sec_pair(base) if name == 'sec' else account_pair(base, name == 'options'))
        cases[name] = {'pairs':rows, 'cold_median_seconds':statistics.median(row[0]['seconds'] for row in rows),
                       'hot_median_seconds':statistics.median(row[1]['seconds'] for row in rows)}
    source_hash = hashlib.sha256(b''.join(path.read_bytes() for path in sorted((ROOT/'portfolio_analyst/cache').glob('*.py')))).hexdigest()
    return {'schema_version':1, 'mode':'OFFLINE_SYNTHETIC', 'controlled_time':NOW.isoformat(),
            'cache_source_sha256':source_hash, 'cases':cases,
            'real_network_performance':'NOT_RUN', 'host_model_tokens':'NOT_RUN',
            'reason':'No real account, source network or LLM was invoked; local wall time is not end-to-end host performance.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    value = measure()
    if args.output:
        args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({name:{key:value for key,value in data.items() if key!='pairs'}
                      for name,data in value['cases'].items()}, indent=2))
