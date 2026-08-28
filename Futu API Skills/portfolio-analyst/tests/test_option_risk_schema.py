from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin

import jsonschema
import pytest
from referencing import Registry, Resource

from portfolio_analyst.option_risk import analyze_option_risk
from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.reporting import build_portfolio_report
from portfolio_analyst.risk import analyze_risk
from portfolio_analyst.schema import ReportSemanticError, validate_option_risk_document

from test_option_risk import Clock, Provider, _position, _snapshot


SCHEMA = Path(__file__).parents[1] / "schemas" / "option_risk.schema.json"
SNAPSHOT_SCHEMA = Path(__file__).parents[1] / "schemas" / "portfolio_snapshot.schema.json"
REPORT_DEFS_SCHEMA = Path(__file__).parents[1] / "schemas" / "report_defs.schema.json"


def _schema_validator():
    documents = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (SCHEMA, SNAPSHOT_SCHEMA, REPORT_DEFS_SCHEMA)
    ]
    registry = Registry().with_resources(
        [(document["$id"], Resource.from_contents(document)) for document in documents]
    )
    return jsonschema.Draft202012Validator(
        documents[0], registry=registry, format_checker=jsonschema.FormatChecker()
    )


def _payload():
    return analyze_option_risk(
        _snapshot(
            (_position("A260116C1"), _position("B260116P1", quantity="-2", market_value="-50"))
        ),
        quote_provider=Provider(
            rows={
                "US.A260116C1": {"implied_volatility": "0.2"},
                "US.B260116P1": {"option_type": "PUT"},
            }
        ),
        clock=Clock(),
    ).to_dict()


def _rehash(payload):
    body = {key: value for key, value in payload.items() if key != "option_risk_id"}
    payload["option_risk_id"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()[:20]


def test_option_risk_schema_is_draft_202012_strict_and_runtime_valid():
    payload = _payload()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    jsonschema.Draft202012Validator.check_schema(schema)
    validate_option_risk_document(payload)

    payload["unexpected"] = True
    with pytest.raises(jsonschema.ValidationError):
        validate_option_risk_document(payload)


def test_option_schema_alone_rejects_600_digit_leg_greek():
    payload = _payload()
    payload["leg_exposures"][0]["greeks"]["delta"] = "1" * 600

    with pytest.raises(jsonschema.ValidationError):
        _schema_validator().validate(payload)


def test_option_schema_alone_rejects_303_character_identity():
    payload = _payload()
    payload["leg_exposures"][0]["identity"] = "US." + "A" * 300

    with pytest.raises(jsonschema.ValidationError):
        _schema_validator().validate(payload)


def test_option_schema_external_ref_graph_is_recursively_bounded():
    documents = {
        document["$id"]: document
        for document in (
            json.loads(SCHEMA.read_text(encoding="utf-8")),
            json.loads(SNAPSHOT_SCHEMA.read_text(encoding="utf-8")),
            json.loads(REPORT_DEFS_SCHEMA.read_text(encoding="utf-8")),
        )
    }
    missing = []
    visited_refs = set()
    visited_resources = set()
    active_refs = set()
    detected_cycles = set()

    def resolve(reference, base_uri):
        absolute = urljoin(base_uri, reference)
        resource_uri, fragment = urldefrag(absolute)
        target = documents[resource_uri]
        if fragment:
            for token in fragment.removeprefix("/").split("/"):
                token = unquote(token).replace("~1", "/").replace("~0", "~")
                target = target[token]
        return resource_uri, fragment, target

    def visit(node, base_uri, path="$"):
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            resource_uri, fragment, target = resolve(node["$ref"], base_uri)
            ref_key = (resource_uri, fragment)
            if ref_key in active_refs:
                detected_cycles.add(ref_key)
            elif ref_key not in visited_refs:
                visited_refs.add(ref_key)
                visited_resources.add(resource_uri)
                active_refs.add(ref_key)
                visit(target, resource_uri, f"{path}/$ref->{resource_uri}#{fragment}")
                active_refs.remove(ref_key)
        node_type = node.get("type")
        if node_type == "object":
            if "maxProperties" not in node:
                missing.append(f"{path}:maxProperties")
            if isinstance(node.get("additionalProperties"), dict) and "propertyNames" not in node:
                missing.append(f"{path}:propertyNames")
        if (
            node_type == "array"
            and "maxItems" not in node
            and not (node.get("items") is False and isinstance(node.get("prefixItems"), list))
        ):
            missing.append(f"{path}:maxItems")
        if node_type == "string" or (isinstance(node_type, list) and "string" in node_type):
            finite_literal = isinstance(node.get("const"), str) or (
                isinstance(node.get("enum"), list)
                and node["enum"]
                and all(not isinstance(item, str) or len(item) <= 512 for item in node["enum"])
            )
            if "maxLength" not in node and not finite_literal:
                missing.append(f"{path}:maxLength")
        for key in ("properties", "$defs", "patternProperties", "dependentSchemas"):
            for name, child in node.get(key, {}).items():
                visit(child, base_uri, f"{path}/{key}/{name}")
        for key in (
            "items",
            "additionalProperties",
            "propertyNames",
            "contains",
            "if",
            "then",
            "else",
            "not",
        ):
            visit(node.get(key), base_uri, f"{path}/{key}")
        for key in ("oneOf", "anyOf", "allOf", "prefixItems"):
            for index, child in enumerate(node.get(key, [])):
                visit(child, base_uri, f"{path}/{key}/{index}")

    option_uri = next(uri for uri in documents if uri.endswith("option_risk.schema.json"))
    visited_resources.add(option_uri)
    visit(documents[option_uri], option_uri, "option_risk.schema.json")
    assert {uri.rsplit("/", 1)[-1] for uri in visited_resources} == {
        "option_risk.schema.json",
        "portfolio_snapshot.schema.json",
        "report_defs.schema.json",
    }
    assert any(fragment == "/$defs/boundedJson" for _uri, fragment in detected_cycles)
    assert missing == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.__setitem__("option_risk_id", "0" * 20),
        lambda body: body.__setitem__("status", "PARTIAL"),
        lambda body: body["coverage"].__setitem__("option_legs", 99),
        lambda body: body["source_population"].__setitem__("gross_market_value_base", "999"),
        lambda body: body["source_population"]["contracts"][0].__setitem__("selection_rank", 2),
        lambda body: body["leg_exposures"][0]["greeks"].__setitem__("delta", "999"),
        lambda body: body["greek_aggregates"]["delta"].__setitem__("net", "999"),
        lambda body: body["expiry_buckets"][0]["dte_by_market"][0].__setitem__("dte", 999),
        lambda body: body["quote_evidence"][0]["request"].__setitem__("action", "SELL"),
        lambda body: body["quote_evidence"][0]["response"]["greeks"].__setitem__("delta", "0.9"),
        lambda body: body["provenance"].__setitem__("greek_unit", "OTHER"),
        lambda body: body["quality_reasons"].append(
            {"code": "FORGED", "scope": "x", "detail": "x"}
        ),
        lambda body: body.__setitem__("source_snapshot_digest", "0" * 64),
        lambda body: body["provenance"]["source_snapshot"].__setitem__("nav_base", "999"),
    ],
)
def test_runtime_rejects_derived_source_quote_and_identity_tampering(mutate):
    payload = deepcopy(_payload())
    mutate(payload)

    with pytest.raises((ReportSemanticError, jsonschema.ValidationError)):
        validate_option_risk_document(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["implied_volatility_coverage"].__setitem__("available_legs", 0),
        lambda body: body["implied_volatility_coverage"].__setitem__("status", "UNAVAILABLE"),
        lambda body: body["quality_reasons"].append(
            {
                "code": "IMPLIED_VOLATILITY_COVERAGE_INCOMPLETE",
                "scope": "implied_volatility",
                "detail": "forged",
            }
        ),
        lambda body: body["limitations"].__setitem__(3, "forged IV interpretation"),
    ],
)
def test_runtime_rejects_rehashed_iv_coverage_and_limitation_tampering(mutate):
    payload = deepcopy(_payload())
    mutate(payload)
    _rehash(payload)

    with pytest.raises((ReportSemanticError, jsonschema.ValidationError)):
        validate_option_risk_document(payload)


@pytest.mark.parametrize("seconds", [6, 34])
def test_runtime_rejects_rehashed_quote_windows_over_hard_limits(seconds):
    payload = deepcopy(_payload())
    started = payload["quote_evidence"][0]["request_started_at"]
    from datetime import datetime

    instant = datetime.fromisoformat(started.replace("Z", "+00:00")) + timedelta(seconds=seconds)
    payload["quote_evidence"][0]["request_finished_at"] = instant.isoformat().replace("+00:00", "Z")
    _rehash(payload)

    with pytest.raises(ReportSemanticError, match="window"):
        validate_option_risk_document(payload)


def test_runtime_rejects_rehashed_failure_class_and_byte_count_tampering():
    payload = deepcopy(_payload())
    payload["quote_evidence"][0]["reason_code"] = "QUOTE_SOURCE_UNAVAILABLE"
    payload["quote_evidence"][0]["status"] = "FAILURE"
    payload["quote_evidence"][0]["response"] = None
    payload["quote_evidence"][0]["source_response"] = None
    payload["quote_evidence"][0]["response_byte_count"] = 0
    _rehash(payload)
    with pytest.raises(ReportSemanticError):
        validate_option_risk_document(payload)

    payload = deepcopy(_payload())
    payload["quote_evidence"][0]["response_byte_count"] += 1
    _rehash(payload)
    with pytest.raises(ReportSemanticError, match="byte"):
        validate_option_risk_document(payload)


def test_runtime_rejects_future_embedded_snapshot_even_when_rehashed():
    payload = deepcopy(_payload())
    source = _snapshot((_position("A260116C1"),))
    future_as_of = source.as_of + timedelta(days=2)
    future = replace(
        source,
        as_of=future_as_of,
        fx_evidence=tuple(replace(item, effective_at=future_as_of) for item in source.fx_evidence),
        snapshot_id="",
    )
    future = replace(future, snapshot_id=snapshot_identity(future))
    source_body = future.to_dict()
    payload["provenance"]["source_snapshot"] = source_body
    payload["source_snapshot_id"] = future.snapshot_id
    payload["source_snapshot_digest"] = hashlib.sha256(
        json.dumps(source_body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload["as_of"] = source_body["as_of"]
    _rehash(payload)

    with pytest.raises(ReportSemanticError, match="follows analysis_now"):
        validate_option_risk_document(payload)


def test_comprehensive_report_embeds_and_binds_full_option_report():
    source = _snapshot((_position("A260116C1"),))
    option_report = analyze_option_risk(source, quote_provider=Provider(), clock=Clock())
    portfolio = build_portfolio_report(
        source,
        analyze_risk(source),
        options=option_report,
    ).to_dict()

    assert portfolio["options"] == option_report.to_dict()
    markdown = build_portfolio_report(
        source, analyze_risk(source), options=option_report
    ).to_markdown()
    assert "RAW_CONTRACT_UNITS_NO_MULTIPLIER" in markdown
    from portfolio_analyst.schema import validate_portfolio_report_document

    validate_portfolio_report_document(portfolio)

    forged = deepcopy(portfolio)
    forged["options"]["base_currency"] = "HKD"
    with pytest.raises((ReportSemanticError, jsonschema.ValidationError)):
        validate_portfolio_report_document(forged)


def test_phase6_version_and_packaged_schema_are_0_6_0():
    root = Path(__file__).parents[1]
    project_text = (root / "pyproject.toml").read_text()
    from portfolio_analyst import __version__

    assert __version__ == "0.6.0"
    assert 'version = "0.6.0"' in project_text
    assert "schemas/option_risk.schema.json" in project_text
    assert "version: 0.6.0" in (root / "SKILL.md").read_text()
