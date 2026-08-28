"""Conservative inventory of raw per-contract option Greeks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

from .models import PortfolioSnapshot, _deep_freeze, _json_value
from .option_quote import GREEKS, RAW_GREEK_UNIT, OptionQuote, parse_option_quote_response
from .skill_trust import OPTION_QUOTE_SHA256


MAX_SOURCE_POSITIONS = 10_000
MAX_QUOTE_CONTRACTS = 30
PER_CALL_TIMEOUT_SECONDS = 5.0
TOTAL_WALL_SECONDS = 30.0
CIRCUIT_BREAKER_FAILURES = 3
MAX_SINGLE_RESPONSE_BYTES = 256 * 1024
MAX_TOTAL_RESPONSE_BYTES = 4 * 1024 * 1024
_MARKET_TIMEZONES = {"US": ZoneInfo("America/New_York"), "HK": ZoneInfo("Asia/Hong_Kong")}


class OptionRiskInputError(ValueError):
    pass


class OptionQuoteProvider(Protocol):
    def quote(self, identity: str, *, timeout: float) -> object: ...


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _bounded_response_bytes(value: object) -> bytes:
    def encode_unknown(item: object) -> str:
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError("option response contains a non-JSON value")

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        default=encode_unknown,
    ).encode("utf-8")


def _reason(code: str, scope: str, detail: str) -> dict[str, str]:
    return {"code": code, "scope": scope, "detail": detail}


@dataclass(frozen=True)
class OptionRiskReport:
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _deep_freeze(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self.payload)

    def to_markdown(self) -> str:
        data = self.to_dict()
        lines = [
            "# Option risk inventory",
            "",
            f"- option_risk_id: `{data['option_risk_id']}`",
            f"- source_snapshot_id: `{data['source_snapshot_id']}`",
            f"- As of: {data['as_of']}",
            f"- Analysis now: {data['analysis_now']}",
            f"- Status: {data['status']}",
            f"- Greek unit: {RAW_GREEK_UNIT}",
            "- Quote freshness: UNVERIFIED (request windows only)",
            "",
            "## Source population",
            "",
            f"- Option legs: {data['coverage']['option_legs']}",
            f"- Unique contracts: {data['coverage']['unique_contracts']}",
            f"- Net market value: {data['source_population']['net_market_value_base']} {data['base_currency']}",
            f"- Gross market value: {data['source_population']['gross_market_value_base']} {data['base_currency']}",
            f"- Gross/NAV: {data['source_population']['gross_to_nav']}",
            "",
            "## Raw Greek aggregates",
            "",
        ]
        for name in GREEKS:
            item = data["greek_aggregates"][name]
            lines.append(
                f"- {name.title()}: {item['status']}; net={item['net']}; gross_abs={item['gross_abs']}; "
                f"coverage={item['coverage']['available_legs']}/{item['coverage']['total_legs']}"
            )
        lines.extend(["", "## Limitations", "", *[f"- {item}" for item in data["limitations"]]])
        return "\n".join(lines) + "\n"


def _identity(position: object) -> str:
    return f"{position.market}.{position.symbol}"  # type: ignore[attr-defined]


def _safe_clock(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OptionRiskInputError("trusted option analysis clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _selection(snapshot: PortfolioSnapshot) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    option_legs: list[dict[str, Any]] = []
    by_identity: dict[str, dict[str, Any]] = {}
    for source_index, position in enumerate(snapshot.positions):
        if position.quantity == 0 or position.security_type != "OPTION":
            continue
        identity = _identity(position)
        leg = {
            "source_index": source_index,
            "identity": identity,
            "market": position.market,
            "account_ref": position.account_ref,
            "quantity": position.quantity,
            "direction": "LONG" if position.quantity > 0 else "SHORT",
            "market_value_base": position.market_value_base,
        }
        option_legs.append(leg)
        contract = by_identity.setdefault(
            identity,
            {
                "identity": identity,
                "market": position.market,
                "gross_abs_market_value_base": Decimal(0),
                "leg_count": 0,
            },
        )
        contract["gross_abs_market_value_base"] += abs(position.market_value_base)
        contract["leg_count"] += 1
    option_legs.sort(key=lambda item: (item["identity"], item["account_ref"], item["source_index"]))
    contracts = sorted(
        by_identity.values(),
        key=lambda item: (-item["gross_abs_market_value_base"], item["identity"]),
    )
    supported_rank = 0
    for contract in contracts:
        if contract["market"] not in _MARKET_TIMEZONES:
            contract["selection_status"] = "UNSUPPORTED_MARKET"
            contract["selection_rank"] = None
        else:
            supported_rank += 1
            contract["selection_rank"] = supported_rank
            contract["selection_status"] = (
                "SELECTED" if supported_rank <= MAX_QUOTE_CONTRACTS else "BUDGET_OMITTED"
            )
    contracts.sort(key=lambda item: item["identity"])
    return option_legs, contracts


def _collect_quotes(
    contracts: list[dict[str, Any]],
    provider: OptionQuoteProvider | None,
    clock: Callable[[], datetime],
    analysis_now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, OptionQuote]]:
    evidence: list[dict[str, Any]] = []
    quotes: dict[str, OptionQuote] = {}
    consecutive_failures = 0
    total_bytes = 0
    for contract in sorted(
        (item for item in contracts if item["selection_status"] == "SELECTED"),
        key=lambda item: item["selection_rank"],
    ):
        identity = contract["identity"]
        base = {
            "identity": identity,
            "selection_rank": contract["selection_rank"],
            "request": {"action": "BUY", "quantity": "1.0"},
            "freshness": "UNVERIFIED",
        }
        if provider is None:
            instant = _utc_text(analysis_now)
            evidence.append(
                {
                    **base,
                    "request_started_at": instant,
                    "request_finished_at": instant,
                    "outcome": "SOURCE_UNAVAILABLE",
                    "response": None,
                }
            )
            continue
        started = _safe_clock(clock)
        if consecutive_failures >= CIRCUIT_BREAKER_FAILURES:
            instant = _utc_text(started)
            evidence.append(
                {
                    **base,
                    "request_started_at": instant,
                    "request_finished_at": instant,
                    "outcome": "CIRCUIT_OPEN",
                    "response": None,
                }
            )
            continue
        if (started - analysis_now).total_seconds() >= TOTAL_WALL_SECONDS:
            instant = _utc_text(started)
            evidence.append(
                {
                    **base,
                    "request_started_at": instant,
                    "request_finished_at": instant,
                    "outcome": "WALL_BUDGET_OMITTED",
                    "response": None,
                }
            )
            continue
        outcome = "SUCCESS"
        response = None
        try:
            raw = provider.quote(identity, timeout=PER_CALL_TIMEOUT_SECONDS)
            encoded = _bounded_response_bytes(raw)
            if len(encoded) > MAX_SINGLE_RESPONSE_BYTES:
                raise ValueError("single response budget exceeded")
            total_bytes += len(encoded)
            if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
                raise ValueError("total response budget exceeded")
            quote = parse_option_quote_response(identity, raw)
            response = quote.to_dict()
            quotes[identity] = quote
            consecutive_failures = 0
        except Exception:
            outcome = "FAILED"
            consecutive_failures += 1
        finished = _safe_clock(clock)
        evidence.append(
            {
                **base,
                "request_started_at": _utc_text(started),
                "request_finished_at": _utc_text(finished),
                "outcome": outcome,
                "response": response,
            }
        )
    return evidence, quotes


def analyze_option_risk(
    snapshot: PortfolioSnapshot,
    *,
    quote_provider: OptionQuoteProvider | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OptionRiskReport:
    if len(snapshot.positions) > MAX_SOURCE_POSITIONS:
        raise OptionRiskInputError("option source position hard cap exceeded")
    analysis_now = _safe_clock(clock)
    option_legs, contracts = _selection(snapshot)
    unknown_count = sum(
        1
        for position in snapshot.positions
        if position.quantity != 0 and position.security_type == "UNKNOWN"
    )
    future = snapshot.as_of > analysis_now
    quote_evidence: list[dict[str, Any]] = []
    quotes: dict[str, OptionQuote] = {}
    if option_legs and not future:
        quote_evidence, quotes = _collect_quotes(contracts, quote_provider, clock, analysis_now)

    reasons: list[dict[str, str]] = []
    if future:
        reasons.append(
            _reason("FUTURE_SNAPSHOT", "snapshot", "snapshot as_of follows trusted analysis_now")
        )
    if unknown_count:
        reasons.append(
            _reason(
                "UNKNOWN_SECURITY_TYPE",
                "source_population",
                "nonzero positions with UNKNOWN security_type may conceal option legs",
            )
        )
    unsupported = [item for item in contracts if item["selection_status"] == "UNSUPPORTED_MARKET"]
    if unsupported:
        reasons.append(
            _reason("UNSUPPORTED_MARKET", "source_population", "only US and HK options are quoted")
        )
    budget_omitted = [item for item in contracts if item["selection_status"] == "BUDGET_OMITTED"]
    if budget_omitted:
        reasons.append(
            _reason(
                "QUOTE_BUDGET_OMITTED",
                "quote_budget",
                "contracts beyond the deterministic 30-contract quote budget were omitted",
            )
        )
    outcomes = {item["outcome"] for item in quote_evidence}
    if "SOURCE_UNAVAILABLE" in outcomes:
        reasons.append(
            _reason("QUOTE_SOURCE_UNAVAILABLE", "live_source", "option quote provider unavailable")
        )
    if "FAILED" in outcomes:
        reasons.append(_reason("QUOTE_REQUEST_FAILED", "live_source", "one or more quotes failed"))
    if "CIRCUIT_OPEN" in outcomes:
        reasons.append(
            _reason("QUOTE_CIRCUIT_OPEN", "live_source", "source failure circuit breaker opened")
        )
    if "WALL_BUDGET_OMITTED" in outcomes:
        reasons.append(
            _reason("QUOTE_WALL_BUDGET", "quote_budget", "total quote wall budget was exhausted")
        )
    if snapshot.data_quality.status != "COMPLETE":
        reasons.append(
            _reason(
                "SOURCE_SNAPSHOT_INCOMPLETE",
                "snapshot",
                f"source snapshot quality is {snapshot.data_quality.status}",
            )
        )

    full_population_authority = (
        not future
        and unknown_count == 0
        and not unsupported
        and not budget_omitted
        and snapshot.data_quality.status == "COMPLETE"
    )
    leg_exposures: list[dict[str, Any]] = []
    for leg in option_legs:
        quote = quotes.get(leg["identity"])
        greek_values = {
            name: None
            if quote is None or quote.greeks[name] is None
            else leg["quantity"] * quote.greeks[name]
            for name in GREEKS
        }
        leg_exposures.append(
            {
                **leg,
                "unit": RAW_GREEK_UNIT,
                "option_type": None if quote is None else quote.option_type,
                "expiry": None
                if quote is None or quote.expiry is None
                else quote.expiry.isoformat(),
                "strike": None if quote is None else quote.strike,
                "implied_volatility": None if quote is None else quote.implied_volatility,
                "greeks": greek_values,
            }
        )

    greek_aggregates: dict[str, dict[str, Any]] = {}
    for name in GREEKS:
        available = [
            item["greeks"][name] for item in leg_exposures if item["greeks"][name] is not None
        ]
        complete = full_population_authority and len(available) == len(option_legs)
        status = "COMPLETE" if complete else ("PARTIAL" if available else "UNAVAILABLE")
        greek_aggregates[name] = {
            "unit": RAW_GREEK_UNIT,
            "status": status,
            "net": sum(available, Decimal(0)) if complete else None,
            "gross_abs": sum((abs(item) for item in available), Decimal(0)) if complete else None,
            "coverage": {"available_legs": len(available), "total_legs": len(option_legs)},
        }
        if option_legs and len(available) != len(option_legs):
            reasons.append(
                _reason(
                    "GREEK_COVERAGE_INCOMPLETE",
                    name,
                    f"{len(available)} of {len(option_legs)} option legs have {name}",
                )
            )

    expiry_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for leg in leg_exposures:
        expiry_groups[leg["expiry"] or "UNKNOWN"].append(leg)
    expiry_buckets: list[dict[str, Any]] = []
    for expiry, legs in sorted(
        expiry_groups.items(), key=lambda item: (item[0] == "UNKNOWN", item[0])
    ):
        dte_by_market = []
        if expiry != "UNKNOWN":
            from datetime import date

            for contract_market in sorted({item["market"] for item in legs}):
                local_date = snapshot.as_of.astimezone(_MARKET_TIMEZONES[contract_market]).date()
                dte = (date.fromisoformat(expiry) - local_date).days
                dte_by_market.append({"market": contract_market, "dte": dte, "expired": dte < 0})
        expiry_buckets.append(
            {
                "expiry": expiry,
                "dte_by_market": dte_by_market,
                "leg_count": len(legs),
                "identities": sorted({item["identity"] for item in legs}),
                "net_market_value_base": sum(
                    (item["market_value_base"] for item in legs), Decimal(0)
                ),
                "gross_market_value_base": sum(
                    (abs(item["market_value_base"]) for item in legs), Decimal(0)
                ),
            }
        )

    type_buckets = []
    for option_type in ("CALL", "PUT", "UNKNOWN"):
        legs = [
            item
            for item in leg_exposures
            if (item["option_type"] if item["option_type"] is not None else "UNKNOWN")
            == option_type
        ]
        if legs:
            type_buckets.append(
                {
                    "option_type": option_type,
                    "leg_count": len(legs),
                    "net_market_value_base": sum(
                        (item["market_value_base"] for item in legs), Decimal(0)
                    ),
                    "gross_market_value_base": sum(
                        (abs(item["market_value_base"]) for item in legs), Decimal(0)
                    ),
                }
            )

    any_greek = any(
        value is not None for item in leg_exposures for value in item["greeks"].values()
    )
    if future:
        status = "FUTURE_SNAPSHOT"
    elif not option_legs:
        status = "PARTIAL" if unknown_count else "NOT_APPLICABLE"
    elif not any_greek and unsupported:
        status = "PARTIAL"
    elif not any_greek:
        status = "UNAVAILABLE"
    elif all(item["status"] == "COMPLETE" for item in greek_aggregates.values()):
        status = "COMPLETE"
    else:
        status = "PARTIAL"
    data_quality_status = (
        "COMPLETE"
        if status in {"COMPLETE", "NOT_APPLICABLE"}
        else "UNAVAILABLE"
        if status in {"UNAVAILABLE", "FUTURE_SNAPSHOT"}
        else "PARTIAL"
    )
    net_mv = sum((item["market_value_base"] for item in option_legs), Decimal(0))
    gross_mv = sum((abs(item["market_value_base"]) for item in option_legs), Decimal(0))
    snapshot_body = snapshot.to_dict()
    quote_capability = (
        "NOT_ACCESSED"
        if not option_legs
        or future
        or not any(item["selection_status"] == "SELECTED" for item in contracts)
        else "UNAVAILABLE"
        if quote_provider is None
        else "AVAILABLE"
    )
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "option_risk_id": "",
        "analysis_now": analysis_now,
        "as_of": snapshot.as_of,
        "source_snapshot_id": snapshot.snapshot_id,
        "source_snapshot_digest": hashlib.sha256(_canonical_bytes(snapshot_body)).hexdigest(),
        "base_currency": snapshot.base_currency,
        "status": status,
        "data_quality": {"status": data_quality_status, "freshness": "UNVERIFIED"},
        "coverage": {
            "source_positions": len(snapshot.positions),
            "nonzero_positions": sum(1 for item in snapshot.positions if item.quantity != 0),
            "unknown_security_type_positions": unknown_count,
            "option_legs": len(option_legs),
            "unique_contracts": len(contracts),
            "supported_contracts": sum(
                1 for item in contracts if item["market"] in _MARKET_TIMEZONES
            ),
            "selected_contracts": sum(
                1 for item in contracts if item["selection_status"] == "SELECTED"
            ),
            "quoted_contracts": len(quotes),
            "unsupported_contracts": len(unsupported),
            "budget_omitted_contracts": len(budget_omitted),
        },
        "provenance": {
            "source_snapshot": snapshot_body,
            "quote_source": "OFFICIAL_FUTUAPI_GET_OPTION_QUOTE",
            "quote_script_sha256": OPTION_QUOTE_SHA256,
            "quote_capability": quote_capability,
            "quote_freshness": "UNVERIFIED",
            "greek_unit": RAW_GREEK_UNIT,
        },
        "source_population": {
            "net_market_value_base": net_mv,
            "gross_market_value_base": gross_mv,
            "gross_to_nav": None if snapshot.nav_base == 0 else gross_mv / snapshot.nav_base,
            "contracts": contracts,
        },
        "quote_evidence": quote_evidence,
        "leg_exposures": leg_exposures,
        "greek_aggregates": greek_aggregates,
        "option_type_buckets": type_buckets,
        "expiry_buckets": expiry_buckets,
        "quality_reasons": sorted(reasons, key=lambda item: (item["code"], item["scope"])),
        "formulas": {
            "leg_greek": "signed_snapshot_quantity * unit_greek",
            "net_greek": "sum(leg_greek) only with complete population coverage",
            "gross_abs_greek": "sum(abs(leg_greek)) only with complete population coverage",
            "gross_to_nav": "gross_market_value_base / snapshot_nav_base",
            "dte": "expiry_calendar_date - snapshot_as_of_market_local_date",
        },
        "limitations": [
            "Greeks are raw contract units with no contract multiplier.",
            "No multiplier, underlying identity, or underlying-share exposure is inferred.",
            "Quote freshness is UNVERIFIED; only request start and finish windows are recorded.",
            "No bid/ask, portfolio pricing, max profit/loss, breakeven, probability, margin, scenario P/L, beta, money delta, or trading advice is provided.",
            "DTE is calendar-day difference, not trading days.",
        ],
    }
    identity_body = {key: value for key, value in payload.items() if key != "option_risk_id"}
    payload["option_risk_id"] = hashlib.sha256(_canonical_bytes(identity_body)).hexdigest()[:20]
    return OptionRiskReport(payload)
