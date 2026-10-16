from decimal import Decimal

import pytest

from sec_research.evidence import ResearchError
from sec_research.holdings import effective_portfolio
from sample_data import filing, row

CUTOFF = "2026-08-31T23:59:59Z"


def test_restatement_replaces_then_addition_supplements():
    original = filing()
    restated = filing([row(quantity="90", value="900")],
                      accession="0000000001-26-000002", filed="2026-05-02",
                      form="13F-HR/A", amendment_type="RESTATEMENT", amendment_number=1)
    addition = filing([row("000000002", "20", "200")],
                      accession="0000000001-26-000003", filed="2026-05-03",
                      form="13F-HR/A", amendment_type="NEW HOLDINGS", amendment_number=2)
    result = effective_portfolio([addition, original, restated, addition], "1",
                                 "2026-03-31", CUTOFF)
    assert result.complete
    assert sum(r.quantity for r in result.rows) == Decimal(110)
    assert result.reported_total == Decimal(1100)
    assert len(result.accessions) == 3


def test_cutoff_does_not_use_late_amendment():
    corrected = filing([row(quantity="50", value="500")],
                       accession="0000000001-26-000002", filed="2026-09-01",
                       form="13F-HR/A", amendment_type="RESTATEMENT", amendment_number=1)
    result = effective_portfolio([filing(), corrected], "1", "2026-03-31", CUTOFF)
    assert result.rows[0].quantity == Decimal(100)


@pytest.mark.parametrize("update, issue", [
    ({"summary_count": 20}, "ROW_COUNT_MISMATCH"),
    ({"summary_value": "2000"}, "VALUE_TOTAL_MISMATCH"),
    ({"unit_verified": False}, "UNITS_AMBIGUOUS"),
    ({"confidential": True}, "CONFIDENTIAL_HOLDINGS"),
    ({"report_type": "COMBINATION"}, "REPORT_SCOPE_UNRESOLVED"),
    ({"rows": [row(quantity=None)]}, "MISSING_NUMERIC_VALUE"),
])
def test_incomplete_public_data_cannot_be_used_to_infer_exits(update, issue):
    result = effective_portfolio([filing(**update)], "1", "2026-03-31", CUTOFF)
    assert not result.complete
    assert issue in result.issues


def test_unknown_amendment_and_duplicate_original_are_not_guessed():
    amendment = filing(form="13F-HR/A", amendment_number=1,
                       accession="0000000001-26-000002", filed="2026-05-02")
    with pytest.raises(ResearchError, match="AMENDMENT"):
        effective_portfolio([filing(), amendment], "1", "2026-03-31", CUTOFF)


def test_units_are_normalized_once():
    result = effective_portfolio([filing(value_unit="USD_THOUSANDS")],
                                 "1", "2026-03-31", CUTOFF)
    assert result.reported_total == Decimal(1_000_000)
    assert result.rows[0].value == Decimal(1_000_000)


def test_security_variants_keep_separate_keys():
    rows = [row(), row(row_id="2", put_call="PUT", instrument_type="OPTION"),
            row(row_id="3", quantity_type="PRN", instrument_type="OTHER")]
    result = effective_portfolio([filing(rows)], "1", "2026-03-31", CUTOFF)
    assert len({r.security_id for r in result.rows}) == 3


def test_notice_is_missing_not_zero():
    with pytest.raises(ResearchError, match="NO_ELIGIBLE"):
        effective_portfolio([filing(form="13F-NT")], "1", "2026-03-31", CUTOFF)


def test_notice_alongside_holdings_preserves_unresolved_scope():
    notice = filing(form="13F-NT", accession="0000000001-26-000002")
    result = effective_portfolio([filing(), notice], "1", "2026-03-31", CUTOFF)
    assert not result.complete
    assert "REPORT_SCOPE_UNRESOLVED" in result.issues


def test_amendment_sequence_gap_is_not_complete():
    addition = filing([row("000000002")], form="13F-HR/A", amendment_number=3,
                      amendment_type="NEW HOLDINGS",
                      accession="0000000001-26-000003", filed="2026-05-03")
    result = effective_portfolio([filing(), addition], "1", "2026-03-31", CUTOFF)
    assert not result.complete
    assert "AMENDMENT_GAP" in result.issues
