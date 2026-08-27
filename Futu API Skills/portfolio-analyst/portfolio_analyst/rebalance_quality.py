"""Canonical, self-verifying rebalance quality notes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def canonical_rebalance_quality_notes(
    data_quality: Any, simulation_warnings: Iterable[str]
) -> tuple[str, ...]:
    """Map embedded snapshot quality and simulation warnings to stable safe templates."""

    notes = [
        *(f"source snapshot warning: {item}" for item in data_quality.warnings),
        *(f"source snapshot missing field: {item}" for item in data_quality.missing_fields),
        *(f"source snapshot error: {item}" for item in data_quality.source_errors),
        *(f"simulation warning: {item}" for item in simulation_warnings),
    ]
    if data_quality.status != "COMPLETE" and not (
        data_quality.warnings or data_quality.missing_fields or data_quality.source_errors
    ):
        notes.insert(0, f"source snapshot data quality status: {data_quality.status}")
    return tuple(dict.fromkeys(notes))
