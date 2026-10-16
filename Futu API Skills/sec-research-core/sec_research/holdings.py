"""Resolve a disclosed manager-quarter without confusing amendments with trades."""

from dataclasses import dataclass
from decimal import Decimal
import re

from .evidence import accession, available_by, cik, day, fields, number, require


@dataclass(frozen=True)
class Holding:
    security_id: str
    cusip: str
    issuer: str
    class_title: str
    quantity_type: str
    put_call: str | None
    instrument_type: str
    quantity: Decimal | None
    value: Decimal | None
    accession: str
    row_id: str
    investment_discretion: str
    other_managers: tuple[str, ...]
    voting_authority: dict

    @property
    def equity(self):
        return (self.instrument_type in {"COMMON", "ADR"} and
                self.quantity_type == "SH" and self.put_call is None)


@dataclass(frozen=True)
class Portfolio:
    manager_cik: str
    report_period: str
    knowledge_cutoff: str
    rows: tuple[Holding, ...]
    accessions: tuple[str, ...]
    sources: tuple[dict, ...]
    issues: tuple[str, ...]
    reported_total: Decimal | None

    @property
    def complete(self):
        return not self.issues


def quarter(value):
    parsed = day(value)
    require((parsed.month, parsed.day) in {(3, 31), (6, 30), (9, 30), (12, 31)},
            "INVALID_REPORT_PERIOD")
    return parsed.isoformat()


def parse_rows(document):
    unit = document.get("value_unit")
    require(unit in {"USD", "USD_THOUSANDS"}, "UNITS_AMBIGUOUS")
    multiplier = Decimal(1 if unit == "USD" else 1000)
    issues, rows, seen = [], [], set()
    if document.get("unit_verified") is not True:
        issues.append("UNITS_AMBIGUOUS")
    if document.get("confidential") is not False:
        issues.append("CONFIDENTIAL_HOLDINGS")
    if document.get("report_type") != "HOLDINGS":
        issues.append("REPORT_SCOPE_UNRESOLVED")
    if document.get("discovery_complete") is not True:
        issues.append("FILING_INDEX_PARTIAL")
    require(isinstance(document["rows"], list), "INVALID_ROWS")
    for raw in document["rows"]:
        fields(raw, {"row_id", "cusip", "issuer", "class_title", "quantity_type", "put_call",
                     "quantity", "reported_value", "instrument_type", "investment_discretion",
                     "other_managers", "voting_authority"},
               {"row_id", "cusip", "issuer", "class_title", "quantity_type", "quantity",
                "reported_value", "instrument_type"})
        row_id = str(raw["row_id"])
        require(row_id and row_id not in seen, "DUPLICATE_ROW")
        seen.add(row_id)
        cusip = str(raw["cusip"]).upper()
        require(bool(re.fullmatch(r"[0-9A-Z*@#]{9}", cusip)), "INVALID_CUSIP")
        title = raw["class_title"]
        require(isinstance(title, str) and bool(title.strip()), "MISSING_SECURITY_CLASS")
        quantity_type, put_call = raw["quantity_type"], raw.get("put_call")
        require(quantity_type in {"SH", "PRN"}, "INVALID_QUANTITY_TYPE")
        require(put_call in {None, "PUT", "CALL"}, "INVALID_PUT_CALL")
        instrument = raw["instrument_type"]
        require(instrument in {"COMMON", "ADR", "ETF", "OPTION", "OTHER", "UNKNOWN"},
                "INVALID_INSTRUMENT")
        if instrument == "UNKNOWN":
            issues.append("SECURITY_TYPE_UNVERIFIED")
        quantity = number(raw["quantity"], nonnegative=True)
        value = number(raw["reported_value"], nonnegative=True)
        if quantity is None or value is None:
            issues.append("MISSING_NUMERIC_VALUE")
        if quantity == 0 and value not in {None, Decimal(0)}:
            issues.append("INCONSISTENT_POSITION")
        managers = raw.get("other_managers", [])
        require(isinstance(managers, list), "INVALID_OTHER_MANAGERS")
        security_id = "|".join((cusip, title.strip().upper(), quantity_type,
                                put_call or "NONE", instrument))
        rows.append(Holding(security_id, cusip, str(raw["issuer"]), title, quantity_type,
                            put_call, instrument, quantity,
                            value * multiplier if value is not None else None,
                            document["accession"], row_id,
                            str(raw.get("investment_discretion", "")),
                            tuple(str(x) for x in managers), raw.get("voting_authority", {})))
    count = document.get("summary_count")
    if type(count) is not int or count != len(rows):
        issues.append("ROW_COUNT_MISMATCH")
    summary = number(document.get("summary_value"), nonnegative=True)
    if summary is None or any(r.value is None for r in rows):
        issues.append("VALUE_TOTAL_UNVERIFIED")
    else:
        # Each declared raw monetary unit can round by half a unit.
        tolerance = multiplier * Decimal(len(rows) + 1) / 2
        if abs(sum((r.value for r in rows), Decimal(0)) - summary * multiplier) > tolerance:
            issues.append("VALUE_TOTAL_MISMATCH")
    return tuple(rows), tuple(sorted(set(issues)))


def effective_portfolio(documents, manager_cik, report_period, cutoff):
    manager_cik, report_period = cik(manager_cik), quarter(report_period)
    selected, notices = {}, []
    allowed = {"manager_cik", "accession", "form", "report_period", "filed", "published_at",
               "report_type", "amendment_type", "amendment_number", "summary_count",
               "summary_value", "value_unit", "unit_verified", "confidential", "rows",
               "source_url", "retrieved_at", "accepted_at", "other_included_managers",
               "discovery_complete", "unit_resolution", "unit_review", "security_type_reviews"}
    for document in documents:
        fields(document, allowed, {"manager_cik", "accession", "form", "report_period",
                                   "filed", "rows"})
        if (cik(document["manager_cik"]) != manager_cik or
                document["report_period"] != report_period or
                document["form"] not in {"13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A"}):
            continue
        require(day(document["filed"]) >= day(report_period), "FILING_BEFORE_PERIOD")
        if not available_by(document.get("published_at") or document["filed"], cutoff):
            continue
        key = accession(document["accession"])
        if document["form"] in {"13F-NT", "13F-NT/A"}:
            notices.append(document)
            continue
        require(key not in selected or selected[key] == document, "ACCESSION_CONFLICT")
        selected[key] = document
    require(bool(selected), "NO_ELIGIBLE_FILING")
    ordered = sorted(selected.values(), key=lambda d: (
        d["filed"], d.get("amendment_number") or 0, d["accession"]))
    rows, issues, sources, accessions = (), (), [], []
    chain_issues = ["REPORT_SCOPE_UNRESOLVED"] if notices else []
    have_base, last_sequence = False, 0
    for document in ordered:
        amendment = document["form"] == "13F-HR/A"
        amendment_type = document.get("amendment_type")
        if amendment:
            sequence = document.get("amendment_number")
            require(type(sequence) is int and sequence > last_sequence, "AMENDMENT_SEQUENCE")
            if sequence != last_sequence + 1:
                chain_issues.append("AMENDMENT_GAP")
            last_sequence = sequence
            require(amendment_type in {"RESTATEMENT", "NEW HOLDINGS"}, "AMENDMENT_TYPE")
        else:
            require(not have_base, "MULTIPLE_ORIGINALS")
            require(amendment_type is None, "AMENDMENT_TYPE")
        incoming, incoming_issues = parse_rows(document)
        if not amendment or amendment_type == "RESTATEMENT":
            rows, issues, have_base = incoming, incoming_issues, True
        else:
            require(have_base, "AMENDMENT_WITHOUT_BASE")
            # Repeated accessions were removed above. Repeated economic entries across
            # distinct additive filings need source review, not automatic deduplication.
            existing = {(r.security_id, r.quantity, r.value, r.other_managers,
                         r.investment_discretion) for r in rows}
            overlap = any((r.security_id, r.quantity, r.value, r.other_managers,
                           r.investment_discretion) in existing for r in incoming)
            rows += incoming
            issues += incoming_issues + (("POSSIBLE_DUPLICATE_ADDITION",) if overlap else ())
        accessions.append(document["accession"])
        sources.append({k: document.get(k) for k in (
            "accession", "form", "report_period", "filed", "published_at", "accepted_at",
            "source_url", "retrieved_at", "amendment_type", "amendment_number",
            "other_included_managers", "unit_resolution", "unit_review", "security_type_reviews")})
    for notice in notices:
        accessions.append(notice["accession"])
        sources.append({key: notice.get(key) for key in (
            "accession", "form", "report_period", "filed", "source_url",
            "other_included_managers")})
    total = (sum((r.value for r in rows), Decimal(0))
             if all(r.value is not None for r in rows) else None)
    return Portfolio(manager_cik, report_period, cutoff, rows, tuple(accessions),
                     tuple(sources), tuple(sorted(set(issues) | set(chain_issues))), total)
