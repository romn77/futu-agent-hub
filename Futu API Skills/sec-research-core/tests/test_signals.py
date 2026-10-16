from sec_research.holdings import effective_portfolio
from sec_research.signals import analyze
from sec_research.universe import Universe
from sample_data import filing, row


def scenario(count=5):
    managers, documents = [], []
    for i in range(1, count + 1):
        managers.append({"cik": str(i), "family_id": f"family-{i}",
                         "relationship_source": "fixture://independent"})
        for period, filed, a, b, suffix in [
            ("2026-03-31", "2026-05-01", 100, 100, 1),
            ("2026-06-30", "2026-08-01", 120, 80, 2),
        ]:
            documents.append(filing([row(quantity=str(a), value=str(a * 10)),
                                     row("000000002", str(b), str(b * 10), row_id="2")],
                                    manager_cik=str(i), accession=f"{i:010}-26-{suffix:06}",
                                    report_period=period, filed=filed))
    universe = Universe.from_mapping({"schema_version": 1, "universe_id": "synthetic",
                                     "managers": managers})
    portfolio = effective_portfolio(documents, "1", "2026-03-31", "2026-08-31T23:59:59Z")
    classification = {r.security_id: {"industry": label, "source": "fixture://industry",
                                      "available_at": "2026-08-01", "as_of": "2026-06-30"}
                      for r, label in zip(portfolio.rows, ["Utilities", "Technology"])}
    enrichment = {"classification_version": "synthetic-v1", "classifications": classification,
                  "intervals": {"2026-03-31:2026-06-30": {
                      "action_check": {"verified": True, "source": "fixture://actions",
                                       "available_at": "2026-08-01", "start": "2026-03-31",
                                       "end": "2026-06-30"}}}}
    return universe, documents, enrichment


def run(universe, documents, enrichment):
    return analyze(universe, documents, "2026-06-30", "2026-08-31T23:59:59Z",
                   quarters=2, enrichment=enrichment)


def test_full_synthetic_pool_produces_evidenced_research_leads():
    result = run(*scenario())
    utilities = next(s for s in result["signals"] if s["label"] == "Utilities")
    assert utilities["status"] == "RESEARCH_LEAD"
    assert utilities["increase_count"] == 5
    assert utilities["net_breadth"] == 1
    assert utilities["median_shift_bps"] == 1000
    assert utilities["consecutive_intervals"] == 1
    assert utilities["evidence_refs"]


def test_small_user_pool_still_reports_observations():
    result = run(*scenario(2))
    assert len(result["universe"]["managers"]) == 2
    assert {s["status"] for s in result["signals"]} == {"OBSERVATION"}


def test_same_family_is_not_multiple_independent_votes():
    universe, documents, enrichment = scenario()
    data = {"schema_version": 1, "universe_id": "synthetic",
            "managers": [{"cik": m.cik, "family_id": "same",
                           "relationship_source": "fixture://overlap"} for m in universe.managers]}
    result = run(Universe.from_mapping(data), documents, enrichment)
    assert all(s["status"] != "RESEARCH_LEAD" for s in result["signals"])
    assert any(d["code"] == "OVERLAPPING_MANAGERS" for d in result["diagnostics"])


def test_missing_latest_filing_is_not_flat_or_exit():
    universe, documents, enrichment = scenario()
    documents = [d for d in documents if not
                 (d["manager_cik"] == "5" and d["report_period"] == "2026-06-30")]
    result = run(universe, documents, enrichment)
    assert all(s["eligible_count"] == 4 for s in result["signals"])
    assert all(s["status"] == "OBSERVATION" for s in result["signals"])


def test_future_or_absent_classification_does_not_invent_style():
    universe, documents, enrichment = scenario()
    for value in enrichment["classifications"].values():
        value["available_at"] = "2026-09-01"
    result = run(universe, documents, enrichment)
    assert result["signals"] == []
    assert result["comparisons"]


def test_disclosed_shared_manager_overrides_configured_independence():
    universe, documents, enrichment = scenario()
    for document in documents:
        document["other_included_managers"] = [{"cik": "999", "file_number": "028-99999"}]
    result = run(universe, documents, enrichment)
    assert all(s["status"] != "RESEARCH_LEAD" for s in result["signals"])
    assert any(d["code"] == "OVERLAPPING_MANAGERS" for d in result["diagnostics"])


def test_style_method_must_exist_by_cutoff():
    universe, documents, enrichment = scenario()
    for value in enrichment["classifications"].values():
        value["style"] = "Growth" if value["industry"] == "Utilities" else "Value"
    enrichment["style_method"] = {"metrics": ["PE"], "universe": "synthetic",
                                  "buckets": "quartiles", "source": "fixture://method",
                                  "as_of": "2026-06-30", "available_at": "2026-09-15"}
    result = run(universe, documents, enrichment)
    assert not any(s["dimension"] == "style" for s in result["signals"])


def test_unmapped_included_manager_file_number_is_unresolved():
    universe, documents, enrichment = scenario()
    for document in documents:
        if document["manager_cik"] == "1":
            document["other_included_managers"] = [{"file_number": "028-00002", "sequence_number": 1}]
            for r in document["rows"]:
                r["other_managers"] = ["1"]
    result = run(universe, documents, enrichment)
    assert all(s["status"] != "RESEARCH_LEAD" for s in result["signals"])
    assert any(d["code"] == "MANAGER_RELATIONSHIP_UNRESOLVED" for d in result["diagnostics"])


def test_signal_identity_includes_lookback_scope():
    universe, documents, enrichment = scenario()
    short = run(universe, documents, enrichment)
    long = analyze(universe, documents, "2026-06-30", "2026-08-31T23:59:59Z",
                   quarters=4, enrichment=enrichment)
    assert short["signals"][0]["signal_id"] != long["signals"][0]["signal_id"]
    assert long["signals"][0]["analysis_scope"]["quarters"] == 4


def test_classification_coverage_is_auditable():
    signal = run(*scenario())["signals"][0]
    assert signal["classification_coverage"][0]["previous_ratio"] == 1
    assert signal["classification_coverage"][0]["current_denominator_usd"] == 2000


def test_mixed_direction_does_not_assign_arbitrary_supporters():
    universe, documents, enrichment = scenario()
    for document in documents:
        if document["report_period"] == "2026-06-30" and document["manager_cik"] in {"3", "4", "5"}:
            a = 100 if document["manager_cik"] == "5" else 80
            document["rows"] = [row(quantity=str(a), value=str(a * 10)),
                                row("000000002", str(200 - a), str((200 - a) * 10), row_id="2")]
    signal = run(universe, documents, enrichment)["signals"][0]
    assert signal["direction"] == "MIXED"
    assert signal["supporting_managers"] == signal["opposing_managers"] == []


def test_persistence_requires_one_qualified_common_sample():
    universe, documents, enrichment = scenario()
    for i in range(1, 6):
        for period, filed, amount, year in [
                ("2025-09-30", "2025-11-01", 60, 25),
                ("2025-12-31", "2026-02-01", 80, 26)]:
            documents.append(filing(
                [row(quantity=str(amount), value=str(amount * 10)),
                 row("000000002", str(200-amount), str((200-amount)*10), row_id="2")],
                manager_cik=str(i), accession=f"{i:010}-{year}-000003",
                report_period=period, filed=filed))
    for before, after in [("2025-09-30", "2025-12-31"), ("2025-12-31", "2026-03-31")]:
        enrichment["intervals"][f"{before}:{after}"] = {
            "action_check": {"verified": True, "source": "fixture://actions",
                             "start": before, "end": after, "available_at": "2026-08-01"}}
    full = analyze(universe, documents, "2026-06-30", "2026-08-31T23:59:59Z",
                   quarters=4, enrichment=enrichment)
    assert all(s["consecutive_intervals"] == 3 for s in full["signals"])
    incomplete = [d for d in documents if not
                  (d["manager_cik"] == "5" and d["report_period"] == "2025-09-30")]
    partial = analyze(universe, incomplete, "2026-06-30", "2026-08-31T23:59:59Z",
                      quarters=4, enrichment=enrichment)
    assert all(s["consecutive_intervals"] == 2 for s in partial["signals"])
