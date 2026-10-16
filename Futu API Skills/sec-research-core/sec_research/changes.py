"""Compare disclosed long-equity quantities at a common price basis."""

from decimal import Decimal

from .evidence import available_by, day, number, require

ZERO = Decimal(0)


def _positions(portfolio):
    result = {}
    for row in portfolio.rows:
        if not row.equity:
            continue
        entry = result.setdefault(row.security_id, {
            "quantity": ZERO, "value": ZERO, "issuer": row.issuer, "sources": []})
        for field in ("quantity", "value"):
            value = getattr(row, field)
            entry[field] = (entry[field] + value
                            if entry[field] is not None and value is not None else None)
        entry["sources"].append({"accession": row.accession, "row_id": row.row_id})
    return result


def _total(positions):
    values = [p["value"] for p in positions.values()]
    return sum(values, ZERO) if all(v is not None for v in values) else None


def _ratio(numerator, denominator):
    return numerator / denominator if denominator is not None and denominator > 0 else None


def _weights(positions):
    total = _total(positions)
    if total is None or total <= 0:
        return {}
    return {key: value["value"] / total for key, value in positions.items()}


def _concentration(weights):
    if not weights:
        return None
    return {"holding_count": len(weights),
            "top5_weight": sum(sorted(weights.values(), reverse=True)[:5], ZERO),
            "hhi": sum((weight * weight for weight in weights.values()), ZERO),
            "basis": "disclosed common/ADR value; includes price effects"}


def _valid_evidence(value, cutoff):
    return (isinstance(value, dict) and value.get("verified") is True and
            bool(value.get("source")) and bool(value.get("available_at")) and
            available_by(value["available_at"], cutoff))


def compare_portfolios(previous, current, enrichment=None):
    enrichment = enrichment or {}
    require(previous.manager_cik == current.manager_cik, "MANAGER_MISMATCH")
    require(previous.knowledge_cutoff == current.knowledge_cutoff, "CUTOFF_MISMATCH")
    start, end = day(previous.report_period), day(current.report_period)
    require(80 <= (end - start).days <= 100, "NONADJACENT_PERIODS")
    old, new = _positions(previous), _positions(current)
    issues = list(previous.issues + current.issues)
    check = enrichment.get("action_check", {})
    actions_verified = (_valid_evidence(check, current.knowledge_cutoff) and
                        check.get("start") == previous.report_period and
                        check.get("end") == current.report_period)
    if not actions_verified:
        issues.append("CORPORATE_ACTIONS_UNVERIFIED")
    unresolved = set(enrichment.get("unresolved_actions", []))
    for old_id, action in enrichment.get("actions", {}).items():
        if old_id not in old:
            continue
        valid = (actions_verified and action.get("verified") is True and action.get("source") and
                 start < day(action["effective_date"]) <= end)
        if not valid:
            unresolved.add(old_id)
            if action.get("current_id"):
                unresolved.add(action["current_id"])
            continue
        factor = number(action.get("factor"), nonnegative=True)
        require(factor is not None and factor > 0, "INVALID_ACTION_FACTOR")
        new_id = action.get("current_id")
        require(isinstance(new_id, str) and bool(new_id), "INVALID_SECURITY_MAPPING")
        require(new_id == old_id or new_id not in old, "AMBIGUOUS_SECURITY_MAPPING")
        item = old.pop(old_id)
        item["quantity"] = item["quantity"] * factor if item["quantity"] is not None else None
        old[new_id] = item
    if unresolved:
        issues.append("UNRESOLVED_CORPORATE_ACTIONS")

    changes, comparable = [], []
    infer_absence = previous.complete and current.complete
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        q0 = before["quantity"] if before else (ZERO if infer_absence else None)
        q1 = after["quantity"] if after else (ZERO if infer_absence else None)
        v0 = before["value"] if before else (ZERO if infer_absence else None)
        v1 = after["value"] if after else (ZERO if infer_absence else None)
        known = q0 is not None and q1 is not None and key not in unresolved
        delta = q1 - q0 if known else None
        status = "UNVERIFIED"
        if known:
            if q0 == 0 and q1 > 0:
                status = "NEW_REPORTED"
            elif q0 > 0 and q1 == 0:
                status = "NO_LONGER_REPORTED"
            else:
                status = "INCREASED" if delta > 0 else "DECREASED" if delta < 0 else "UNCHANGED"
        p1 = v1 / q1 if q1 is not None and q1 > 0 and v1 is not None else None
        if p1 is None:
            quote = enrichment.get("prices", {}).get(key, {})
            if (_valid_evidence(quote, current.knowledge_cutoff) and
                    quote.get("as_of") == current.report_period):
                p1 = number(quote.get("value"), nonnegative=True)
        if p1 is not None and p1 <= 0:
            p1 = None
            issues.append("PRICE_UNVERIFIED")
        p0 = v0 / q0 if q0 is not None and q0 > 0 and v0 is not None else None
        change = {
            "security_id": key, "issuer": (after or before)["issuer"],
            "status": status, "previous_quantity_adjusted": q0, "current_quantity": q1,
            "quantity_basis": "corporate_actions_verified" if actions_verified else "reported_unadjusted",
            "delta_quantity": delta, "quantity_change_pct": _ratio(delta, q0)
            if delta is not None else None, "previous_value": v0, "current_value": v1,
            "quantity_effect_proxy": delta * p1 if delta is not None and p1 is not None else None,
            "repricing_effect": q0 * (p1 - p0)
            if known and p1 is not None and p0 is not None else None,
            "allocation_shift": None, "current_weight": None, "previous_fixed_weight": None,
            "sources": (before["sources"] if before else []) + (after["sources"] if after else []),
        }
        changes.append(change)
        if known and p1 is not None and v0 is not None and v1 is not None:
            comparable.append((change, q0 * p1, q1 * p1))
    fixed0 = sum((v0 for _, v0, _ in comparable), ZERO)
    fixed1 = sum((v1 for _, _, v1 in comparable), ZERO)
    for change, value0, value1 in comparable:
        if fixed0 > 0 and fixed1 > 0:
            change["previous_fixed_weight"] = value0 / fixed0
            change["current_weight"] = value1 / fixed1
            change["allocation_shift"] = value1 / fixed1 - value0 / fixed0
    total0, total1 = _total(old), _total(new)
    covered0 = sum((change["previous_value"] for change, _, _ in comparable), ZERO)
    covered1 = sum((change["current_value"] for change, _, _ in comparable), ZERO)
    coverage0, coverage1 = _ratio(covered0, total0), _ratio(covered1, total1)
    if any(change["allocation_shift"] is None for change in changes):
        issues.append("PARTIAL_FIXED_PRICE_COVERAGE")
    eligible = (previous.complete and current.complete and actions_verified and not unresolved and
                fixed0 > 0 and fixed1 > 0 and coverage0 is not None and coverage1 is not None and
                min(coverage0, coverage1) >= Decimal(".9"))
    weights0 = _weights(old) if previous.complete and not unresolved else {}
    weights1 = _weights(new) if current.complete and not unresolved else {}
    return {
        "manager_cik": current.manager_cik, "previous_period": previous.report_period,
        "current_period": current.report_period, "knowledge_cutoff": current.knowledge_cutoff,
        "eligible": eligible, "issues": sorted(set(issues)), "changes": changes,
        "coverage_previous": coverage0, "coverage_current": coverage1,
        "reported_total_previous": previous.reported_total,
        "reported_total_current": current.reported_total,
        "equity_total_previous": total0, "equity_total_current": total1,
        "fixed_price_total_previous": fixed0, "fixed_price_total_current": fixed1,
        "concentration_previous": _concentration(weights0),
        "concentration_current": _concentration(weights1),
        "reported_weight_overlap": sum((min(weights0.get(key, ZERO), weights1.get(key, ZERO))
                                        for key in set(weights0) | set(weights1)), ZERO)
        if weights0 and weights1 else None,
        "excluded_instruments": sorted({r.instrument_type for r in previous.rows + current.rows
                                        if not r.equity}),
        "evidence": list(previous.sources + current.sources),
    }
