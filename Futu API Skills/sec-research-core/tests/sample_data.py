def row(cusip="000000001", quantity="100", value="1000", **updates):
    result = {"row_id": "1", "cusip": cusip, "issuer": "Synthetic issuer",
              "class_title": "COM", "quantity_type": "SH", "put_call": None,
              "quantity": quantity, "reported_value": value,
              "instrument_type": "COMMON", "investment_discretion": "SOLE",
              "other_managers": [], "voting_authority": {}}
    result.update(updates)
    return result


def filing(rows=None, **updates):
    from decimal import Decimal
    rows = [row()] if rows is None else rows
    result = {"manager_cik": "1", "accession": "0000000001-26-000001",
              "form": "13F-HR", "report_period": "2026-03-31", "filed": "2026-05-01",
              "report_type": "HOLDINGS", "amendment_type": None, "amendment_number": None,
              "summary_count": len(rows), "summary_value": str(sum(
                  Decimal(r["reported_value"] or "0") for r in rows)),
              "value_unit": "USD", "unit_verified": True, "confidential": False,
              "rows": rows, "source_url": "https://www.sec.gov/Archives/synthetic",
              "discovery_complete": True}
    result.update(updates)
    return result
