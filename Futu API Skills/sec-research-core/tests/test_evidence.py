from decimal import Decimal

import pytest

from sec_research.evidence import ResearchError, available_by, number, quarterly_total


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "abc"])
def test_invalid_numbers_never_turn_into_zero(value):
    with pytest.raises(ResearchError):
        number(value)


def test_missing_is_not_zero():
    assert number(None) is None
    assert number(0) == Decimal(0)


def test_date_only_filing_is_not_claimed_available_intraday():
    assert not available_by("2026-08-14", "2026-08-14T15:00:00Z")
    assert available_by("2026-08-14", "2026-08-15T04:00:00Z")


def quarters():
    return [
        dict(value="10", start=start, end=end, concept="revenue", unit="USD",
             basis="GAAP", period_kind="standalone", aggregation="additive")
        for start, end in [("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30"),
                           ("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31")]
    ]


def test_ttm_requires_actual_consecutive_standalone_periods():
    assert quarterly_total(quarters()) == Decimal(40)
    facts = quarters()
    facts[2]["period_kind"] = "cumulative"
    with pytest.raises(ResearchError, match="standalone"):
        quarterly_total(facts)


def test_ttm_rejects_gap_and_mismatched_units():
    facts = quarters()
    facts[1]["start"] = "2025-04-02"
    with pytest.raises(ResearchError):
        quarterly_total(facts)
    facts = quarters()
    facts[2]["unit"] = "EUR"
    with pytest.raises(ResearchError):
        quarterly_total(facts)


def test_per_share_and_unknown_aggregation_are_not_summed():
    facts = quarters()
    for fact in facts:
        fact.update(concept="EarningsPerShareDiluted", unit="USD/shares")
    with pytest.raises(ResearchError, match="NONADDITIVE"):
        quarterly_total(facts)
    facts = quarters()
    facts[0].pop("aggregation")
    with pytest.raises(ResearchError, match="NONADDITIVE"):
        quarterly_total(facts)
