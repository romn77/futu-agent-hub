"""Conservative inventory of raw per-contract option Greeks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import queue
import threading
import time
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

from .models import PortfolioSnapshot, _deep_freeze, _json_value
from .futu_adapter import FutuReadError
from .option_quote import (
    GREEKS,
    RAW_GREEK_UNIT,
    OptionQuote,
    OptionQuoteError,
    OptionQuoteProviderError,
    parse_option_quote_response,
)
from .skill_trust import OPTION_QUOTE_SHA256


MAX_SOURCE_POSITIONS = 10_000
MAX_QUOTE_CONTRACTS = 30
PER_CALL_TIMEOUT_SECONDS = 5.0
TOTAL_WALL_SECONDS = 30.0
CIRCUIT_BREAKER_FAILURES = 3
MAX_SINGLE_RESPONSE_BYTES = 256 * 1024
MAX_TOTAL_RESPONSE_BYTES = 4 * 1024 * 1024
_MARKET_TIMEZONES = {"US": ZoneInfo("America/New_York"), "HK": ZoneInfo("Asia/Hong_Kong")}
_QUOTE_EVIDENCE_FIELDS = frozenset(
    {"code", *GREEKS, "option_type", "expire_time", "strike_price", "implied_volatility"}
)


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
    def normalize(item: object) -> object:
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, float) and not math.isfinite(item):
            return str(item)
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(child) for child in item]
        return item

    return json.dumps(
        normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _normalized_source_response(value: object) -> object:
    if not isinstance(value, dict) or set(value) != {"data"}:
        return {"contract_invalid": True}
    rows = value["data"]
    if not isinstance(rows, list):
        return {"data": None}
    return {
        "data": [
            {key: item[key] for key in sorted(item) if key in _QUOTE_EVIDENCE_FIELDS}
            if isinstance(item, dict)
            else None
            for item in rows
        ]
    }


def _bounded_provider_call(
    provider: OptionQuoteProvider, identity: str, *, timeout: float
) -> object:
    results: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            value = provider.quote(identity, timeout=timeout)
        except BaseException as exc:
            results.put((False, exc))
        else:
            results.put((True, value))

    worker = threading.Thread(
        target=invoke,
        daemon=True,
        name="portfolio-option-quote",
    )
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError("bounded option quote request timed out")
    ok, value = results.get_nowait()
    if not ok:
        raise value  # type: ignore[misc]
    return value


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
        iv = data["implied_volatility_coverage"]
        lines.append(
            "- Implied volatility (raw vendor value, nonaggregated): "
            f"{iv['status']}; coverage={iv['available_legs']}/{iv['total_legs']}"
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


def _trusted_analysis_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
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
    operation_started = time.monotonic()
    trusted_deadline = analysis_now + timedelta(seconds=TOTAL_WALL_SECONDS)

    def unavailable_evidence(
        base: dict[str, Any], instant: datetime, reason_code: str
    ) -> dict[str, Any]:
        return {
            **base,
            "request_started_at": _utc_text(instant),
            "request_finished_at": _utc_text(instant),
            "status": "OMITTED"
            if reason_code in {"QUOTE_CIRCUIT_OPEN", "QUOTE_TOTAL_BUDGET_EXCEEDED"}
            else "FAILURE",
            "reason_code": reason_code,
            "response_byte_count": 0,
            "source_response": None,
            "response": None,
        }

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
            evidence.append(
                unavailable_evidence(base, analysis_now, "QUOTE_CAPABILITY_UNAVAILABLE")
            )
            continue
        started = _safe_clock(clock)
        trusted_elapsed = (started - analysis_now).total_seconds()
        real_elapsed = time.monotonic() - operation_started
        if trusted_elapsed < 0:
            raise OptionRiskInputError("trusted option quote clock moved before analysis_now")
        if trusted_elapsed >= TOTAL_WALL_SECONDS or real_elapsed >= TOTAL_WALL_SECONDS:
            evidence.append(
                unavailable_evidence(
                    base, min(started, trusted_deadline), "QUOTE_TOTAL_BUDGET_EXCEEDED"
                )
            )
            continue
        if consecutive_failures >= CIRCUIT_BREAKER_FAILURES:
            evidence.append(unavailable_evidence(base, started, "QUOTE_CIRCUIT_OPEN"))
            continue

        remaining = min(
            PER_CALL_TIMEOUT_SECONDS,
            TOTAL_WALL_SECONDS - trusted_elapsed,
            TOTAL_WALL_SECONDS - real_elapsed,
        )
        status = "SUCCESS"
        reason_code: str | None = None
        response: dict[str, object] | None = None
        source_response: object | None = None
        response_byte_count = 0
        quote: OptionQuote | None = None
        call_started = time.monotonic()
        try:
            raw = _bounded_provider_call(provider, identity, timeout=remaining)
            raw_encoded = _bounded_response_bytes(raw)
            if len(raw_encoded) > MAX_SINGLE_RESPONSE_BYTES:
                response_byte_count = MAX_SINGLE_RESPONSE_BYTES + 1
                raise OverflowError("single")
            source_response = json.loads(_bounded_response_bytes(_normalized_source_response(raw)))
            normalized_bytes = _bounded_response_bytes(source_response)
            response_byte_count = len(normalized_bytes)
            if total_bytes + response_byte_count > MAX_TOTAL_RESPONSE_BYTES:
                raise OverflowError("total")
            total_bytes += response_byte_count
            quote = parse_option_quote_response(identity, raw)
            response = quote.to_dict()
        except TimeoutError:
            status, reason_code = "FAILURE", "QUOTE_SOURCE_TIMEOUT"
        except FutuReadError:
            status, reason_code = "FAILURE", "QUOTE_SOURCE_READ_FAILED"
        except OptionQuoteProviderError as exc:
            status, reason_code = "FAILURE", exc.reason_code
            if reason_code == "QUOTE_RESPONSE_TOO_LARGE":
                response_byte_count = MAX_SINGLE_RESPONSE_BYTES + 1
        except OverflowError as exc:
            status = "FAILURE"
            reason_code = (
                "QUOTE_RESPONSE_TOO_LARGE"
                if str(exc) == "single"
                else "QUOTE_TOTAL_BUDGET_EXCEEDED"
            )
        except (OptionQuoteError, TypeError, ValueError):
            status, reason_code = "FAILURE", "QUOTE_RESPONSE_CONTRACT_INVALID"
        except Exception:
            status, reason_code = "FAILURE", "QUOTE_SOURCE_UNAVAILABLE"
        observed_finished = _safe_clock(clock)
        trusted_duration = (observed_finished - started).total_seconds()
        trusted_total = (observed_finished - analysis_now).total_seconds()
        real_total = time.monotonic() - operation_started
        real_duration = time.monotonic() - call_started
        if observed_finished < started:
            raise OptionRiskInputError("trusted option quote clock moved backwards")
        call_deadline = min(started + timedelta(seconds=PER_CALL_TIMEOUT_SECONDS), trusted_deadline)
        if trusted_total > TOTAL_WALL_SECONDS or real_total > TOTAL_WALL_SECONDS:
            reason_code = (
                "QUOTE_SOURCE_TIMEOUT"
                if call_deadline < trusted_deadline
                else "QUOTE_TOTAL_BUDGET_EXCEEDED"
            )
            status = "FAILURE"
            quote = response = source_response = None
            response_byte_count = 0
        elif trusted_duration > PER_CALL_TIMEOUT_SECONDS or real_duration > remaining + 0.01:
            status, reason_code = "FAILURE", "QUOTE_SOURCE_TIMEOUT"
            quote = response = source_response = None
            response_byte_count = 0
        finished = (
            min(observed_finished, call_deadline)
            if reason_code
            in {
                "QUOTE_SOURCE_TIMEOUT",
                "QUOTE_TOTAL_BUDGET_EXCEEDED",
            }
            else observed_finished
        )
        if status == "SUCCESS" and quote is not None:
            quotes[identity] = quote
            consecutive_failures = 0
        else:
            response = None
            consecutive_failures += 1
        evidence.append(
            {
                **base,
                "request_started_at": _utc_text(started),
                "request_finished_at": _utc_text(finished),
                "status": status,
                "reason_code": reason_code,
                "response_byte_count": response_byte_count,
                "source_response": source_response,
                "response": response,
            }
        )
    return evidence, quotes


def analyze_option_risk(
    snapshot: PortfolioSnapshot,
    *,
    quote_provider: OptionQuoteProvider | None = None,
    analysis_now: datetime | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OptionRiskReport:
    if len(snapshot.positions) > MAX_SOURCE_POSITIONS:
        raise OptionRiskInputError("option source position hard cap exceeded")
    analysis_now = (
        _safe_clock(clock) if analysis_now is None else _trusted_analysis_now(analysis_now)
    )
    if snapshot.as_of > analysis_now:
        raise OptionRiskInputError("FUTURE_SNAPSHOT")
    option_legs, contracts = _selection(snapshot)
    unknown_count = sum(
        1
        for position in snapshot.positions
        if position.quantity != 0 and position.security_type == "UNKNOWN"
    )
    quote_evidence: list[dict[str, Any]] = []
    quotes: dict[str, OptionQuote] = {}
    if option_legs:
        quote_evidence, quotes = _collect_quotes(contracts, quote_provider, clock, analysis_now)

    reasons: list[dict[str, str]] = []
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
    failure_details = {
        "QUOTE_CAPABILITY_UNAVAILABLE": ("live_source", "option quote capability unavailable"),
        "QUOTE_SOURCE_TIMEOUT": ("live_source", "option quote source timed out"),
        "QUOTE_SOURCE_READ_FAILED": ("live_source", "audited option quote read failed"),
        "QUOTE_SOURCE_UNAVAILABLE": ("live_source", "option quote source unavailable"),
        "QUOTE_RESPONSE_CONTRACT_INVALID": (
            "quote_response",
            "option quote response contract invalid",
        ),
        "QUOTE_RESPONSE_TOO_LARGE": ("quote_budget", "single quote response exceeded 256 KiB"),
        "QUOTE_TOTAL_BUDGET_EXCEEDED": (
            "quote_budget",
            "total quote wall or response budget was exhausted",
        ),
        "QUOTE_CIRCUIT_OPEN": ("live_source", "source failure circuit breaker opened"),
    }
    for code in sorted({item["reason_code"] for item in quote_evidence if item["reason_code"]}):
        scope, detail = failure_details[code]
        reasons.append(_reason(code, scope, detail))
    if snapshot.data_quality.status != "COMPLETE":
        reasons.append(
            _reason(
                "SOURCE_SNAPSHOT_INCOMPLETE",
                "snapshot",
                f"source snapshot quality is {snapshot.data_quality.status}",
            )
        )

    full_population_authority = (
        unknown_count == 0
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

    iv_available = sum(1 for item in leg_exposures if item["implied_volatility"] is not None)
    if not option_legs:
        iv_status = "NOT_APPLICABLE"
    elif full_population_authority and iv_available == len(option_legs):
        iv_status = "COMPLETE"
    elif iv_available:
        iv_status = "PARTIAL"
    else:
        iv_status = "UNAVAILABLE"
    implied_volatility_coverage = {
        "status": iv_status,
        "available_legs": iv_available,
        "total_legs": len(option_legs),
    }
    if option_legs and iv_available != len(option_legs):
        reasons.append(
            _reason(
                "IMPLIED_VOLATILITY_COVERAGE_INCOMPLETE",
                "implied_volatility",
                f"{iv_available} of {len(option_legs)} option legs have implied volatility",
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
    if not option_legs:
        status = "PARTIAL" if unknown_count else "NOT_APPLICABLE"
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
        if status == "UNAVAILABLE"
        else "PARTIAL"
    )
    net_mv = sum((item["market_value_base"] for item in option_legs), Decimal(0))
    gross_mv = sum((abs(item["market_value_base"]) for item in option_legs), Decimal(0))
    snapshot_body = snapshot.to_dict()
    quote_capability = (
        "NOT_ACCESSED"
        if not option_legs or not any(item["selection_status"] == "SELECTED" for item in contracts)
        else "UNAVAILABLE"
        if quote_provider is None
        or any(item["reason_code"] == "QUOTE_CAPABILITY_UNAVAILABLE" for item in quote_evidence)
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
        "implied_volatility_coverage": implied_volatility_coverage,
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
            "Implied volatility is a raw vendor value and is not aggregated.",
            "Implied volatility has no high/low interpretation and inherits UNVERIFIED quote freshness.",
            "No bid/ask, portfolio pricing, max profit/loss, breakeven, probability, margin, scenario P/L, beta, money delta, or trading advice is provided.",
            "DTE is calendar-day difference, not trading days.",
        ],
    }
    identity_body = {key: value for key, value in payload.items() if key != "option_risk_id"}
    payload["option_risk_id"] = hashlib.sha256(_canonical_bytes(identity_body)).hexdigest()[:20]
    return OptionRiskReport(payload)
