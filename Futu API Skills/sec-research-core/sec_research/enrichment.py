"""Apply explicitly sourced, cutoff-qualified reviews without rewriting raw inputs."""

from copy import deepcopy

from .evidence import available_by, day, fields, require


def _qualified(proof, cutoff):
    return (proof.get("verified") is True and bool(proof.get("source")) and
            bool(proof.get("available_at")) and available_by(proof["available_at"], cutoff))


def review_filings(documents, enrichment, cutoff):
    reviewed = deepcopy(documents)
    unit_reviews = enrichment.get("filing_reviews", {})
    type_reviews = enrichment.get("security_types", {})
    require(isinstance(unit_reviews, dict) and isinstance(type_reviews, dict), "INVALID_REVIEW")
    for document in reviewed:
        proof = unit_reviews.get(document["accession"])
        if proof is not None:
            fields(proof, {"unit", "verified", "source", "available_at"}, {"unit"})
            require(proof["unit"] in {"USD", "USD_THOUSANDS"}, "UNITS_AMBIGUOUS")
            if _qualified(proof, cutoff):
                document["unit_review"] = deepcopy(proof)
                document["value_unit"] = proof["unit"]
                document["unit_verified"] = True
        for row in document["rows"]:
            key = "|".join((str(row["cusip"]).upper(), row["class_title"].strip().upper(),
                            row["quantity_type"], row.get("put_call") or "NONE",
                            row["instrument_type"]))
            proof = type_reviews.get(key)
            if proof is None:
                continue
            fields(proof, {"instrument_type", "verified", "source", "available_at", "as_of"},
                   {"instrument_type", "as_of"})
            require(proof["instrument_type"] in {"COMMON", "ADR", "ETF", "OTHER"},
                    "INVALID_INSTRUMENT")
            if (_qualified(proof, cutoff) and day(proof["as_of"]) <= day(document["report_period"])
                    and row["quantity_type"] == "SH" and row.get("put_call") is None):
                document.setdefault("security_type_reviews", []).append({
                    "row_id": row["row_id"], "original_type": row["instrument_type"],
                    "evidence": deepcopy(proof)})
                row["instrument_type"] = proof["instrument_type"]
    return reviewed
