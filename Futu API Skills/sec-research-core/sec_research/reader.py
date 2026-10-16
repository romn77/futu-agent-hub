"""Use installed SDK objects; validate missingness against the original XML."""

from datetime import datetime, timezone
from decimal import Decimal
import re
import xml.etree.ElementTree as ET

from .evidence import ResearchError, accession, available_by, cik, day, number, require, timestamp
from .holdings import quarter


def _xml(text):
    require(isinstance(text, str) and "<!DOCTYPE" not in text.upper()
            and "<!ENTITY" not in text.upper(), "UNSUPPORTED_XML")
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        raise ResearchError("INVALID_XML") from None


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _text(element, tag):
    return next((child.text.strip() for child in element.iter()
                 if _tag(child) == tag and child.text and child.text.strip()), None)


def content_slice(text, *, offset=0, limit=12000):
    require(type(offset) is int and offset >= 0 and type(limit) is int
            and 1 <= limit <= 100000, "INVALID_CONTENT_RANGE")
    require(isinstance(text, str), "CONTENT_UNAVAILABLE")
    require(offset <= len(text), "OFFSET_OUT_OF_RANGE")
    end = min(offset + limit, len(text))
    return {"text": text[offset:end], "offset": offset, "total_chars": len(text),
            "next_offset": end if end < len(text) else None,
            "truncated": offset > 0 or end < len(text)}


def _instrument(title, kind, put_call):
    if put_call:
        return "OPTION"
    if kind != "SH":
        return "OTHER"
    title = title.strip().upper()
    if title in {"COM", "COMMON", "COMMON STOCK", "ORD", "ORD SHS", "CL A", "CL B"}:
        # Class-only labels don't establish a common-stock type.
        return "COMMON" if title not in {"CL A", "CL B"} else "UNKNOWN"
    if title in {"ADR", "SPON ADR", "SPONSORED ADR", "SPONSORED ADS"}:
        return "ADR"
    return "UNKNOWN"


def fact_record(raw):
    def date_value(value):
        return None if value is None or str(value) in {"NaT", "nan", "<NA>"} else str(value)[:10]
    start, end = date_value(raw.get("period_start")), date_value(raw.get("period_end"))
    duration = (day(end) - day(start)).days + 1 if start and end else None
    original = raw.get("value")
    value = None if str(original) in {"nan", "<NA>", "None"} else number(original)
    if end is None:
        period_kind = "unverified"
    elif start is None:
        period_kind = "instant"
    elif 75 <= duration <= 105:
        period_kind = "standalone"
    else:
        period_kind = "other_duration"
    return {"concept": raw["concept"], "value": value, "unit": raw.get("unit"),
            "start": start, "end": end, "duration_days": duration, "period_kind": period_kind,
            "basis": raw.get("taxonomy"), "accession": raw["accession"],
            "filed": str(raw["filing_date"])[:10], "derived": False}


def normalize_13f(obj, metadata, primary_xml):
    raw_nodes = [node for node in _xml(obj.infotable_xml).iter() if _tag(node) == "infoTable"]
    require(obj.raw_infotable is not None, "INFORMATION_TABLE_UNAVAILABLE")
    sdk_rows = obj.raw_infotable.to_dict("records")
    require(len(raw_nodes) == len(sdk_rows), "RAW_ROW_COUNT_MISMATCH")
    rows = []
    for index, (node, sdk_row) in enumerate(zip(raw_nodes, sdk_rows)):
        value, quantity = _text(node, "value"), _text(node, "sshPrnamt")
        require(value is not None and quantity is not None, "RAW_NUMERIC_MISSING")
        value, quantity = number(value, nonnegative=True), number(quantity, nonnegative=True)
        require(number(sdk_row.get("Value")) == value and
                number(sdk_row.get("SharesPrnAmount")) == quantity, "SDK_NUMERIC_MISMATCH")
        identifier, title = _text(node, "cusip"), _text(node, "titleOfClass")
        require(identifier == sdk_row.get("Cusip") and title == sdk_row.get("Class"),
                "SDK_IDENTITY_MISMATCH")
        kind, put_call = _text(node, "sshPrnamtType"), _text(node, "putCall")
        rows.append({"row_id": str(index + 1), "cusip": identifier,
                     "issuer": _text(node, "nameOfIssuer") or "", "class_title": title,
                     "quantity_type": kind, "put_call": put_call.upper() if put_call else None,
                     "quantity": str(quantity), "reported_value": str(value),
                     "instrument_type": _instrument(title, kind, put_call),
                     "investment_discretion": _text(node, "investmentDiscretion") or "",
                     "other_managers": [x for x in re.split(r"[,\s]+", _text(node, "otherManager") or "") if x],
                     "voting_authority": {name: _text(node, name) for name in ("Sole", "Shared", "None")}})
    primary = _xml(primary_xml)
    summary = obj.primary_form_information.summary_page
    require(summary is not None, "SUMMARY_UNAVAILABLE")
    resolution = obj.value_unit_resolution
    require(resolution.unit in {"dollars", "thousands"}, "UNITS_AMBIGUOUS")
    report_type = obj.primary_form_information.cover_page.report_type
    return {
        **metadata, "report_period": str(obj.report_period)[:10],
        "report_type": "HOLDINGS" if report_type == "13F HOLDINGS REPORT" else "COMBINATION",
        "amendment_type": obj.amendment_type, "amendment_number": obj.amendment_number,
        "summary_count": summary.total_holdings, "summary_value": str(summary.total_value),
        "value_unit": "USD" if resolution.unit == "dollars" else "USD_THOUSANDS",
        "unit_verified": not resolution.ambiguous,
        "unit_resolution": {key: getattr(resolution, key, None) for key in (
            "unit", "source", "ambiguous", "priceable_rows", "fraction_sub_dollar", "schema_version")},
        "confidential": (_text(primary, "isConfidentialOmitted") or "false").lower() == "true",
        "other_included_managers": [
            {"cik": str(manager.cik) if manager.cik else None, "name": manager.name,
             "file_number": manager.file_number, "sequence_number": manager.sequence_number}
            for manager in (summary.other_managers or [])],
        "rows": rows, "discovery_complete": False,
    }


class EdgarReader:
    def __init__(self, sdk, budget):
        self.sdk, self.budget = sdk, budget
        self.companies = {}
        self.filings = {}
        self.contents = {}

    def _company(self, identifier):
        identifier = cik(identifier)
        if identifier not in self.companies:
            self.budget.check()
            self.companies[identifier] = self.sdk.Company(int(identifier))
        return self.companies[identifier]

    def _metadata(self, filing):
        return {"manager_cik": cik(filing.cik), "accession": filing.accession_no,
                "form": filing.form, "filed": str(filing.filing_date),
                "source_url": filing.homepage_url,
                "retrieved_at": datetime.now(timezone.utc).isoformat()}

    def index(self, identifier, forms, start, cutoff, *, limit=100):
        require(1 <= limit <= 1000, "INVALID_LIMIT")
        timestamp(cutoff)
        day(start)
        allowed = {"13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A", "10-K", "10-K/A",
                   "10-Q", "10-Q/A", "8-K", "8-K/A", "20-F", "20-F/A", "6-K", "40-F"}
        require(bool(forms) and set(forms) <= allowed, "UNSUPPORTED_FORM")
        self.budget.check()
        filings = self._company(identifier).get_filings(
            form=forms, filing_date=f"{start}:{timestamp(cutoff).date().isoformat()}",
            amendments=True, trigger_full_load=True)
        rows = []
        for filing in filings:
            self.budget.check()
            if not available_by(str(filing.filing_date), cutoff):
                continue
            rows.append({**self._metadata(filing),
                         "report_period": str(getattr(filing, "report_date", ""))[:10] or None})
            self.filings[cik(identifier), filing.accession_no] = filing
            if len(rows) > limit:
                break
        complete = len(rows) <= limit
        return {"filings": rows[:limit], "complete": complete,
                "issues": [] if complete else ["FILING_LIMIT"],
                "time_precision": "filing_date; conservative next Eastern midnight",
                "knowledge_cutoff": cutoff}

    def _filing(self, identifier, accession_number, cutoff):
        identifier, accession_number = cik(identifier), accession(accession_number)
        key = identifier, accession_number
        if key not in self.filings:
            self.budget.check()
            result = self._company(identifier).get_filings(
                accession_number=accession_number, amendments=True, trigger_full_load=True)
            require(result is not None and len(result) == 1, "FILING_NOT_FOUND")
            self.filings[key] = result[0]
        filing = self.filings[key]
        require(cik(filing.cik) == identifier and filing.accession_no == accession_number,
                "FILING_IDENTITY_MISMATCH")
        require(available_by(str(filing.filing_date), cutoff), "FILING_AFTER_CUTOFF")
        return filing

    def holdings(self, identifier, accession_number, cutoff):
        filing = self._filing(identifier, accession_number, cutoff)
        require(filing.form in {"13F-HR", "13F-HR/A"}, "UNSUPPORTED_FORM")
        self.budget.check()
        result = normalize_13f(filing.obj(), self._metadata(filing), filing.xml())
        quarter(result["report_period"])
        return result

    def content(self, identifier, accession_number, cutoff, *, section="full_text",
                offset=0, limit=12000):
        filing = self._filing(identifier, accession_number, cutoff)
        key = filing.accession_no, section
        if key not in self.contents:
            self.budget.check()
            if section == "full_text":
                text = filing.text()
            else:
                require(section in {"business", "risk_factors", "mda", "financials", "legal",
                                    "Part I, Item 1", "Part I, Item 2", "Part II, Item 1A",
                                    "Item 3", "Item 4", "Item 5"}, "UNSUPPORTED_SECTION")
                obj = filing.obj()
                require(obj is not None, "SECTION_UNAVAILABLE")
                text = obj[section]
            require(text is not None, "SECTION_UNAVAILABLE")
            self.contents[key] = str(text)
        return {**self._metadata(filing), "section": section,
                **content_slice(self.contents[key], offset=offset, limit=limit)}

    def facts(self, identifier, accessions, concepts, cutoff, *, limit=2000):
        require(bool(accessions) and bool(concepts) and 1 <= limit <= 10000, "INVALID_FACT_SCOPE")
        for acc in accessions:
            self._filing(identifier, acc, cutoff)
        facts = self._company(identifier).get_facts()
        require(facts is not None, "FACTS_UNAVAILABLE")
        frame = facts.to_dataframe(include_metadata=True, pit_mode=True)
        selected = frame[frame["accession"].isin(accessions) & frame["concept"].isin(concepts)]
        output = []
        for raw in selected.to_dict("records"):
            self.budget.check()
            filed = str(raw.get("filing_date"))[:10]
            if not available_by(filed, cutoff):
                continue
            output.append(fact_record(raw))
            if len(output) > limit:
                break
        return {"cik": cik(identifier), "knowledge_cutoff": cutoff,
                "facts": output[:limit], "truncated": len(output) > limit,
                "coverage": "requested concepts and filings; Company Facts standard entity-wide facts"}
