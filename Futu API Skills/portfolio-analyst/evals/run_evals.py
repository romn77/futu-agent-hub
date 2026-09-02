#!/usr/bin/env python3
"""Validate assertions in externally produced Portfolio Analyst audit envelopes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any


MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_ASSERTIONS_PER_CASE = 1_000
ALLOWED_ASSERTIONS = frozenset({"contains", "not_contains", "equals", "count_equals"})
CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PATH_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROVENANCE_FIELDS = frozenset(
    {
        "producer_kind",
        "runner_id",
        "runner_version",
        "generated_at",
        "case_id",
        "case_input_sha256",
    }
)


class EvalInputError(ValueError):
    """Raised for malformed eval definitions or audit envelopes."""


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise EvalInputError(f"{label} cannot be read: {path}") from exc
    if len(body) > MAX_DOCUMENT_BYTES:
        raise EvalInputError(f"{label} exceeds {MAX_DOCUMENT_BYTES} bytes")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvalInputError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise EvalInputError(f"{label} root must be an object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvalInputError(f"{label} must be a non-empty string")
    return value


def _validate_case_id(value: Any, label: str) -> str:
    case_id = _require_string(value, label)
    if CASE_ID_RE.fullmatch(case_id) is None:
        raise EvalInputError(f"{label} has an invalid format")
    return case_id


def case_input_sha256(case: Mapping[str, Any]) -> str:
    """Hash the immutable case input bound to an external audit envelope."""

    body = json.dumps(
        {
            "id": case["id"],
            "prompt": case["prompt"],
            "fixture": case.get("fixture"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def load_eval_cases(path: Path) -> dict[str, dict[str, Any]]:
    document = _load_json(path, "eval document")
    if document.get("schema_version") != "1.0":
        raise EvalInputError("eval document schema_version must be 1.0")
    _require_string(document.get("skill_name"), "eval document skill_name")
    cases = document.get("evals")
    if not isinstance(cases, list) or not cases:
        raise EvalInputError("eval document evals must be a non-empty array")

    indexed: dict[str, dict[str, Any]] = {}
    for case_index, case in enumerate(cases):
        label = f"evals[{case_index}]"
        if not isinstance(case, dict):
            raise EvalInputError(f"{label} must be an object")
        case_id = _validate_case_id(case.get("id"), f"{label}.id")
        if case_id in indexed:
            raise EvalInputError(f"duplicate eval case id: {case_id}")
        _require_string(case.get("prompt"), f"{label}.prompt")
        expected = case.get("expected_behavior")
        if not isinstance(expected, list) or not expected:
            raise EvalInputError(f"{label}.expected_behavior must be a non-empty array")
        for item_index, item in enumerate(expected):
            _require_string(item, f"{label}.expected_behavior[{item_index}]")

        assertions = case.get("assertions")
        if (
            not isinstance(assertions, list)
            or not assertions
            or len(assertions) > MAX_ASSERTIONS_PER_CASE
        ):
            raise EvalInputError(
                f"{label}.assertions must contain 1..{MAX_ASSERTIONS_PER_CASE} items"
            )
        for assertion_index, assertion in enumerate(assertions):
            assertion_label = f"{label}.assertions[{assertion_index}]"
            if not isinstance(assertion, dict) or set(assertion) != {
                "type",
                "target",
                "value",
            }:
                raise EvalInputError(f"{assertion_label} must contain only type, target, and value")
            assertion_type = assertion["type"]
            if assertion_type not in ALLOWED_ASSERTIONS:
                raise EvalInputError(f"{assertion_label}.type is unsupported")
            target = _require_string(assertion["target"], f"{assertion_label}.target")
            parts = target.split(".")
            if parts[0] not in {"response_markdown", "trace"} or any(
                PATH_PART_RE.fullmatch(part) is None for part in parts
            ):
                raise EvalInputError(f"{assertion_label}.target is not a valid audit path")
            if assertion_type == "count_equals" and (
                type(assertion["value"]) is not int or assertion["value"] < 0
            ):
                raise EvalInputError(f"{assertion_label}.value must be a non-negative integer")
        indexed[case_id] = case
    return indexed


def load_audit_envelope(path: Path) -> dict[str, Any]:
    audit = _load_json(path, "audit envelope")
    if audit.get("schema_version") != "1.0":
        raise EvalInputError("audit envelope schema_version must be 1.0")
    case_id = _validate_case_id(audit.get("case_id"), "audit envelope case_id")
    provenance = audit.get("provenance")
    if not isinstance(provenance, dict):
        raise EvalInputError("audit envelope provenance must be an object")
    if set(provenance) != PROVENANCE_FIELDS:
        raise EvalInputError(
            "audit envelope provenance must contain only producer_kind, runner_id, "
            "runner_version, generated_at, case_id, and case_input_sha256"
        )
    if provenance["producer_kind"] != "external_harness":
        raise EvalInputError("audit envelope provenance producer_kind must be external_harness")
    _validate_case_id(provenance["runner_id"], "audit envelope provenance runner_id")
    _require_string(provenance["runner_version"], "audit envelope provenance runner_version")
    provenance_case_id = _validate_case_id(
        provenance["case_id"], "audit envelope provenance case_id"
    )
    if provenance_case_id != case_id:
        raise EvalInputError("audit envelope provenance case_id does not match case_id")
    generated_at = _require_string(
        provenance["generated_at"], "audit envelope provenance generated_at"
    )
    try:
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvalInputError(
            "audit envelope provenance generated_at must be an ISO-8601 timestamp"
        ) from exc
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise EvalInputError(
            "audit envelope provenance generated_at must include a timezone offset"
        )
    if (
        not isinstance(provenance["case_input_sha256"], str)
        or SHA256_RE.fullmatch(provenance["case_input_sha256"]) is None
    ):
        raise EvalInputError(
            "audit envelope provenance case_input_sha256 must be lowercase SHA-256"
        )
    _require_string(audit.get("response_markdown"), "audit envelope response_markdown")
    trace = audit.get("trace")
    if not isinstance(trace, dict):
        raise EvalInputError("audit envelope trace must be an object")
    for field in ("snapshot", "observations"):
        if not isinstance(trace.get(field), dict):
            raise EvalInputError(f"audit envelope trace.{field} must be an object")
    for field in ("loaded_references", "executed_commands"):
        value = trace.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise EvalInputError(f"audit envelope trace.{field} must be a string array")
    return audit


def _resolve_dotted(document: Mapping[str, Any], target: str) -> tuple[bool, Any]:
    current: Any = document
    for part in target.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _strict_equal(actual: Any, expected: Any) -> bool:
    return type(actual) is type(expected) and actual == expected


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str):
        return isinstance(expected, str) and expected in actual
    if isinstance(actual, Mapping):
        return any(_strict_equal(key, expected) for key in actual)
    if isinstance(actual, Sequence) and not isinstance(actual, (str, bytes, bytearray)):
        for item in actual:
            if _strict_equal(item, expected):
                return True
            if isinstance(item, str) and isinstance(expected, str) and expected in item:
                return True
        return False
    return False


def _evaluate_assertion(assertion: Mapping[str, Any], audit: Mapping[str, Any]) -> tuple[bool, Any]:
    found, actual = _resolve_dotted(audit, assertion["target"])
    if not found:
        return False, {"missing_path": assertion["target"]}
    assertion_type = assertion["type"]
    expected = assertion["value"]
    if assertion_type == "equals":
        return _strict_equal(actual, expected), actual
    if assertion_type == "contains":
        return _contains(actual, expected), actual
    if assertion_type == "not_contains":
        return not _contains(actual, expected), actual
    try:
        actual_count = len(actual)
    except TypeError:
        return False, {"not_countable": actual}
    return _strict_equal(actual_count, expected), actual_count


def evaluate_case(case: Mapping[str, Any], audit: Mapping[str, Any]) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    for index, assertion in enumerate(case["assertions"]):
        satisfied, actual = _evaluate_assertion(assertion, audit)
        evidence.append(
            {
                "index": index,
                "type": assertion["type"],
                "target": assertion["target"],
                "expected": assertion["value"],
                "actual": actual,
                "satisfied": satisfied,
            }
        )
    return {
        "case_id": case["id"],
        "assertions_satisfied": all(item["satisfied"] for item in evidence),
        "assertions": evidence,
    }


def run(evals_path: Path, audit_paths: list[Path], *, require_all: bool = False) -> dict[str, Any]:
    cases = load_eval_cases(evals_path)
    audits: dict[str, dict[str, Any]] = {}
    for path in audit_paths:
        audit = load_audit_envelope(path)
        case_id = audit["case_id"]
        if case_id not in cases:
            raise EvalInputError(f"unknown case_id in audit envelope: {case_id}")
        if case_id in audits:
            raise EvalInputError(f"duplicate audit envelope for case_id: {case_id}")
        if audit["provenance"]["case_input_sha256"] != case_input_sha256(cases[case_id]):
            raise EvalInputError(
                f"audit envelope provenance case_input_sha256 does not match case: {case_id}"
            )
        audits[case_id] = audit
    if require_all:
        missing = [case_id for case_id in cases if case_id not in audits]
        if missing:
            raise EvalInputError(f"missing audit envelopes for case_ids: {', '.join(missing)}")

    results = [evaluate_case(cases[case_id], audit) for case_id, audit in audits.items()]
    total_assertions = sum(len(case["assertions"]) for case in results)
    satisfied_assertions = sum(item["satisfied"] for case in results for item in case["assertions"])
    satisfied_cases = sum(case["assertions_satisfied"] for case in results)
    return {
        "schema_version": "1.0",
        "validator_scope": "EXTERNAL_AUDIT_ENVELOPE_ONLY",
        "skill_execution_performed_by_validator": False,
        "audit_validation_succeeded": all(case["assertions_satisfied"] for case in results),
        "cases": results,
        "summary": {
            "total_cases": len(results),
            "satisfied_cases": satisfied_cases,
            "unsatisfied_cases": len(results) - satisfied_cases,
            "total_assertions": total_assertions,
            "satisfied_assertions": satisfied_assertions,
            "unsatisfied_assertions": total_assertions - satisfied_assertions,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evals", type=Path, default=Path(__file__).with_name("evals.json"))
    parser.add_argument("--audit", type=Path, action="append", required=True)
    parser.add_argument(
        "--require-all", action="store_true", help="require one audit envelope for every eval case"
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = run(args.evals, args.audit, require_all=args.require_all)
    except EvalInputError as exc:
        result = {
            "schema_version": "1.0",
            "validator_scope": "EXTERNAL_AUDIT_ENVELOPE_ONLY",
            "skill_execution_performed_by_validator": False,
            "audit_validation_succeeded": False,
            "error": str(exc),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["audit_validation_succeeded"] else 1


if __name__ == "__main__":
    sys.exit(main())
