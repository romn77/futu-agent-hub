"""Runtime validation against the published snapshot schema."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema


def _schema_path(name: str = "portfolio_snapshot.schema.json") -> Path:
    candidates = (
        Path(__file__).resolve().parents[1] / "schemas" / name,
        Path(sys.prefix) / "schemas" / name,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"{name} is not installed")


def validate_snapshot_document(payload: dict[str, Any]) -> None:
    schema = json.loads(_schema_path().read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    validator.validate(payload)


def _validate(payload: dict[str, Any], name: str) -> None:
    schema = json.loads(_schema_path(name).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    validator.validate(payload)


def validate_risk_document(payload: dict[str, Any]) -> None:
    _validate(payload, "risk_report.schema.json")


def validate_portfolio_report_document(payload: dict[str, Any]) -> None:
    _validate(payload, "portfolio_report.schema.json")
