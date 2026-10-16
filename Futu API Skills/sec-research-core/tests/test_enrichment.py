from copy import deepcopy

from sec_research.enrichment import review_filings
from sec_research.holdings import effective_portfolio
from sample_data import filing, row

CUTOFF = "2026-08-31T23:59:59Z"


def test_unit_review_requires_explicit_asof_evidence_and_keeps_raw_values():
    original = filing(unit_verified=False)
    review = {"filing_reviews": {original["accession"]: {
        "unit": "USD", "verified": True, "source": "fixture://unit-proof",
        "available_at": "2026-08-01"}}}
    reviewed = review_filings([original], review, CUTOFF)
    result = effective_portfolio(reviewed, "1", "2026-03-31", CUTOFF)
    assert result.complete
    assert result.reported_total == 1000
    assert not original["unit_verified"]
    assert result.sources[0]["unit_review"]["source"] == "fixture://unit-proof"
    review["filing_reviews"][original["accession"]]["available_at"] = "2026-09-01"
    late = review_filings([original], review, CUTOFF)
    assert not effective_portfolio(late, "1", "2026-03-31", CUTOFF).complete


def test_verified_instrument_type_can_resolve_unknown_but_never_change_option_identity():
    original = filing([row(class_title="CL B", instrument_type="UNKNOWN")])
    key = "000000001|CL B|SH|NONE|UNKNOWN"
    review = {"security_types": {key: {
        "instrument_type": "COMMON", "verified": True, "source": "fixture://security",
        "available_at": "2026-05-01", "as_of": "2026-03-31"}}}
    reviewed = review_filings([original], review, CUTOFF)
    assert reviewed[0]["rows"][0]["instrument_type"] == "COMMON"
    assert original["rows"][0]["instrument_type"] == "UNKNOWN"
    option = deepcopy(original)
    option["rows"][0]["put_call"] = "PUT"
    assert review_filings([option], review, CUTOFF)[0]["rows"][0]["instrument_type"] == "UNKNOWN"
