"""Pure hypothetical rebalance simulation; never creates executable instructions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping

from .models import NormalizedPosition, PortfolioSnapshot, _deep_freeze, _json_value
from .rendering import markdown_text, terminal_text
from .schema import validate_rebalance_request_document
from .snapshot_io import InvalidSnapshot, _read_confined_bytes, _unique_object


class RebalanceError(ValueError):
    pass


_IDENTITY = re.compile(r"[A-Z]{2,4}\.[A-Z0-9][A-Z0-9.-]{0,31}")
_OPERATIONS = frozenset({"TARGET_WEIGHT", "AMOUNT_DELTA", "SHARE_DELTA"})


@dataclass(frozen=True)
class RebalanceChange:
    symbol: str
    operation: str
    value: Decimal

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        operation = self.operation.strip().upper()
        if _IDENTITY.fullmatch(symbol) is None:
            raise RebalanceError("change symbol must be market-qualified")
        if operation not in _OPERATIONS:
            raise RebalanceError("unsupported rebalance operation")
        if not isinstance(self.value, Decimal) or not self.value.is_finite():
            raise RebalanceError("change value must be a finite Decimal")
        if operation == "TARGET_WEIGHT" and not Decimal(0) <= self.value <= Decimal(1):
            raise RebalanceError("target weight must be between zero and one")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "operation", operation)


@dataclass(frozen=True)
class RebalanceRequest:
    snapshot_id: str
    base_currency: str
    changes: tuple[RebalanceChange, ...] = ()
    new_cash: Decimal = Decimal(0)
    allow_margin: bool = False
    allow_short: bool = False
    sell_only: bool = False
    new_cash_only: bool = False
    schema_version: str = "1.1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "changes", tuple(self.changes))
        if self.schema_version != "1.1" or not self.snapshot_id:
            raise RebalanceError("request identity/version is invalid")
        if re.fullmatch(r"[A-Z]{3}", self.base_currency) is None:
            raise RebalanceError("request base currency must be an uppercase ISO-style code")
        for name in ("allow_margin", "allow_short", "sell_only", "new_cash_only"):
            if not isinstance(getattr(self, name), bool):
                raise RebalanceError(f"{name} must be boolean")
        if (
            not isinstance(self.new_cash, Decimal)
            or not self.new_cash.is_finite()
            or self.new_cash < 0
        ):
            raise RebalanceError("new cash must be a non-negative finite Decimal")
        identities = [change.symbol for change in self.changes]
        if len(identities) != len(set(identities)):
            raise RebalanceError("duplicate or conflicting symbol changes are forbidden")


@dataclass(frozen=True)
class PortfolioState:
    snapshot_id: str
    nav_base: Decimal
    cash_base: Decimal
    cash_weight: Decimal
    long_market_value_base: Decimal
    short_market_value_base: Decimal
    gross_exposure_base: Decimal
    net_exposure_base: Decimal
    long_weight: Decimal
    short_weight: Decimal
    gross_weight: Decimal
    net_weight: Decimal
    positions: Mapping[str, Mapping[str, Decimal]]
    exposures: Mapping[str, Mapping[str, Decimal]]
    exposure_views: Mapping[str, Mapping[str, Mapping[str, Decimal]]]
    concentration_status: str
    concentration: Mapping[str, Decimal | None]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "positions",
            MappingProxyType({k: MappingProxyType(dict(v)) for k, v in self.positions.items()}),
        )
        object.__setattr__(
            self,
            "exposures",
            MappingProxyType({k: MappingProxyType(dict(v)) for k, v in self.exposures.items()}),
        )
        object.__setattr__(self, "exposure_views", _deep_freeze(dict(self.exposure_views)))
        object.__setattr__(self, "concentration", MappingProxyType(dict(self.concentration)))


@dataclass(frozen=True)
class RebalanceResult:
    schema_version: str
    simulation_id: str
    source_snapshot_id: str
    source_snapshot: Mapping[str, Any]
    as_of: datetime
    base_currency: str
    status: str
    execution_disabled: bool
    feasible: bool
    constraints: Mapping[str, Any]
    request: Mapping[str, Any]
    evidence: Mapping[str, Mapping[str, Any]]
    before: PortfolioState
    after: PortfolioState
    changes: tuple[Mapping[str, Any], ...]
    risk_changes: Mapping[str, Any]
    warnings: tuple[str, ...]
    data_quality_status: str
    coverage: Mapping[str, Any]
    data_quality_notes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_snapshot", _deep_freeze(dict(self.source_snapshot)))
        object.__setattr__(self, "constraints", _deep_freeze(dict(self.constraints)))
        object.__setattr__(self, "request", _deep_freeze(dict(self.request)))
        object.__setattr__(self, "evidence", _deep_freeze(dict(self.evidence)))
        object.__setattr__(self, "changes", _deep_freeze(tuple(self.changes)))
        object.__setattr__(self, "risk_changes", _deep_freeze(dict(self.risk_changes)))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "coverage", _deep_freeze(dict(self.coverage)))
        object.__setattr__(self, "data_quality_notes", tuple(self.data_quality_notes))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)

    def to_markdown(
        self, *, json_artifact: str | None = None, json_digest: str | None = None
    ) -> str:
        safe = markdown_text
        lines = [
            "# Hypothetical Rebalance Simulation\n",
            "- Status: HYPOTHETICAL",
            "- Execution disabled: true",
            f"- Simulation ID: {safe(self.simulation_id)}",
            f"- Source snapshot: {safe(self.source_snapshot_id)}",
            f"- As of: {self.as_of.isoformat()}",
            f"- Base currency: {safe(self.base_currency)}",
            f"- Feasible: {str(self.feasible).lower()}",
            f"- Authoritative request: {safe(json.dumps(_json_value(self.request), sort_keys=True))}",
            f"- Price/FX evidence: {safe(json.dumps(_json_value(self.evidence), sort_keys=True))}",
            f"- Before cash: {self.before.cash_base}",
            f"- After cash: {self.after.cash_base}",
            f"- Before long / short / gross / net market value: {self.before.long_market_value_base} / {self.before.short_market_value_base} / {self.before.gross_exposure_base} / {self.before.net_exposure_base}",
            f"- After long / short / gross / net market value: {self.after.long_market_value_base} / {self.after.short_market_value_base} / {self.after.gross_exposure_base} / {self.after.net_exposure_base}",
            f"- Before exposure views: {safe(json.dumps(_json_value(self.before.exposure_views), sort_keys=True))}",
            f"- After exposure views: {safe(json.dumps(_json_value(self.after.exposure_views), sort_keys=True))}",
            f"- Risk comparison status: {safe(self.risk_changes['status'])}",
            f"- Before weights: {safe(json.dumps(_json_value({k: v['weight'] for k, v in self.before.positions.items()}), sort_keys=True))}",
            f"- After weights: {safe(json.dumps(_json_value({k: v['weight'] for k, v in self.after.positions.items()}), sort_keys=True))}",
            f"- Before concentration: {safe(json.dumps(_json_value(self.before.concentration), sort_keys=True))}",
            f"- After concentration: {safe(json.dumps(_json_value(self.after.concentration), sort_keys=True))}",
            f"- Exposure/risk deltas: {safe(json.dumps(_json_value(self.risk_changes), sort_keys=True))}",
            f"- Coverage: {self.coverage['changes_applied']} of {self.coverage['changes_requested']} changes; {self.coverage['price_evidence']} price and {self.coverage['fx_evidence']} FX evidence records",
            f"- Data quality: {safe(self.data_quality_status)}",
            "\n## Hypothetical changes\n",
        ]
        lines.extend(
            f"- {safe(change['symbol'])}: {safe(change['operation'])}, amount delta {change['amount_delta_base']}"
            for change in self.changes
        )
        lines.extend(["\n## Warnings\n", *([f"- {safe(x)}" for x in self.warnings] or ["- None"])])
        lines.extend(
            [
                "\n## Data quality notes\n",
                *([f"- {safe(x)}" for x in self.data_quality_notes] or ["- None"]),
            ]
        )
        lines.append(
            "\nThis artifact is analysis only and cannot be converted into an executable order.\n"
        )
        if json_artifact:
            lines.append(f"Authoritative JSON: `{safe(json_artifact)}`\n")
        if json_digest:
            lines.append(f"Authoritative JSON SHA-256: `{safe(json_digest)}`\n")
        return "\n".join(lines)

    def to_human(self) -> str:
        return "\n".join(
            [
                "Hypothetical rebalance simulation",
                f"Simulation: {terminal_text(self.simulation_id)}",
                f"Source snapshot: {terminal_text(self.source_snapshot_id)}",
                f"As of: {self.as_of.isoformat()}",
                f"Base currency: {terminal_text(self.base_currency)}",
                f"Feasible: {self.feasible}",
                "Execution disabled: True",
                f"Authoritative request: {terminal_text(json.dumps(_json_value(self.request), sort_keys=True))}",
                f"Price/FX evidence: {terminal_text(json.dumps(_json_value(self.evidence), sort_keys=True))}",
                f"Before cash: {self.before.cash_base}",
                f"After cash: {self.after.cash_base}",
                f"Before long/short/gross/net: {self.before.long_market_value_base}/{self.before.short_market_value_base}/{self.before.gross_exposure_base}/{self.before.net_exposure_base}",
                f"After long/short/gross/net: {self.after.long_market_value_base}/{self.after.short_market_value_base}/{self.after.gross_exposure_base}/{self.after.net_exposure_base}",
                f"Before exposure views: {terminal_text(json.dumps(_json_value(self.before.exposure_views), sort_keys=True))}",
                f"After exposure views: {terminal_text(json.dumps(_json_value(self.after.exposure_views), sort_keys=True))}",
                f"Risk comparison: {terminal_text(self.risk_changes['status'])}",
                f"Before weights: {terminal_text(json.dumps(_json_value({k: v['weight'] for k, v in self.before.positions.items()}), sort_keys=True))}",
                f"After weights: {terminal_text(json.dumps(_json_value({k: v['weight'] for k, v in self.after.positions.items()}), sort_keys=True))}",
                f"Before concentration: {terminal_text(json.dumps(_json_value(self.before.concentration), sort_keys=True))}",
                f"After concentration: {terminal_text(json.dumps(_json_value(self.after.concentration), sort_keys=True))}",
                f"Exposure/risk deltas: {terminal_text(json.dumps(_json_value(self.risk_changes), sort_keys=True))}",
                f"Coverage: {self.coverage['changes_applied']}/{self.coverage['changes_requested']} changes, {self.coverage['price_evidence']} price, {self.coverage['fx_evidence']} FX",
                f"Data quality: {terminal_text(self.data_quality_status)}",
                *[f"Warning: {terminal_text(item)}" for item in self.warnings],
                *[f"Quality note: {terminal_text(item)}" for item in self.data_quality_notes],
            ]
        )


def request_from_document(payload: dict[str, Any]) -> RebalanceRequest:
    try:
        validate_rebalance_request_document(payload)
        return RebalanceRequest(
            snapshot_id=payload["snapshot_id"],
            base_currency=payload["base_currency"],
            changes=tuple(
                RebalanceChange(item["symbol"], item["operation"], Decimal(item["value"]))
                for item in payload["changes"]
            ),
            new_cash=Decimal(payload["new_cash_base"]),
            allow_margin=payload["allow_margin"],
            allow_short=payload["allow_short"],
            sell_only=payload["sell_only"],
            new_cash_only=payload["new_cash_only"],
            schema_version=payload["schema_version"],
        )
    except RebalanceError:
        raise
    except Exception as exc:
        raise RebalanceError("rebalance request schema or value validation failed") from exc


def load_rebalance_request(
    data_home: Any, relative_path: str
) -> tuple[RebalanceRequest, dict[str, Any]]:
    try:
        raw = _read_confined_bytes(data_home, relative_path)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except InvalidSnapshot as exc:
        raise RebalanceError("rebalance request path is unavailable or unsafe") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RebalanceError("rebalance request is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RebalanceError("rebalance request root must be an object")
    return request_from_document(payload), payload


def _identity(position: NormalizedPosition) -> str:
    return f"{position.market.strip().upper()}.{position.symbol.strip().upper()}"


def _concentration(values: Mapping[str, Decimal], nav: Decimal) -> dict[str, Decimal]:
    weights = sorted((value / nav for value in values.values()), reverse=True)
    return {
        "top_1": sum(weights[:1], Decimal(0)),
        "top_3": sum(weights[:3], Decimal(0)),
        "top_5": sum(weights[:5], Decimal(0)),
        "hhi": sum((weight * weight for weight in weights), Decimal(0)),
    }


def _state(
    snapshot_id: str,
    positions: Mapping[str, Decimal],
    quantities: Mapping[str, Decimal],
    metadata: Mapping[str, NormalizedPosition],
    cash: Decimal,
    nav: Decimal,
) -> PortfolioState:
    weights = {identity: value / nav for identity, value in positions.items() if value != 0}
    exposure_views: dict[str, dict[str, dict[str, Decimal]]] = {
        view: {"sector": {}, "region": {}, "theme_full": {}, "theme_split": {}}
        for view in ("long", "short", "gross", "net")
    }
    for identity, weight in weights.items():
        position = metadata[identity]
        view_weights = {
            "long": max(weight, Decimal(0)),
            "short": max(-weight, Decimal(0)),
            "gross": abs(weight),
            "net": weight,
        }
        for dimension, label in (("sector", position.sector), ("region", position.region)):
            for view, view_weight in view_weights.items():
                if view_weight == 0:
                    continue
                exposure_views[view][dimension][label] = (
                    exposure_views[view][dimension].get(label, Decimal(0)) + view_weight
                )
        themes = position.themes or ("UNKNOWN",)
        for theme in themes:
            for view, view_weight in view_weights.items():
                if view_weight == 0:
                    continue
                exposure_views[view]["theme_full"][theme] = (
                    exposure_views[view]["theme_full"].get(theme, Decimal(0)) + view_weight
                )
                exposure_views[view]["theme_split"][theme] = exposure_views[view][
                    "theme_split"
                ].get(theme, Decimal(0)) + view_weight / len(themes)
    long_value = sum((max(value, Decimal(0)) for value in positions.values()), Decimal(0))
    short_value = sum((max(-value, Decimal(0)) for value in positions.values()), Decimal(0))
    signed = short_value > 0
    return PortfolioState(
        snapshot_id,
        nav,
        cash,
        cash / nav,
        long_value,
        short_value,
        long_value + short_value,
        long_value - short_value,
        long_value / nav,
        short_value / nav,
        (long_value + short_value) / nav,
        (long_value - short_value) / nav,
        {
            identity: {
                "quantity": quantities[identity],
                "market_value_base": value,
                "weight": weights.get(identity, Decimal(0)),
            }
            for identity, value in sorted(positions.items())
        },
        exposure_views["net"],
        exposure_views,
        "UNAVAILABLE_SIGNED" if signed else "AVAILABLE",
        (
            {name: None for name in ("top_1", "top_3", "top_5", "hhi")}
            if signed
            else _concentration(positions, nav)
        ),
    )


def simulate_rebalance(snapshot: PortfolioSnapshot, request: RebalanceRequest) -> RebalanceResult:
    if request.snapshot_id != snapshot.snapshot_id:
        raise RebalanceError("request snapshot identity does not match source")
    if request.base_currency != snapshot.base_currency:
        raise RebalanceError("request base currency does not match snapshot currency")
    if snapshot.nav_base <= 0:
        raise RebalanceError("source NAV must be positive")
    metadata: dict[str, NormalizedPosition] = {}
    values: dict[str, Decimal] = {}
    quantities: dict[str, Decimal] = {}
    leg_signs: dict[str, set[int]] = {}
    source_legs: dict[str, list[dict[str, Any]]] = {}
    for position in snapshot.positions:
        identity = _identity(position)
        if any(leg["account_ref"] == position.account_ref for leg in source_legs.get(identity, [])):
            raise RebalanceError("duplicate account/identity source legs are unsupported")
        source_legs.setdefault(identity, []).append(
            {
                "account_ref": position.account_ref,
                "quantity": position.quantity,
                "market_value_base": position.market_value_base,
            }
        )
        if position.quantity != 0 or position.market_value_base != 0:
            sign = 1 if position.quantity > 0 and position.market_value_base > 0 else -1
            if (position.quantity > 0) != (position.market_value_base > 0):
                raise RebalanceError("source leg quantity/value signs conflict")
            leg_signs.setdefault(identity, set()).add(sign)
        if identity in metadata:
            reference = metadata[identity]
            comparable = (
                "currency",
                "fx_to_base",
                "last_price_local",
                "sector",
                "region",
                "themes",
                "asset_class",
                "security_type",
            )
            if any(getattr(reference, name) != getattr(position, name) for name in comparable):
                raise RebalanceError("cross-account position metadata or price evidence conflicts")
            values[identity] += position.market_value_base
            quantities[identity] += position.quantity
        else:
            metadata[identity] = position
            values[identity] = position.market_value_base
            quantities[identity] = position.quantity
    if any(len(signs) > 1 for signs in leg_signs.values()):
        raise RebalanceError(
            "UNSUPPORTED_OFFSETTING_LEGS: opposite cross-account legs cannot be authoritatively allocated"
        )
    if any(
        quantities[identity] * metadata[identity].last_price_local * metadata[identity].fx_to_base
        != value
        for identity, value in values.items()
    ):
        raise RebalanceError("source quantity/value does not reconcile with price/FX evidence")
    source_signed = any(
        quantity < 0 or values[identity] < 0 for identity, quantity in quantities.items()
    )
    if source_signed and not request.allow_short:
        raise RebalanceError("source portfolio contains short positions while shorting is disabled")
    before = _state(
        snapshot.snapshot_id,
        values,
        quantities,
        metadata,
        snapshot.cash_base,
        snapshot.nav_base,
    )
    final_nav = snapshot.nav_base + request.new_cash
    cash = snapshot.cash_base + request.new_cash
    applied: list[dict[str, Any]] = []
    warnings: list[str] = (
        ["source portfolio contains signed positions; long-only concentration is unavailable"]
        if source_signed
        else []
    )
    positive_delta = Decimal(0)
    feasible = True
    for change in request.changes:
        if change.symbol not in metadata:
            raise RebalanceError("change identity is absent from the immutable source snapshot")
        position = metadata[change.symbol]
        current = values[change.symbol]
        if position.last_price_local <= 0 or position.fx_to_base <= 0:
            raise RebalanceError("positive price and FX evidence are required")
        if change.operation == "TARGET_WEIGHT":
            delta = change.value * final_nav - current
            quantity_delta = delta / (position.last_price_local * position.fx_to_base)
        elif change.operation == "AMOUNT_DELTA":
            delta = change.value
            quantity_delta = delta / (position.last_price_local * position.fx_to_base)
        else:
            quantity_delta = change.value
            delta = quantity_delta * position.last_price_local * position.fx_to_base
        if request.sell_only and delta > 0:
            warnings.append(f"{change.symbol}: buy change violates sell-only constraint")
            feasible = False
        if request.new_cash_only and delta < 0:
            warnings.append(f"{change.symbol}: sell change violates new-cash-only constraint")
            feasible = False
        values[change.symbol] = current + delta
        quantities[change.symbol] += quantity_delta
        cash -= delta
        positive_delta += max(delta, Decimal(0))
        legs = sorted(source_legs[change.symbol], key=lambda item: item["account_ref"])
        allocation_basis = sum((abs(item["market_value_base"]) for item in legs), Decimal(0))
        leg_allocations: list[dict[str, Any]] = []
        allocated_amount = Decimal(0)
        allocated_quantity = Decimal(0)
        for index, leg in enumerate(legs):
            if index == len(legs) - 1:
                leg_amount = delta - allocated_amount
                leg_quantity = quantity_delta - allocated_quantity
            else:
                weight = (
                    abs(leg["market_value_base"]) / allocation_basis
                    if allocation_basis
                    else Decimal(1) / len(legs)
                )
                leg_amount = delta * weight
                leg_quantity = quantity_delta * weight
            allocated_amount += leg_amount
            allocated_quantity += leg_quantity
            leg_allocations.append(
                {
                    "account_ref": leg["account_ref"],
                    "amount_delta_base": leg_amount,
                    "quantity_delta": leg_quantity,
                }
            )
        applied.append(
            {
                "symbol": change.symbol,
                "operation": change.operation,
                "value": change.value,
                "amount_delta_base": delta,
                "quantity_delta": quantity_delta,
                "unit_price_local": position.last_price_local,
                "fx_to_base": position.fx_to_base,
                "leg_allocations": leg_allocations,
            }
        )
    if request.new_cash_only and positive_delta > request.new_cash:
        warnings.append("purchases exceed explicitly supplied new cash")
        feasible = False
    if cash < 0 and not request.allow_margin:
        warnings.append("simulated cash is negative while margin is disabled")
        feasible = False
    after_signed = any(quantity < 0 for quantity in quantities.values())
    if after_signed and not request.allow_short:
        raise RebalanceError("simulated quantity is negative while shorting is disabled")
    if after_signed:
        warnings.append(
            "hypothetical portfolio contains signed positions; long-only concentration is unavailable"
        )
    after = _state(snapshot.snapshot_id, values, quantities, metadata, cash, final_nav)
    signed_authority = source_signed or after_signed
    risk_changes: dict[str, Any] = {
        name: (None if signed_authority else after.concentration[name] - before.concentration[name])
        for name in before.concentration
    }
    risk_changes["status"] = "STRUCTURAL_ONLY_SIGNED" if signed_authority else "STRUCTURAL_ONLY"
    risk_changes["concentration"] = (
        None if signed_authority else MappingProxyType(dict(after.concentration))
    )
    for dimension in ("sector", "region", "theme_full", "theme_split"):
        risk_changes[dimension] = MappingProxyType(
            {
                key: after.exposures[dimension].get(key, Decimal(0))
                - before.exposures[dimension].get(key, Decimal(0))
                for key in sorted(
                    set(before.exposures[dimension]) | set(after.exposures[dimension])
                )
            }
        )
    risk_changes["exposure_views"] = MappingProxyType(
        {
            view: MappingProxyType(
                {
                    dimension: MappingProxyType(
                        {
                            key: after.exposure_views[view][dimension].get(key, Decimal(0))
                            - before.exposure_views[view][dimension].get(key, Decimal(0))
                            for key in sorted(
                                set(before.exposure_views[view][dimension])
                                | set(after.exposure_views[view][dimension])
                            )
                        }
                    )
                    for dimension in ("sector", "region", "theme_full", "theme_split")
                }
            )
            for view in ("long", "short", "gross", "net")
        }
    )
    request_body = {
        "schema_version": request.schema_version,
        "snapshot_id": request.snapshot_id,
        "base_currency": request.base_currency,
        "new_cash_base": request.new_cash,
        "allow_margin": request.allow_margin,
        "allow_short": request.allow_short,
        "sell_only": request.sell_only,
        "new_cash_only": request.new_cash_only,
        "changes": [
            {"symbol": x.symbol, "operation": x.operation, "value": x.value}
            for x in request.changes
        ],
    }
    evidence = {
        identity: {
            "unit_price_local": position.last_price_local,
            "currency": position.currency,
            "fx_to_base": position.fx_to_base,
            "fx_as_of": snapshot.as_of,
            "fx_method": "SOURCE_SNAPSHOT_CURRENT_VALUATION",
            "sector": position.sector,
            "region": position.region,
            "themes": list(position.themes),
            "theme_allocation": "FULL_AND_EQUAL_SPLIT",
            "source_legs": sorted(source_legs[identity], key=lambda item: item["account_ref"]),
            "operation_allocation": "PROPORTIONAL_SAME_DIRECTION_SOURCE_LEGS",
        }
        for identity, position in sorted(metadata.items())
    }
    constraints = {
        "allow_margin": request.allow_margin,
        "allow_short": request.allow_short,
        "sell_only": request.sell_only,
        "new_cash_only": request.new_cash_only,
        "new_cash_base": request.new_cash,
    }
    source_quality_notes = tuple(
        dict.fromkeys(
            (
                *snapshot.data_quality.warnings,
                *(f"missing source field: {item}" for item in snapshot.data_quality.missing_fields),
                *snapshot.data_quality.source_errors,
            )
        )
    )
    if snapshot.data_quality.status != "COMPLETE" and not source_quality_notes:
        source_quality_notes = (f"source snapshot data quality is {snapshot.data_quality.status}",)
    quality_notes = tuple(dict.fromkeys((*source_quality_notes, *warnings)))
    coverage = {
        "source_positions": len(snapshot.positions),
        "hypothetical_positions": len(after.positions),
        "changes_requested": len(request.changes),
        "changes_applied": len(applied),
        "price_evidence": len(metadata),
        "fx_evidence": len(metadata),
        "source_data_quality_status": snapshot.data_quality.status,
    }
    quality_status = "PARTIAL" if quality_notes else "COMPLETE"
    identity_body = _json_value(
        {
            "schema_version": "1.3",
            "source_snapshot_id": snapshot.snapshot_id,
            "source_snapshot": snapshot.to_dict(),
            "as_of": snapshot.as_of,
            "base_currency": snapshot.base_currency,
            "status": "HYPOTHETICAL",
            "execution_disabled": True,
            "feasible": feasible,
            "constraints": constraints,
            "request": request_body,
            "evidence": evidence,
            "before": before,
            "after": after,
            "changes": applied,
            "risk_changes": risk_changes,
            "warnings": warnings,
            "data_quality_status": quality_status,
            "coverage": coverage,
            "data_quality_notes": quality_notes,
        }
    )
    simulation_id = (
        "sim_"
        + hashlib.sha256(
            json.dumps(identity_body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
    )
    return RebalanceResult(
        "1.3",
        simulation_id,
        snapshot.snapshot_id,
        snapshot.to_dict(),
        snapshot.as_of,
        snapshot.base_currency,
        "HYPOTHETICAL",
        True,
        feasible,
        constraints,
        request_body,
        evidence,
        before,
        after,
        tuple(applied),
        risk_changes,
        tuple(warnings),
        quality_status,
        coverage,
        quality_notes,
    )
