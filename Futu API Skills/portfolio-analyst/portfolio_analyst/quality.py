"""Deterministic portfolio data-quality composition."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .models import DataQuality


_SEVERITY = {
    "COMPLETE": 0,
    "RECONSTRUCTED": 1,
    "STALE": 2,
    "PARTIAL": 3,
    "UNAVAILABLE": 4,
    "ERROR": 5,
}


def compose_quality(
    base: DataQuality,
    *,
    reconstructed: bool = False,
    stale: bool = False,
    missing_fields: Iterable[str] = (),
    warnings: Iterable[str] = (),
) -> DataQuality:
    """Compose independent degradations using one documented severity order."""

    missing = tuple(dict.fromkeys((*base.missing_fields, *missing_fields)))
    stale_present = stale or base.status == "STALE"
    reconstructed_present = reconstructed or base.status == "RECONSTRUCTED"
    automatic_warnings = []
    if stale_present:
        automatic_warnings.append("one or more source inputs are STALE")
    if reconstructed_present:
        automatic_warnings.append("one or more source values were RECONSTRUCTED")
    combined_warnings = tuple(dict.fromkeys((*base.warnings, *automatic_warnings, *warnings)))
    candidates = [base.status]
    if reconstructed_present:
        candidates.append("RECONSTRUCTED")
    if stale_present:
        candidates.append("STALE")
    if missing:
        candidates.append("PARTIAL")
    if base.source_errors and base.status not in {"ERROR", "UNAVAILABLE"}:
        candidates.append("PARTIAL")
    status = max(candidates, key=_SEVERITY.__getitem__)
    return replace(base, status=status, missing_fields=missing, warnings=combined_warnings)
