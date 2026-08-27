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

from .models import NormalizedPosition, PortfolioSnapshot, _json_value
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
    positions: Mapping[str, Mapping[str, Decimal]]
    exposures: Mapping[str, Mapping[str, Decimal]]
    concentration: Mapping[str, Decimal]

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
        object.__setattr__(self, "concentration", MappingProxyType(dict(self.concentration)))


@dataclass(frozen=True)
class RebalanceResult:
    schema_version: str
    simulation_id: str
    source_snapshot_id: str
    as_of: datetime
    base_currency: str
    status: str
    execution_disabled: bool
    feasible: bool
    constraints: Mapping[str, Any]
    before: PortfolioState
    after: PortfolioState
    changes: tuple[Mapping[str, Any], ...]
    risk_changes: Mapping[str, Any]
    warnings: tuple[str, ...]
    data_quality_status: str
    coverage: Mapping[str, Any]
    data_quality_notes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "constraints", MappingProxyType(dict(self.constraints)))
        object.__setattr__(self, "changes", tuple(MappingProxyType(dict(x)) for x in self.changes))
        object.__setattr__(self, "risk_changes", MappingProxyType(dict(self.risk_changes)))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "coverage", MappingProxyType(dict(self.coverage)))
        object.__setattr__(self, "data_quality_notes", tuple(self.data_quality_notes))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)

    def to_markdown(self) -> str:
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
            f"- Before cash: {self.before.cash_base}",
            f"- After cash: {self.after.cash_base}",
            f"- Risk comparison status: {safe(self.risk_changes['status'])}",
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
                f"Before cash: {self.before.cash_base}",
                f"After cash: {self.after.cash_base}",
                f"Risk comparison: {terminal_text(self.risk_changes['status'])}",
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
    dimensions: dict[str, dict[str, Decimal]] = {
        "sector": {},
        "region": {},
        "theme_full": {},
        "theme_split": {},
    }
    for identity, weight in weights.items():
        position = metadata[identity]
        for dimension, label in (("sector", position.sector), ("region", position.region)):
            dimensions[dimension][label] = dimensions[dimension].get(label, Decimal(0)) + weight
        themes = position.themes or ("UNKNOWN",)
        for theme in themes:
            dimensions["theme_full"][theme] = (
                dimensions["theme_full"].get(theme, Decimal(0)) + weight
            )
            dimensions["theme_split"][theme] = dimensions["theme_split"].get(
                theme, Decimal(0)
            ) + weight / len(themes)
    return PortfolioState(
        snapshot_id,
        nav,
        cash,
        cash / nav,
        {
            identity: {
                "quantity": quantities[identity],
                "market_value_base": value,
                "weight": weights.get(identity, Decimal(0)),
            }
            for identity, value in sorted(positions.items())
        },
        dimensions,
        _concentration(positions, nav),
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
    for position in snapshot.positions:
        identity = _identity(position)
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
    warnings: list[str] = []
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
        applied.append(
            {
                "symbol": change.symbol,
                "operation": change.operation,
                "value": change.value,
                "amount_delta_base": delta,
                "quantity_delta": quantity_delta,
            }
        )
    if request.new_cash_only and positive_delta > request.new_cash:
        warnings.append("purchases exceed explicitly supplied new cash")
        feasible = False
    if cash < 0 and not request.allow_margin:
        warnings.append("simulated cash is negative while margin is disabled")
        feasible = False
    if any(quantity < 0 for quantity in quantities.values()) and not request.allow_short:
        warnings.append("simulated quantity is negative while shorting is disabled")
        feasible = False
    after = _state(snapshot.snapshot_id, values, quantities, metadata, cash, final_nav)
    signed_hypothetical = any(quantity < 0 for quantity in quantities.values())
    if signed_hypothetical:
        warnings.append(
            "signed hypothetical portfolio: Phase 2 long-only risk authority is unavailable; structural metrics only"
        )
    risk_changes: dict[str, Any] = {
        name: after.concentration[name] - before.concentration[name]
        for name in before.concentration
    }
    risk_changes["status"] = "STRUCTURAL_ONLY_SIGNED" if signed_hypothetical else "STRUCTURAL_ONLY"
    risk_changes["concentration"] = MappingProxyType(dict(after.concentration))
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
    body = {
        "snapshot_id": request.snapshot_id,
        "base_currency": request.base_currency,
        "new_cash": str(request.new_cash),
        "allow_margin": request.allow_margin,
        "allow_short": request.allow_short,
        "sell_only": request.sell_only,
        "new_cash_only": request.new_cash_only,
        "changes": [
            {"symbol": x.symbol, "operation": x.operation, "value": str(x.value)}
            for x in request.changes
        ],
    }
    simulation_id = (
        "sim_"
        + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
    )
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
    quality_notes = tuple(dict.fromkeys((*source_quality_notes, *warnings)))
    return RebalanceResult(
        "1.1",
        simulation_id,
        snapshot.snapshot_id,
        snapshot.as_of,
        snapshot.base_currency,
        "HYPOTHETICAL",
        True,
        feasible,
        constraints,
        before,
        after,
        tuple(applied),
        risk_changes,
        tuple(warnings),
        "PARTIAL" if quality_notes else "COMPLETE",
        {
            "source_positions": len(snapshot.positions),
            "hypothetical_positions": len(after.positions),
            "changes_requested": len(request.changes),
            "changes_applied": len(applied),
            "price_evidence": len(metadata),
            "fx_evidence": len(metadata),
            "source_data_quality_status": snapshot.data_quality.status,
        },
        quality_notes,
    )
