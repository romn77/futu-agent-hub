"""Deterministic portfolio risk analysis over one immutable snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import re
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from .models import NormalizedPosition, PortfolioSnapshot, _json_value
from .normalization import canonical_unknown
from .secure_config import load_optional_json_object


@dataclass(frozen=True)
class PricePoint:
    date: date
    adjusted_close: Decimal


class PriceHistoryProvider(Protocol):
    def history(
        self, symbol: str, *, market: str, lookback: int, as_of: date
    ) -> Sequence[PricePoint] | None: ...


@dataclass(frozen=True)
class PortfolioPolicy:
    """Optional user-configured NAV-fraction thresholds, never investment defaults."""

    single_position: Decimal | None = None
    top_3: Decimal | None = None
    top_5: Decimal | None = None
    sector: Decimal | None = None
    region: Decimal | None = None
    theme_split: Decimal | None = None

    def __post_init__(self) -> None:
        for value in self.__dict__.values():
            if value is not None and (not value.is_finite() or value <= 0 or value > 1):
                raise ValueError("policy threshold must be a finite NAV fraction in (0, 1]")


class PolicyConfigError(ValueError):
    """Optional user policy is present but unsafe or invalid."""


class RiskConfigError(ValueError):
    """Risk computation settings violate the bounded analysis contract."""


class RiskInputError(ValueError):
    """Snapshot positions cannot be safely represented as economic holdings."""


@dataclass(frozen=True)
class EconomicPosition:
    identity: str
    market: str
    symbol: str
    security_type: str
    currency: str
    sector: str
    industry: str
    region: str
    themes: tuple[str, ...]
    asset_class: str
    position_role: str | None
    weight: Decimal
    market_value_base: Decimal
    unrealized_pl_base: Decimal | None
    realized_pl_base: Decimal | None
    source_holding_count: int


_SUPPORTED_POSITION_MARKETS = frozenset({"US", "HK", "CN", "SG", "MY", "JP", "CA", "AU"})
_POSITION_SYMBOL = re.compile(r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*")
_ECONOMIC_METADATA_FIELDS = (
    "security_type",
    "currency",
    "currency_provenance",
    "sector",
    "industry",
    "region",
    "themes",
    "asset_class",
    "position_role",
)


def _canonical_identity(position: NormalizedPosition) -> tuple[str, str, str]:
    market = str(position.market).strip().upper()
    symbol = str(position.symbol).strip().upper()
    if (
        market not in _SUPPORTED_POSITION_MARKETS
        or not symbol
        or symbol.startswith(f"{market}.")
        or _POSITION_SYMBOL.fullmatch(symbol) is None
    ):
        raise RiskInputError("position requires an unambiguous market-qualified security identity")
    return f"{market}.{symbol}", market, symbol


def aggregate_economic_positions(snapshot: PortfolioSnapshot) -> tuple[EconomicPosition, ...]:
    grouped: dict[str, list[NormalizedPosition]] = {}
    identities: dict[str, tuple[str, str]] = {}
    for position in snapshot.positions:
        identity, market, symbol = _canonical_identity(position)
        grouped.setdefault(identity, []).append(position)
        identities[identity] = (market, symbol)

    holdings: list[EconomicPosition] = []
    for identity in sorted(grouped):
        sources = grouped[identity]
        reference = sources[0]

        def metadata(position: NormalizedPosition) -> dict[str, Any]:
            canonical_themes = tuple(
                sorted(
                    {
                        canonical
                        for theme in position.themes
                        if (canonical := canonical_unknown(theme)) != "UNKNOWN"
                    }
                )
            ) or ("UNKNOWN",)
            values = {field: getattr(position, field) for field in _ECONOMIC_METADATA_FIELDS}
            values.update(
                {
                    "sector": canonical_unknown(position.sector),
                    "region": canonical_unknown(position.region),
                    "themes": canonical_themes,
                }
            )
            return values

        reference_metadata = metadata(reference)
        if any(metadata(source) != reference_metadata for source in sources[1:]):
            raise RiskInputError(f"conflicting metadata for economic position {identity}")

        def aggregate_optional(field: str) -> Decimal | None:
            present = [
                value
                for value in (getattr(source, field) for source in sources)
                if value is not None
            ]
            return sum(present, Decimal("0")) if present else None

        market, symbol = identities[identity]
        holdings.append(
            EconomicPosition(
                identity=identity,
                market=market,
                symbol=symbol,
                security_type=reference.security_type,
                currency=reference.currency,
                sector=reference_metadata["sector"],
                industry=reference.industry,
                region=reference_metadata["region"],
                themes=reference_metadata["themes"],
                asset_class=reference.asset_class,
                position_role=reference.position_role,
                weight=sum((source.weight for source in sources), Decimal("0")),
                market_value_base=sum(
                    (source.market_value_base for source in sources), Decimal("0")
                ),
                unrealized_pl_base=aggregate_optional("unrealized_pl_base"),
                realized_pl_base=aggregate_optional("realized_pl_base"),
                source_holding_count=len(sources),
            )
        )
    return tuple(holdings)


def load_portfolio_policy(data_home: Any) -> PortfolioPolicy | None:
    payload = load_optional_json_object(data_home, "risk_policy.json")
    if payload is None:
        return None
    if payload.get("provenance") != "USER_CONFIG":
        raise PolicyConfigError("risk policy provenance must be USER_CONFIG")
    thresholds = payload.get("thresholds")
    if not isinstance(thresholds, Mapping):
        raise PolicyConfigError("risk policy thresholds must be an object")
    allowed = set(PortfolioPolicy.__dataclass_fields__)
    if set(thresholds) - allowed:
        raise PolicyConfigError("risk policy contains an unsupported threshold")
    try:
        values = {
            name: Decimal(str(value))
            for name, value in thresholds.items()
            if value is not None and not isinstance(value, bool)
        }
    except (InvalidOperation, ValueError) as exc:
        raise PolicyConfigError("risk policy threshold must be decimal") from exc
    if len(values) != len(thresholds):
        raise PolicyConfigError("risk policy threshold must be decimal")
    try:
        return PortfolioPolicy(**values)
    except ValueError as exc:
        raise PolicyConfigError(str(exc)) from exc


@dataclass(frozen=True)
class Concentration:
    status: str
    top_1: Decimal | None
    top_3: Decimal | None
    top_5: Decimal | None
    hhi: Decimal | None
    effective_positions: Decimal | None
    note: str


@dataclass(frozen=True)
class ExposureViews:
    sector: Mapping[str, Decimal]
    region: Mapping[str, Decimal]
    theme_full: Mapping[str, Decimal]
    theme_split: Mapping[str, Decimal]


@dataclass(frozen=True)
class CorrelationPair:
    symbols: tuple[str, str]
    coefficient: Decimal
    sample_count: int
    band: str


@dataclass(frozen=True)
class CorrelationEvidence:
    status: str
    lookback: int
    minimum_observations: int
    medium_threshold: Decimal
    high_threshold: Decimal
    adjusted_prices: bool
    coverage: Mapping[str, int]
    matrix: Mapping[str, Mapping[str, Decimal | None]]
    pairs: tuple[CorrelationPair, ...]
    clusters: tuple[tuple[str, ...], ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class RiskFinding:
    type: str
    severity: str
    summary: str
    evidence: Mapping[str, Any]
    affected_positions: tuple[str, ...]
    review_priority: str
    simulation_direction: str | None = None


@dataclass(frozen=True)
class RiskReport:
    schema_version: str
    as_of: Any
    source_snapshot_id: str
    coverage: Mapping[str, Any]
    data_quality_status: str
    data_quality_notes: tuple[str, ...]
    overall_risk: str
    concentration: Concentration
    exposures: ExposureViews
    correlation: CorrelationEvidence
    top_risks: tuple[RiskFinding, ...]
    policy_status: str = "NOT_CONFIGURED"
    policy_checks: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = _json_value(self)
        payload["exposures"] = {
            "sector": {"basis": "NAV", "weights": payload["exposures"]["sector"]},
            "region": {"basis": "NAV", "weights": payload["exposures"]["region"]},
            "theme_full": {
                "view": "OVERLAP_FULL_ALLOCATION",
                "basis": "NAV",
                "weights": payload["exposures"]["theme_full"],
            },
            "theme_split": {
                "view": "ADDITIVE_SPLIT_ALLOCATION",
                "basis": "NAV",
                "weights": payload["exposures"]["theme_split"],
            },
        }
        return payload


def _sorted_map(values: Mapping[str, Decimal]) -> Mapping[str, Decimal]:
    return MappingProxyType(dict(sorted(values.items())))


def _concentration(
    snapshot: PortfolioSnapshot, positions: Sequence[EconomicPosition]
) -> Concentration:
    weights = tuple(position.weight for position in positions)
    if any(weight < 0 for weight in weights) or snapshot.short_exposure_base > 0:
        return Concentration(
            "UNAVAILABLE",
            None,
            None,
            None,
            None,
            None,
            "Long-only NAV concentration is unavailable for signed short or hedge positions; "
            "signed weights are never fed to HHI.",
        )
    ranked = sorted(weights, reverse=True)
    top = lambda count: sum(ranked[:count], Decimal("0"))  # noqa: E731
    hhi = sum((weight * weight for weight in weights), Decimal("0"))
    effective = Decimal("1") / hhi if hhi else None
    return Concentration(
        "AVAILABLE",
        top(1),
        top(3),
        top(5),
        hhi,
        effective,
        "Nonnegative position NAV weights; cash is excluded from position concentration.",
    )


def _exposures(positions: Sequence[EconomicPosition]) -> ExposureViews:
    sector: dict[str, Decimal] = {}
    region: dict[str, Decimal] = {}
    full: dict[str, Decimal] = {}
    split: dict[str, Decimal] = {}
    for position in positions:
        weight = position.weight
        for target, label in ((sector, position.sector), (region, position.region)):
            key = label or "UNKNOWN"
            target[key] = target.get(key, Decimal("0")) + weight
        themes = tuple(dict.fromkeys(position.themes)) or ("UNKNOWN",)
        for theme in themes:
            full[theme] = full.get(theme, Decimal("0")) + weight
            split[theme] = split.get(theme, Decimal("0")) + weight / Decimal(len(themes))
    return ExposureViews(
        _sorted_map(sector), _sorted_map(region), _sorted_map(full), _sorted_map(split)
    )


def _valid_returns(points: Sequence[PricePoint] | None) -> dict[date, Decimal] | None:
    if points is None or len(points) < 2:
        return None
    previous_date: date | None = None
    previous_close: Decimal | None = None
    returns: dict[date, Decimal] = {}
    for point in points:
        try:
            close = Decimal(point.adjusted_close)
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not close.is_finite() or close <= 0:
            return None
        if previous_date is not None and point.date <= previous_date:
            return None
        if previous_close is not None:
            with localcontext() as context:
                context.prec = 50
                daily_return = close / previous_close - Decimal("1")
            if not daily_return.is_finite():
                return None
            returns[point.date] = daily_return
        previous_date, previous_close = point.date, close
    return returns


def _pearson(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal | None:
    count = len(left)
    if count != len(right) or count < 2:
        return None
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in (*left, *right)):
        return None
    with localcontext() as context:
        context.prec = 50
        left_mean = sum(left, Decimal("0")) / count
        right_mean = sum(right, Decimal("0")) / count
        covariance = sum(
            ((x - left_mean) * (y - right_mean) for x, y in zip(left, right)), Decimal("0")
        )
        left_ss = sum(((x - left_mean) ** 2 for x in left), Decimal("0"))
        right_ss = sum(((y - right_mean) ** 2 for y in right), Decimal("0"))
        if not left_ss or not right_ss:
            return None
        denominator = (left_ss * right_ss).sqrt()
        if not denominator or not denominator.is_finite():
            return None
        coefficient = covariance / denominator
        if not coefficient.is_finite() or abs(coefficient) > 1:
            return None
        return +coefficient


def _validate_risk_config(
    lookback: int,
    minimum_observations: int,
    medium_correlation: Decimal,
    high_correlation: Decimal,
) -> None:
    if isinstance(lookback, bool) or not isinstance(lookback, int):
        raise RiskConfigError("lookback must be an integer")
    if not 2 <= lookback <= 5000:
        raise RiskConfigError("lookback must be between 2 and 5000 trading prices")
    if isinstance(minimum_observations, bool) or not isinstance(minimum_observations, int):
        raise RiskConfigError("minimum_observations must be an integer")
    if minimum_observations <= 0:
        raise RiskConfigError("minimum_observations must be positive")
    if minimum_observations > lookback - 1:
        raise RiskConfigError("minimum_observations exceeds the lookback return capacity")
    if not isinstance(medium_correlation, Decimal) or not isinstance(high_correlation, Decimal):
        raise RiskConfigError("correlation thresholds must be Decimal values")
    if not medium_correlation.is_finite() or not high_correlation.is_finite():
        raise RiskConfigError("correlation thresholds must be finite")
    if not Decimal("0") <= medium_correlation <= high_correlation <= Decimal("1"):
        raise RiskConfigError("correlation thresholds require 0 <= medium <= high <= 1")


def _clusters(
    symbols: Sequence[str], high_edges: Sequence[tuple[str, str]]
) -> tuple[tuple[str, ...], ...]:
    neighbors = {symbol: set() for symbol in symbols}
    for left, right in high_edges:
        neighbors[left].add(right)
        neighbors[right].add(left)
    found: list[tuple[str, ...]] = []
    visited: set[str] = set()
    for symbol in symbols:
        if symbol in visited or not neighbors[symbol]:
            continue
        pending = [symbol]
        component: set[str] = set()
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(neighbors[current] - component)
        visited.update(component)
        found.append(tuple(sorted(component)))
    return tuple(sorted(found))


def _correlation(
    snapshot: PortfolioSnapshot,
    positions: Sequence[EconomicPosition],
    provider: PriceHistoryProvider | None,
    *,
    lookback: int,
    minimum: int,
    high: Decimal,
    medium: Decimal,
) -> CorrelationEvidence:
    identities = {position.identity: position for position in positions}
    symbols = tuple(sorted(identities))
    if provider is None:
        return CorrelationEvidence(
            "UNAVAILABLE",
            lookback,
            minimum,
            medium,
            high,
            True,
            MappingProxyType({"requested": len(symbols), "available": 0}),
            MappingProxyType({}),
            (),
            (),
            ("Adjusted daily price provider was not supplied; correlation was not inferred.",),
        )
    histories: dict[str, dict[date, Decimal]] = {}
    invalid: list[str] = []
    for identity in symbols:
        position = identities[identity]
        try:
            returns = _valid_returns(
                provider.history(
                    position.symbol,
                    market=position.market,
                    lookback=lookback,
                    as_of=snapshot.as_of.date(),
                )
            )
        except Exception:
            returns = None
        if returns is None:
            invalid.append(identity)
        else:
            histories[identity] = returns
    available_symbols = tuple(sorted(histories))
    matrix: dict[str, dict[str, Decimal | None]] = {
        left: {right: Decimal("1") if left == right else None for right in available_symbols}
        for left in available_symbols
    }
    pairs: list[CorrelationPair] = []
    high_edges: list[tuple[str, str]] = []
    computed_pairs = 0
    for index, left in enumerate(available_symbols):
        for right in available_symbols[index + 1 :]:
            dates = sorted(set(histories[left]) & set(histories[right]))
            if len(dates) < minimum:
                continue
            coefficient = _pearson(
                [histories[left][day] for day in dates], [histories[right][day] for day in dates]
            )
            if coefficient is None:
                continue
            computed_pairs += 1
            matrix[left][right] = coefficient
            matrix[right][left] = coefficient
            magnitude = abs(coefficient)
            if magnitude >= medium:
                band = "HIGH" if magnitude >= high else "MEDIUM"
                pairs.append(CorrelationPair((left, right), coefficient, len(dates), band))
                if magnitude >= high:
                    high_edges.append((left, right))
    status = "UNAVAILABLE"
    expected_pairs = len(symbols) * (len(symbols) - 1) // 2
    if computed_pairs:
        status = (
            "COMPLETE"
            if len(histories) == len(symbols) and computed_pairs == expected_pairs
            else "PARTIAL"
        )
    notes = []
    if invalid:
        notes.append("Missing or invalid adjusted histories: " + ", ".join(sorted(invalid)))
    if status == "UNAVAILABLE":
        notes.append("No pair met the minimum overlapping adjusted-return observations.")
    elif computed_pairs < expected_pairs:
        notes.append("Some position pairs lacked the minimum overlapping return observations.")
    frozen_matrix = MappingProxyType(
        {
            symbol: MappingProxyType(dict(sorted(row.items())))
            for symbol, row in sorted(matrix.items())
        }
    )
    return CorrelationEvidence(
        status,
        lookback,
        minimum,
        medium,
        high,
        True,
        MappingProxyType({"requested": len(symbols), "available": len(histories)}),
        frozen_matrix,
        tuple(sorted(pairs, key=lambda pair: (-abs(pair.coefficient), pair.symbols))),
        _clusters(available_symbols, high_edges),
        tuple(notes),
    )


def _findings(
    snapshot: PortfolioSnapshot,
    economic_positions: Sequence[EconomicPosition],
    concentration: Concentration,
    exposures: ExposureViews,
    correlation: CorrelationEvidence,
    policy_checks: Mapping[str, Any],
    high_correlation: Decimal,
) -> tuple[RiskFinding, ...]:
    positions = sorted(
        economic_positions, key=lambda position: (-abs(position.weight), position.identity)
    )
    if not positions:
        return tuple(
            RiskFinding(
                finding_type,
                "LOW",
                summary,
                MappingProxyType({"available_positions": 0}),
                (),
                "P3",
            )
            for finding_type, summary in (
                (
                    "POSITION_DATA_UNAVAILABLE",
                    "No positions are present; position concentration has no risk population.",
                ),
                (
                    "CLASSIFICATION_DATA_UNAVAILABLE",
                    "No positions are present; sector, region, and theme review populations are empty.",
                ),
                (
                    "CORRELATION_DATA_UNAVAILABLE",
                    "No positions are present; adjusted-return correlation has no pair population.",
                ),
            )
        )
    findings: list[RiskFinding] = []
    if concentration.status == "AVAILABLE":
        findings.append(
            RiskFinding(
                "POSITION_CONCENTRATION",
                "MEDIUM",
                "Largest holding is the primary single-name concentration review signal.",
                MappingProxyType(
                    {
                        "basis": "NAV",
                        "top_1": concentration.top_1,
                        "top_3": concentration.top_3,
                        "policy_threshold_applied": any(
                            name in policy_checks for name in ("single_position", "top_3", "top_5")
                        ),
                    }
                ),
                tuple(position.identity for position in positions[:3]),
                "P1",
                "Hypothetical only: simulate reducing the largest weight and recompute concentration.",
            )
        )
    else:
        findings.append(
            RiskFinding(
                "SIGNED_EXPOSURE",
                "MEDIUM",
                "Signed positions require gross/net and hedge-aware review; long-only HHI is unavailable.",
                MappingProxyType({"short_exposure_base": snapshot.short_exposure_base}),
                tuple(position.identity for position in positions if position.weight < 0),
                "P1",
            )
        )
    sector, sector_weight = max(exposures.sector.items(), key=lambda item: (item[1], item[0]))
    findings.append(
        RiskFinding(
            "SECTOR_CONCENTRATION",
            "HIGH" if policy_checks.get("sector", {}).get("breached") else "MEDIUM",
            f"{sector} is the largest deterministic sector exposure.",
            MappingProxyType(
                {
                    "basis": "NAV",
                    "sector": sector,
                    "weight": sector_weight,
                    "policy_threshold_applied": "sector" in policy_checks,
                }
            ),
            tuple(position.identity for position in positions if position.sector == sector),
            "P2",
            "Hypothetical only: simulate lower exposure to the largest sector and recompute views.",
        )
    )
    pnl_values = {
        position.identity: sum(
            (
                value
                for value in (position.unrealized_pl_base, position.realized_pl_base)
                if value is not None
            ),
            Decimal("0"),
        )
        for position in positions
        if position.unrealized_pl_base is not None or position.realized_pl_base is not None
    }
    pnl_total = sum((abs(value) for value in pnl_values.values()), Decimal("0"))
    roles_configured = bool(positions) and all(position.position_role for position in positions)
    if pnl_total:
        symbol, amount = max(pnl_values.items(), key=lambda item: (abs(item[1]), item[0]))
        findings.append(
            RiskFinding(
                "PNL_CONCENTRATION",
                "MEDIUM",
                "One holding dominates the available absolute realized and unrealized P&L evidence.",
                MappingProxyType(
                    {
                        "basis": "ABSOLUTE_REPORTED_PNL",
                        "largest_absolute_share": abs(amount) / pnl_total,
                        "source_fields": ("unrealized_pl_base", "realized_pl_base"),
                    }
                ),
                (symbol,),
                "P1",
                "Hypothetical only: simulate the dominant P&L contributor returning to cost.",
            )
        )
    elif roles_configured:
        roles: dict[str, Decimal] = {}
        for position in positions:
            role = str(position.position_role).upper()
            roles[role] = roles.get(role, Decimal("0")) + position.weight
        dominant_role, dominant_weight = max(roles.items(), key=lambda item: (item[1], item[0]))
        findings.append(
            RiskFinding(
                "ROLE_IMBALANCE",
                "LOW",
                f"{dominant_role} is the largest configured portfolio-role exposure.",
                MappingProxyType(
                    {
                        "basis": "NAV",
                        "roles": _sorted_map(roles),
                        "dominant_weight": dominant_weight,
                    }
                ),
                tuple(
                    position.identity
                    for position in positions
                    if position.position_role and position.position_role.upper() == dominant_role
                ),
                "P3",
                "Hypothetical only: simulate a shock to the dominant configured role.",
            )
        )
    elif correlation.clusters:
        cluster = correlation.clusters[0]
        findings.append(
            RiskFinding(
                "CORRELATION_CLUSTER",
                "MEDIUM",
                "A high-correlation cluster may reduce effective diversification.",
                MappingProxyType({"symbols": cluster, "threshold": high_correlation}),
                cluster,
                "P1",
                "Hypothetical only: simulate a shock shared by the correlated cluster.",
            )
        )
    else:
        theme, theme_weight = max(
            exposures.theme_split.items(), key=lambda item: (item[1], item[0])
        )
        findings.append(
            RiskFinding(
                "THEME_CONCENTRATION",
                "HIGH" if policy_checks.get("theme_split", {}).get("breached") else "LOW",
                f"{theme} is the largest additive split-allocation theme exposure.",
                MappingProxyType(
                    {
                        "view": "ADDITIVE_SPLIT_ALLOCATION",
                        "theme": theme,
                        "weight": theme_weight,
                        "policy_threshold_applied": "theme_split" in policy_checks,
                    }
                ),
                tuple(
                    position.identity
                    for position in positions
                    if theme in (position.themes or ("UNKNOWN",))
                ),
                "P3",
            )
        )
    return tuple(findings[:3])


def analyze_risk(
    snapshot: PortfolioSnapshot,
    *,
    price_provider: PriceHistoryProvider | None = None,
    lookback: int = 120,
    minimum_observations: int = 60,
    high_correlation: Decimal = Decimal("0.75"),
    medium_correlation: Decimal = Decimal("0.55"),
    policy: PortfolioPolicy | None = None,
) -> RiskReport:
    _validate_risk_config(lookback, minimum_observations, medium_correlation, high_correlation)
    economic_positions = aggregate_economic_positions(snapshot)
    concentration = _concentration(snapshot, economic_positions)
    exposures = _exposures(economic_positions)
    correlation = _correlation(
        snapshot,
        economic_positions,
        price_provider,
        lookback=lookback,
        minimum=minimum_observations,
        high=high_correlation,
        medium=medium_correlation,
    )
    checks: dict[str, Any] = {}
    if policy is not None:
        candidates = {
            "single_position": concentration.top_1,
            "top_3": concentration.top_3,
            "top_5": concentration.top_5,
            "sector": max(exposures.sector.values(), default=Decimal("0")),
            "region": max(exposures.region.values(), default=Decimal("0")),
            "theme_split": max(exposures.theme_split.values(), default=Decimal("0")),
        }
        for name, measured in candidates.items():
            threshold = getattr(policy, name)
            if threshold is not None and measured is not None:
                checks[name] = MappingProxyType(
                    {"measured": measured, "threshold": threshold, "breached": measured > threshold}
                )
    frozen_checks = MappingProxyType(dict(sorted(checks.items())))
    findings = _findings(
        snapshot,
        economic_positions,
        concentration,
        exposures,
        correlation,
        frozen_checks,
        high_correlation,
    )
    if any(checks.get(name, {}).get("breached") for name in ("single_position", "top_3", "top_5")):
        findings = (replace(findings[0], severity="HIGH"), *findings[1:])
    notes = tuple(
        dict.fromkeys(
            (
                *snapshot.data_quality.warnings,
                *(f"missing field: {field}" for field in snapshot.data_quality.missing_fields),
                *snapshot.data_quality.source_errors,
                *correlation.notes,
            )
        )
    )
    quality_status = snapshot.data_quality.status
    if quality_status == "COMPLETE" and correlation.status != "COMPLETE":
        quality_status = "PARTIAL"
    return RiskReport(
        "1.0",
        snapshot.as_of,
        snapshot.snapshot_id,
        MappingProxyType(
            {
                "source_holdings": len(snapshot.positions),
                "positions": len(economic_positions),
                "classified_sector": sum(p.sector != "UNKNOWN" for p in economic_positions),
                "classified_region": sum(p.region != "UNKNOWN" for p in economic_positions),
                "price_histories": correlation.coverage,
            }
        ),
        quality_status,
        notes,
        max(
            (finding.severity for finding in findings),
            key=("LOW", "MEDIUM", "HIGH", "CRITICAL").index,
        ),
        concentration,
        exposures,
        correlation,
        findings,
        "CONFIGURED" if policy is not None else "NOT_CONFIGURED",
        frozen_checks,
    )
