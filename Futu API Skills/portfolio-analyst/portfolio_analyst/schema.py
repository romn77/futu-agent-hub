"""Runtime validation against the published snapshot schema."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema


def _schema_path() -> Path:
    candidates = (
        Path(__file__).resolve().parents[1] / "schemas" / "portfolio_snapshot.schema.json",
        Path(sys.prefix) / "schemas" / "portfolio_snapshot.schema.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("portfolio_snapshot.schema.json is not installed")


def validate_snapshot_document(payload: dict[str, Any]) -> None:
    schema = json.loads(_schema_path().read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    validator.validate(payload)
