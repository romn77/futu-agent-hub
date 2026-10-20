from datetime import timedelta
from decimal import Decimal

import pytest

from portfolio_analyst.cache import CacheError
from portfolio_analyst.cache.public import PublicCache
from portfolio_analyst.cache.workflow import CachedWorkflow
from test_cache_account import setup
from test_cache_public import QUERY, URL


REQUEST = {"question": "分析当前组合", "preferences": {}, "language": "zh-CN"}
HEADINGS = ["范围与数据时间", "一句话结论", "组合概览", "三个主要风险", "完整持仓表",
            "重点研究", "宏观影响", "观察", "数据缺口与来源"]


def report(snapshot):
    table = "| Symbol | Current weight | Market value | Unrealized P&L | Sector/theme | Short comment |\n|---|---:|---:|---:|---|---|\n"
    for item in snapshot["positions"]:
        if Decimal(item["quantity"]) != 0:
            table += (f'| {item["market"]}.{item["symbol"]} | {Decimal(item["weight"])*100}% | '
                      f'{item["market_value_base"]} | {item["unrealized_pl_base"] or "Unknown"} | Unknown | Review |\n')
    return "\n\n".join("## " + name + "\n" + (
        snapshot["snapshot_id"] + " " + snapshot["as_of"] if name == "范围与数据时间" else
        table if name == "完整持仓表" else "Public evidence " + URL
    ) for name in HEADINGS)


def coverage(snapshot):
    return {"holdings": sorted({p["market"] + "." + p["symbol"] for p in snapshot["positions"]
                                if Decimal(p["quantity"]) != 0}),
            "research_complete": True, "open_required_items": []}


def workflow(tmp_path):
    account, scope, source, moment = setup(tmp_path)
    identities = []
    def resolve(settings, ids):
        identities.append("discovery")
        return scope
    flow = CachedWorkflow(account.settings, account.store, account.market.store,
                          resolver=resolve, invoke=source, version="test-v1")
    public = PublicCache(account.market.store)
    discovery = public.publish({**QUERY, "dataset": "news", "parameters": {"window": "recent"}},
                               {"observations": []}, source_url=URL, source_as_of=moment[0], available_at=moment[0])
    evidence = public.publish(QUERY, {"observations": [{"text": "public facts"}]}, source_url=URL,
                              source_as_of=moment[0], available_at=moment[0],
                              dependencies=[public.store.discovery_ref(discovery)])
    return flow, scope, source, moment, evidence["ref"], identities


def test_complete_report_replay_skips_all_analysis_but_checks_identity(tmp_path):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    first = flow.prepare(REQUEST)
    snap = first["envelope"]["snapshot"]
    published = flow.finalize(REQUEST, first["context"], report(snap), coverage(snap), [ref])
    assert published["route"] == "report_published"
    moment[0] += timedelta(seconds=180)
    hit = flow.prepare(REQUEST)
    assert hit["route"] == "report_hit"
    assert hit["markdown"] == report(snap)
    assert source.calls == ["snapshot", "risk", "technical"]
    assert len(identities) == 3


def test_new_question_misses_report_but_reuses_account(tmp_path):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    first = flow.prepare(REQUEST)
    snap = first["envelope"]["snapshot"]
    flow.finalize(REQUEST, first["context"], report(snap), coverage(snap), [ref])
    next_result = flow.prepare({**REQUEST, "question": "检验现金流论点"})
    assert next_result["route"] == "components_ready"
    assert source.calls == ["snapshot", "risk", "technical"]


def test_report_cannot_extend_snapshot_deadline(tmp_path):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    first = flow.prepare(REQUEST)
    snap = first["envelope"]["snapshot"]
    moment[0] += timedelta(seconds=120)
    flow.finalize(REQUEST, first["context"], report(snap), coverage(snap), [ref])
    moment[0] += timedelta(seconds=180)
    result = flow.prepare(REQUEST)
    assert result["route"] == "components_ready"
    assert source.calls.count("snapshot") == 2


def test_context_cannot_be_forged_or_used_for_different_request(tmp_path):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    first = flow.prepare(REQUEST)
    snap = first["envelope"]["snapshot"]
    with pytest.raises(CacheError, match="CONTEXT_INVALID"):
        flow.finalize(REQUEST, {**first["context"], "token": "wrong"}, report(snap), coverage(snap), [ref])
    with pytest.raises(CacheError, match="CONTEXT_INVALID"):
        flow.finalize({**REQUEST, "preferences": {"new": True}}, first["context"], report(snap), coverage(snap), [ref])


def test_long_generation_has_at_most_one_additional_refresh(tmp_path):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    first = flow.prepare(REQUEST)
    snap = first["envelope"]["snapshot"]
    moment[0] += timedelta(seconds=360)
    refreshed = flow.finalize(REQUEST, first["context"], report(snap), coverage(snap), [ref])
    assert refreshed["route"] == "components_ready"
    assert refreshed["refresh_attempt"] == 1
    assert source.calls.count("snapshot") == 2
    moment[0] += timedelta(seconds=360)
    new_snap = refreshed["envelope"]["snapshot"]
    final = flow.finalize(REQUEST, refreshed["context"], report(new_snap), coverage(new_snap), [ref])
    assert final["route"] == "historical_only"
    assert source.calls.count("snapshot") == 2


def test_missing_holdings_cells_and_incomplete_research_are_not_cached(tmp_path):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    first = flow.prepare(REQUEST)
    snap = first["envelope"]["snapshot"]
    with pytest.raises(CacheError, match="REPORT_INVALID"):
        flow.finalize(REQUEST, first["context"], report(snap).replace("| US.AAA |", "| US.OTHER |"), coverage(snap), [ref])
    value = flow.finalize(REQUEST, first["context"], report(snap),
                          {**coverage(snap), "open_required_items": ["missing thesis evidence"]}, [ref])
    assert value["route"] == "report_uncached"
    assert flow.prepare(REQUEST)["route"] == "components_ready"


def test_event_during_generation_rejects_stale_report(tmp_path):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    first = flow.prepare(REQUEST)
    snap = first["envelope"]["snapshot"]
    flow.private.invalidate("account:" + scope.key)
    result = flow.finalize(REQUEST, first["context"], report(snap), coverage(snap), [ref])
    assert result["route"] == "components_ready"
    assert source.calls.count("snapshot") == 2


def test_historical_mode_never_fabricates_missing_snapshot(tmp_path):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    with pytest.raises(CacheError, match="HISTORICAL_SNAPSHOT_UNAVAILABLE"):
        flow.prepare({**REQUEST, "analysis_mode": "historical", "snapshot_id": "missing"})
    assert source.calls == []


def test_no_discovery_receipt_prevents_current_report_publication(tmp_path):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    raw = PublicCache(flow.public).publish({**QUERY, "parameters": {"section": "other"}},
                                         {"observations": [{"text": "facts"}]}, source_url=URL,
                                         source_as_of=moment[0], available_at=moment[0])
    first = flow.prepare(REQUEST)
    snap = first["envelope"]["snapshot"]
    result = flow.finalize(REQUEST, first["context"], report(snap), coverage(snap), [raw["ref"]])
    assert result["route"] == "report_uncached"
    assert result["reason"] == "DISCOVERY_REQUIRED"


def test_known_sec_event_cannot_be_forgotten_by_the_next_request(tmp_path):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    first = flow.prepare(REQUEST)
    snap = first["envelope"]["snapshot"]
    flow.finalize(REQUEST, first["context"], report(snap), coverage(snap), [ref])
    with_event = flow.prepare({**REQUEST, "events": [{"type": "sec_filing", "cik": "1234"}]})
    assert with_event["route"] == "components_ready"
    flow.abort(with_event["context"])
    assert flow.prepare(REQUEST)["route"] != "report_hit"


def test_report_write_failure_keeps_valid_markdown(tmp_path, monkeypatch):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    first = flow.prepare(REQUEST)
    snap = first["envelope"]["snapshot"]
    original = flow.private.put
    def unavailable(query, payload, **kwargs):
        if kwargs.get("kind") == "portfolio_report":
            raise CacheError("CAPACITY_EXCEEDED")
        return original(query, payload, **kwargs)
    monkeypatch.setattr(flow.private, "put", unavailable)
    result = flow.finalize(REQUEST, first["context"], report(snap), coverage(snap), [ref])
    assert result["route"] == "report_uncached"
    assert result["markdown"] == report(snap)


def test_report_reservation_failure_keeps_valid_markdown(tmp_path, monkeypatch):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    first = flow.prepare(REQUEST)
    snap = first["envelope"]["snapshot"]
    monkeypatch.setattr(flow.private, "reserve", lambda *args, **kwargs: (_ for _ in ()).throw(CacheError("CORRUPT")))
    result = flow.finalize(REQUEST, first["context"], report(snap), coverage(snap), [ref])
    assert result["route"] == "report_uncached"
    assert result["markdown"] == report(snap)


def test_private_reference_is_a_hard_research_scope_error(tmp_path):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    first = flow.prepare(REQUEST)
    snap = first["envelope"]["snapshot"]
    with pytest.raises(CacheError, match="SCOPE_CHANGED"):
        flow.finalize(REQUEST, first["context"], report(snap), coverage(snap), [first["context"]["ref"]])


def test_guard_failure_after_success_does_not_refetch_source(tmp_path, monkeypatch):
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    original = flow.private.guard
    def broken(tag):
        if source.calls:
            raise CacheError("CORRUPT")
        return original(tag)
    monkeypatch.setattr(flow.private, "guard", broken)
    result = flow.prepare(REQUEST)
    assert result["route"] == "components_ready"
    assert source.calls == ["snapshot", "risk", "technical"]


def test_release_error_preserves_original_source_failure(tmp_path, monkeypatch):
    from portfolio_analyst.current_analysis import CurrentAnalysisError
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    def fail(args):
        source.calls.append(args[0])
        raise CurrentAnalysisError("source failed")
    flow.invoke = fail
    monkeypatch.setattr(flow.private, "release", lambda *args: (_ for _ in ()).throw(CacheError("CORRUPT")))
    with pytest.raises(CurrentAnalysisError, match="source failed"):
        flow.prepare(REQUEST)
    assert source.calls == ["snapshot"]


def test_latest_refreshes_dynamic_market_inputs_but_reuses_them_in_request(tmp_path):
    from datetime import date
    from portfolio_analyst.price_history import FutuAdjustedPriceProvider
    from portfolio_analyst.technical import FutuTechnicalHistoryProvider
    from test_cache_market import Quotes
    flow, scope, source, moment, ref, identities = workflow(tmp_path)
    quotes = Quotes()
    def invoke(args):
        if args[0] == 'risk':
            FutuAdjustedPriceProvider(quotes).history('AAA', market='US', lookback=120, as_of=date(2026,10,19))
        if args[0] == 'technical':
            FutuTechnicalHistoryProvider(quotes).bars('AAA', market='US', interval='1d', as_of=date(2026,10,19))
        return source(args)
    flow.invoke = invoke
    first = flow.prepare(REQUEST)
    flow.abort(first['context'])
    assert len(quotes.calls) == 1
    moment[0] += timedelta(seconds=1)
    second = flow.prepare({**REQUEST, 'freshness_requirement': 'latest'})
    assert len(quotes.calls) == 2
    assert source.calls.count('risk') == 2
    flow.abort(second['context'])


def test_off_workflow_creates_no_cache_or_identity_key(tmp_path):
    cache, scope, source, moment = setup(tmp_path)
    flow = CachedWorkflow(cache.settings, cache.store, cache.market.store, invoke=source,
                          resolver=lambda *a: (_ for _ in ()).throw(AssertionError('off must not resolve cached identity')),
                          version='off-test')
    result = flow.prepare({**REQUEST, 'cache_mode':'off'})
    assert result['context'] is None and result['research_cache_mode'] == 'off'
    assert source.calls == ['snapshot','risk','technical']
    assert not cache.store.root.exists() and not cache.market.store.root.exists()
    assert not list(cache.settings.data_home.rglob('*'))
