from decimal import Decimal as D

from sec_research.changes import compare_portfolios
from sec_research.holdings import effective_portfolio
from sample_data import filing, row


def pair(old, new, **old_updates):
    previous = effective_portfolio([filing(old, **old_updates)], "1", "2026-03-31",
                                   "2026-08-31T23:59:59Z")
    current = effective_portfolio([filing(new, report_period="2026-06-30",
                               accession="0000000001-26-000004", filed="2026-08-01")],
                                  "1", "2026-06-30", "2026-08-31T23:59:59Z")
    return previous, current


def checked():
    return {"action_check": {"verified": True, "source": "fixture://actions",
                            "start": "2026-03-31", "end": "2026-06-30",
                            "available_at": "2026-08-01"}}


def test_price_drift_is_not_allocation_change():
    old = [row(), row("000000002", row_id="2")]
    new = [row(value="2000"), row("000000002", row_id="2")]
    result = compare_portfolios(*pair(old, new), checked())
    a = result["changes"][0]
    assert a["allocation_shift"] == 0
    assert a["delta_quantity"] == 0
    assert a["repricing_effect"] == 1000
    assert result["coverage_previous"] == result["coverage_current"] == 1


def test_share_reallocation_has_correct_fixed_price_weights():
    old = [row(), row("000000002", row_id="2")]
    new = [row(quantity="150", value="1500"),
           row("000000002", "50", "500", row_id="2")]
    result = compare_portfolios(*pair(old, new), checked())
    assert [r["allocation_shift"] for r in result["changes"]] == [D(".25"), D("-.25")]
    assert result["concentration_previous"]["hhi"] == D(".5")
    assert result["concentration_current"]["hhi"] == D(".625")
    assert result["reported_weight_overlap"] == D(".75")


def test_split_adjusts_quantity_without_inventing_purchase():
    previous, current = pair([row()], [row(quantity="200")])
    enrichment = checked()
    key = previous.rows[0].security_id
    enrichment["actions"] = {key: {"current_id": key, "factor": "2", "verified": True,
                                   "source": "fixture://split", "effective_date": "2026-05-01"}}
    a = compare_portfolios(previous, current, enrichment)["changes"][0]
    assert a["delta_quantity"] == 0
    assert a["repricing_effect"] == 0


def test_incomplete_previous_report_is_not_new_position():
    result = compare_portfolios(*pair([row()], [row("000000002")], summary_count=20),
                                checked())
    assert {r["status"] for r in result["changes"]} == {"UNVERIFIED"}
    assert not result["eligible"]


def test_exit_without_current_price_is_not_valued_at_zero():
    result = compare_portfolios(*pair([row(), row("000000002", row_id="2")],
                                     [row()]), checked())
    exit_row = result["changes"][1]
    assert exit_row["status"] == "NO_LONGER_REPORTED"
    assert exit_row["quantity_effect_proxy"] is None
    assert exit_row["allocation_shift"] is None
    assert result["coverage_previous"] == D(".5")


def test_quantity_effect_is_a_proxy_not_market_value_change():
    result = compare_portfolios(*pair([row()], [row(quantity="120", value="1800")]),
                                checked())
    a = result["changes"][0]
    assert a["quantity_effect_proxy"] == D(300)
    assert a["repricing_effect"] == D(500)


def test_unchecked_actions_and_future_prices_limit_comparison():
    result = compare_portfolios(*pair([row()], [row(quantity="120")]))
    assert not result["eligible"]
    assert "CORPORATE_ACTIONS_UNVERIFIED" in result["issues"]


def test_zero_implied_price_is_excluded_and_reduces_coverage():
    old = [row(), row("000000002", row_id="2")]
    new = [row(quantity="200", value="0"), row("000000002", "50", "500", row_id="2")]
    result = compare_portfolios(*pair(old, new), checked())
    assert not result["eligible"]
    assert result["coverage_previous"] == D(".5")
    assert result["changes"][0]["allocation_shift"] is None


def test_unresolved_identifier_change_cannot_create_a_new_position():
    previous, current = pair([row()], [row("000000002")])
    enrichment = checked()
    enrichment["actions"] = {previous.rows[0].security_id: {
        "current_id": current.rows[0].security_id, "factor": "1",
        "effective_date": "2026-05-01", "verified": False}}
    result = compare_portfolios(previous, current, enrichment)
    assert {r["status"] for r in result["changes"]} == {"UNVERIFIED"}
