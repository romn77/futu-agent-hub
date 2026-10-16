"""Deterministic public-sample observations; no return or causality predictions."""

from collections import Counter
from datetime import date
from decimal import Decimal
from statistics import median

from .changes import compare_portfolios
from .enrichment import review_filings
from .evidence import ResearchError, available_by, cik, day, json_value, require, timestamp
from .holdings import effective_portfolio, quarter
from .universe import digest

EPSILON = Decimal("0.00000001")


def periods_ending(target, count):
    require(type(count) is int and 1 <= count <= 20, "INVALID_QUARTER_COUNT")
    end = day(quarter(target))
    result = []
    for _ in range(count):
        result.append(end.isoformat())
        year, month = end.year, end.month - 3
        if month <= 0:
            year, month = year - 1, 12
        end = date(year, month, 31 if month in {3, 12} else 30)
    return list(reversed(result))


def _classifications(enrichment, cutoff, target):
    result = {}
    if not enrichment.get("classification_version"):
        return result
    for key, value in enrichment.get("classifications", {}).items():
        require(isinstance(value, dict), "INVALID_CLASSIFICATION")
        if (not value.get("source") or not value.get("available_at") or not value.get("as_of")
                or not available_by(value["available_at"], cutoff)
                or day(value["as_of"]) > day(target)):
            continue
        labels = []
        for dimension in ("industry", "size"):
            if isinstance(value.get(dimension), str) and value[dimension]:
                labels.append((dimension, value[dimension]))
        themes = value.get("themes", [])
        require(isinstance(themes, list) and all(isinstance(x, str) for x in themes),
                "INVALID_THEMES")
        labels.extend(("theme", theme) for theme in set(themes))
        method = enrichment.get("style_method", {})
        if (all(method.get(k) for k in ("metrics", "universe", "buckets", "source",
                                       "as_of", "available_at"))
                and day(method["as_of"]) <= day(target)
                and available_by(method["available_at"], cutoff) and value.get("style")):
            labels.append(("style", str(value["style"])))
        result[key] = labels
    return result


def _observations(comparison, classification):
    totals, coverage = {}, {}
    for change in comparison["changes"]:
        if change["allocation_shift"] is None:
            continue
        labels = classification.get(change["security_id"], [])
        for dimension in {dimension for dimension, _ in labels}:
            pair = coverage.setdefault(dimension, [Decimal(0), Decimal(0)])
            pair[0] += change["previous_fixed_weight"]
            pair[1] += change["current_weight"]
        for label in labels:
            totals[label] = totals.get(label, Decimal(0)) + change["allocation_shift"]
    valid_dimensions = {key for key, pair in coverage.items() if min(pair) >= Decimal(".9")}
    return ({key: value for key, value in totals.items() if key[0] in valid_dimensions},
            valid_dimensions, coverage)


def _statistics(values, expected_count):
    n = len(values)
    up = sum(value > EPSILON for value in values)
    down = sum(value < -EPSILON for value in values)
    mid = median(values) if n else Decimal(0)
    breadth = Decimal(up - down) / n if n else Decimal(0)
    coverage = Decimal(n) / expected_count if expected_count else Decimal(0)
    direction = "INCREASE" if mid > EPSILON else "DECREASE" if mid < -EPSILON else "MIXED"
    support = up if direction == "INCREASE" else down if direction == "DECREASE" else 0
    qualified = (n >= 5 and coverage >= Decimal(".8") and
                 Decimal(support) / n >= Decimal(".6") and
                 (breadth > 0 if direction == "INCREASE" else breadth < 0))
    return {"eligible_count": n, "expected_count": expected_count, "sample_coverage": coverage,
            "increase_count": up, "decrease_count": down, "unchanged_count": n - up - down,
            "net_breadth": breadth, "median_shift_bps": mid * 10000,
            "direction": direction, "status": "RESEARCH_LEAD" if qualified else "OBSERVATION"}


def _disclosed_overlap(universe, portfolios):
    identities = {m.cik: {f"cik:{m.cik}", f"family:{m.sampling_unit}"}
                  for m in universe.managers}
    unresolved = set()
    for (manager, _), portfolio in portfolios.items():
        by_accession = {}
        for source in portfolio.sources:
            included = source.get("other_included_managers") or []
            by_accession[source["accession"]] = included
            for other in included:
                if other.get("cik"):
                    identities[manager].add("cik:" + cik(other["cik"]))
                if other.get("file_number"):
                    identities[manager].add("file:" + str(other["file_number"]))
                if not other.get("cik"):
                    # A file number alone cannot be mapped to configured CIKs safely.
                    unresolved.add(manager)
        for row in portfolio.rows:
            included = by_accession.get(row.accession, [])
            mapped = {str(other.get("sequence_number")) for other in included}
            if any(reference not in mapped for reference in row.other_managers):
                unresolved.add(manager)
    overlapping = set()
    managers = list(identities)
    for index, left in enumerate(managers):
        for right in managers[index + 1:]:
            if identities[left] & identities[right]:
                overlapping.update((left, right))
    return overlapping, unresolved


def analyze(universe, documents, target_period, cutoff, *, quarters=4, enrichment=None):
    timestamp(cutoff)
    require(day(target_period) <= timestamp(cutoff).date(), "FUTURE_REPORT_PERIOD")
    periods = periods_ending(target_period, quarters)
    enrichment = enrichment or {}
    require(isinstance(documents, list) and isinstance(enrichment, dict), "INVALID_BUNDLE")
    documents = review_filings(documents, enrichment, cutoff)
    diagnostics, portfolios, comparisons = [], {}, []
    # Validate all supplied identifiers before filtering; no private account adapter is involved.
    by_manager = {}
    for document in documents:
        require(isinstance(document, dict) and "manager_cik" in document, "INVALID_FILING")
        by_manager.setdefault(cik(document["manager_cik"]), []).append(document)
    families = Counter(m.sampling_unit for m in universe.managers)
    independent = {m.cik for m in universe.managers if m.independent and
                   families[m.sampling_unit] == 1}
    expected = len({m.sampling_unit for m in universe.managers})
    for manager in universe.managers:
        if families[manager.sampling_unit] > 1:
            diagnostics.append({"manager_cik": manager.cik, "code": "OVERLAPPING_MANAGERS"})
        elif not manager.independent:
            diagnostics.append({"manager_cik": manager.cik, "code": "INDEPENDENCE_UNVERIFIED"})
        for period in periods:
            try:
                portfolios[manager.cik, period] = effective_portfolio(
                    by_manager.get(manager.cik, []), manager.cik, period, cutoff)
            except ResearchError as error:
                diagnostics.append({"manager_cik": manager.cik, "period": period, "code": error.code})
        for previous, current in zip(periods, periods[1:]):
            if (manager.cik, previous) not in portfolios or (manager.cik, current) not in portfolios:
                continue
            interval = f"{previous}:{current}"
            comparison = compare_portfolios(portfolios[manager.cik, previous],
                                           portfolios[manager.cik, current],
                                           enrichment.get("intervals", {}).get(interval, {}))
            comparisons.append(comparison)
    overlapping, unresolved = _disclosed_overlap(universe, portfolios)
    independent -= overlapping | unresolved
    for manager in sorted(overlapping):
        diagnostic = {"manager_cik": manager, "code": "OVERLAPPING_MANAGERS"}
        if diagnostic not in diagnostics:
            diagnostics.append(diagnostic)
    diagnostics.extend({"manager_cik": manager, "code": "MANAGER_RELATIONSHIP_UNRESOLVED"}
                       for manager in sorted(unresolved))
    classification = _classifications(enrichment, cutoff, target_period)
    if not classification:
        diagnostics.append({"code": "CLASSIFICATION_UNAVAILABLE"})
    observations = {}
    labels = set()
    for comparison in comparisons:
        values, dimensions, classification_coverage = _observations(comparison, classification)
        interval = comparison["previous_period"], comparison["current_period"]
        labels.update(values)
        observations[interval, comparison["manager_cik"]] = (
            values, dimensions, comparison["eligible"] and comparison["manager_cik"] in independent,
            classification_coverage)
        comparison["classification_coverage"] = classification_coverage
    intervals = list(zip(periods, periods[1:]))
    analysis_scope = {"quarters": quarters, "report_periods": periods,
                      "instruments": ["COMMON", "ADR"], "method": "common_price_v1"}
    evidence_version = digest(json_value({"documents": documents, "enrichment": enrichment}))
    signals = []
    for label in sorted(labels):
        if not intervals:
            break
        latest = intervals[-1]
        cohorts = {}
        for interval in intervals:
            cohorts[interval] = {
                m.cik: observations[interval, m.cik][0].get(label, Decimal(0))
                for m in universe.managers
                if (interval, m.cik) in observations and observations[interval, m.cik][2] and
                label[0] in observations[interval, m.cik][1]}
        latest_values = cohorts[latest]
        stats = _statistics(list(latest_values.values()), expected)
        direction = stats["direction"]
        consecutive, common = 1 if latest_values else 0, set(latest_values)
        for index in range(len(intervals) - 2, -1, -1):
            candidate = common & set(cohorts[intervals[index]])
            window = intervals[index:]
            checks = [_statistics([cohorts[i][m] for m in candidate], expected) for i in window]
            if not checks or any(s["status"] != "RESEARCH_LEAD" or
                                 s["direction"] != direction for s in checks):
                break
            common, consecutive = candidate, len(window)
        relevant_periods = {i[1] for i in intervals[-max(1, consecutive):]}
        refs = [source for comparison in comparisons
                if comparison["manager_cik"] in latest_values and
                comparison["current_period"] in relevant_periods for source in comparison["evidence"]]
        classification_coverage = []
        contributions = []
        for comparison in comparisons:
            if comparison["current_period"] != target_period:
                continue
            ratios = comparison["classification_coverage"].get(label[0], [Decimal(0), Decimal(0)])
            classification_coverage.append({
                "manager_cik": comparison["manager_cik"],
                "previous_ratio": ratios[0], "current_ratio": ratios[1],
                "previous_denominator_usd": comparison["fixed_price_total_previous"],
                "current_denominator_usd": comparison["fixed_price_total_current"]})
            if comparison["manager_cik"] in latest_values:
                contributions.extend({"manager_cik": comparison["manager_cik"], **change}
                                     for change in comparison["changes"]
                                     if label in classification.get(change["security_id"], []))
        signal = {
            "signal_id": digest([universe.version, target_period, cutoff, label,
                                 enrichment.get("classification_version"), analysis_scope,
                                 evidence_version]),
            "analysis_scope": analysis_scope, "evidence_version": evidence_version,
            "universe_version": universe.version, "classification_version":
                enrichment.get("classification_version"), "dimension": label[0], "label": label[1],
            "report_periods": list(latest), "knowledge_cutoff": cutoff,
            **stats, "eligible_manager_ids": sorted(latest_values),
            "supporting_managers": sorted(m for m, v in latest_values.items()
                                         if ((direction == "INCREASE" and v > EPSILON) or
                                             (direction == "DECREASE" and v < -EPSILON))),
            "opposing_managers": sorted(m for m, v in latest_values.items()
                                       if ((direction == "INCREASE" and v < -EPSILON) or
                                           (direction == "DECREASE" and v > EPSILON))),
            "increasing_managers": sorted(m for m, v in latest_values.items() if v > EPSILON),
            "decreasing_managers": sorted(m for m, v in latest_values.items() if v < -EPSILON),
            "classification_coverage": classification_coverage,
            "security_contributions": contributions,
            "classification_sources": [enrichment["classifications"][key] for key, labels_for_key
                                       in classification.items() if label in labels_for_key],
            "consecutive_intervals": consecutive,
            "persistent_cohort": sorted(common) if consecutive >= 2 else [],
            "evidence_refs": refs,
            "research_questions": [f"What operating or valuation evidence could explain {label[1]}?",
                                   "What contrary disclosures or alternative explanations weaken this view?"],
            "limitations": ["Disclosed sample only; not actual fund flows or a return forecast.",
                            "Common-price weights do not identify subscriptions, mandates or motives."],
        }
        signals.append(signal)
    signals.sort(key=lambda s: (s["status"] != "RESEARCH_LEAD", -s["consecutive_intervals"],
                               -abs(s["net_breadth"]), -abs(s["median_shift_bps"]),
                               s["dimension"], s["label"]))
    return {"schema_version": 1, "universe": universe.public_summary(),
            "target_period": target_period, "report_periods": periods, "knowledge_cutoff": cutoff,
            "status": "COMPLETE" if not diagnostics and all(c["eligible"] for c in comparisons)
            and bool(comparisons) else "PARTIAL",
            "signals": signals, "research_leads": [s["signal_id"] for s in signals
                                                  if s["status"] == "RESEARCH_LEAD"][:5],
            "comparisons": comparisons, "diagnostics": diagnostics,
            "portfolio_coverage": [{"manager_cik": m, "period": p, "complete": v.complete,
                                    "issues": list(v.issues), "row_count": len(v.rows),
                                    "accessions": list(v.accessions)}
                                   for (m, p), v in portfolios.items()]}
