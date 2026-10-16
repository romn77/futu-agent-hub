from types import SimpleNamespace

import pytest

from sec_research.evidence import ResearchError
from sec_research.reader import normalize_13f, content_slice, fact_record
from sec_research.transport import Budget


XML = """<informationTable><infoTable><nameOfIssuer>Synthetic</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>000000001</cusip><value>1000</value>
<shrsOrPrnAmt><sshPrnamt>100</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
</infoTable></informationTable>"""


class Table:
    def to_dict(self, orient):
        return [{"Issuer": "Synthetic", "Class": "COM", "Cusip": "000000001", "Value": 1000,
                 "SharesPrnAmount": 100, "PutCall": "", "Type": "Shares",
                 "InvestmentDiscretion": "", "OtherManager": ""}]


def obj(xml=XML):
    cover = SimpleNamespace(report_type="13F HOLDINGS REPORT")
    summary = SimpleNamespace(total_holdings=1, total_value=1000, other_managers=[])
    return SimpleNamespace(infotable_xml=xml, raw_infotable=Table(),
                           primary_form_information=SimpleNamespace(cover_page=cover,
                                                                    summary_page=summary),
                           value_unit_resolution=SimpleNamespace(unit="dollars", ambiguous=False),
                           report_period="2026-03-31", amendment_type=None, amendment_number=None)


def metadata():
    return {"manager_cik": "0000000001", "accession": "0000000001-26-000001", "form": "13F-HR",
            "filed": "2026-05-01", "source_url": "https://www.sec.gov/Archives/synthetic",
            "retrieved_at": "2026-08-31T12:00:00+00:00"}


def test_original_missing_number_does_not_become_sdk_zero():
    with pytest.raises(ResearchError, match="RAW_NUMERIC"):
        normalize_13f(obj(XML.replace("<value>1000</value>", "")), metadata(),
                      "<edgarSubmission/>")


def test_sdk_numeric_disagreement_is_rejected():
    with pytest.raises(ResearchError, match="SDK_NUMERIC"):
        normalize_13f(obj(XML.replace("<value>1000</value>", "<value>1001</value>")),
                      metadata(), "<edgarSubmission/>")


def test_complete_raw_rows_and_original_unit_survive_normalization():
    result = normalize_13f(obj(), metadata(), "<edgarSubmission/>")
    assert result["rows"][0]["reported_value"] == "1000"
    assert result["value_unit"] == "USD"
    assert result["summary_count"] == 1
    assert not result["discovery_complete"]


def test_content_pagination_is_explicit_and_repeatable():
    result = content_slice("abcdefghij", offset=3, limit=4)
    assert result == {"text": "defg", "offset": 3, "total_chars": 10,
                      "next_offset": 7, "truncated": True}


def test_budget_stops_before_exceeding_limit():
    budget = Budget(max_bytes=5, seconds=10)
    budget.consume(5)
    with pytest.raises(ResearchError, match="BYTE_BUDGET"):
        budget.consume(1)


def test_xml_entities_and_negative_offsets_are_rejected():
    with pytest.raises(ResearchError):
        normalize_13f(obj("<!DOCTYPE bad>" + XML), metadata(), "<edgarSubmission/>")
    with pytest.raises(ResearchError):
        content_slice("abc", offset=-1, limit=2)


def test_unknown_sdk_units_cannot_become_verified_thousands():
    document = obj()
    document.value_unit_resolution.unit = "bananas"
    with pytest.raises(ResearchError, match="UNITS_AMBIGUOUS"):
        normalize_13f(document, metadata(), "<edgarSubmission/>")


def test_overlapping_sdk_sessions_are_rejected_before_mutating_environment():
    from sec_research.transport import _SESSION_LOCK, sdk_session
    assert _SESSION_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(ResearchError, match="SDK_SESSION_BUSY"):
            with sdk_session(Budget()):
                pass
    finally:
        _SESSION_LOCK.release()


@pytest.mark.parametrize("failure", ["create", "close"])
def test_sdk_failure_restores_host_manager_and_environment(monkeypatch, failure):
    import os
    import sys
    from sec_research import transport
    original = SimpleNamespace(closed=False)
    created = SimpleNamespace(httpx_params={})
    def create(**kwargs):
        if failure == "create":
            raise RuntimeError("setup failure")
        return created
    def close():
        if failure == "close":
            raise RuntimeError("cleanup failure")
    created.close = close
    httpclient = SimpleNamespace(HTTP_MGR=original, get_http_mgr=create,
                                 configure_http=lambda **kwargs: None,
                                 close_clients=lambda: setattr(original, "closed", True))
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(SyncByteStream=object))
    monkeypatch.setitem(sys.modules, "edgar", SimpleNamespace(httpclient=httpclient))
    monkeypatch.setattr(transport.importlib.metadata, "version", lambda name: "5.62.0")
    monkeypatch.setenv("EDGAR_IDENTITY", "Synthetic Test test@example.invalid")
    monkeypatch.setenv("EDGAR_LOCAL_DATA_DIR", "previous-cache")
    with pytest.raises((RuntimeError, ResearchError)):
        with transport.sdk_session(Budget()):
            pass
    assert httpclient.HTTP_MGR is original
    assert not original.closed
    assert os.environ["EDGAR_LOCAL_DATA_DIR"] == "previous-cache"


def test_financial_fact_preserves_raw_integer_precision_and_missing_period():
    raw = {"value": 9007199254740993, "numeric_value": 9007199254740992.0,
           "concept": "assets", "unit": "JPY", "period_start": None,
           "period_end": "2026-06-30", "taxonomy": "ifrs-full",
           "accession": "0000000001-26-000001", "filing_date": "2026-08-01"}
    assert fact_record(raw)["value"] == 9007199254740993
    raw["period_start"], raw["period_end"] = "2026-01-01", None
    assert fact_record(raw)["period_kind"] == "unverified"
