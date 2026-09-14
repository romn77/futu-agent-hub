"""Deterministic portfolio risk analysis over one immutable snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import inspect
import queue
import re
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from .models import NormalizedPosition, PortfolioSnapshot, _json_value
from .normalization import canonical_unknown
from .risk_metrics import classification_coverage, holdings_diversification, policy_severity
from .secure_config import PrivateConfigError, load_optional_json_object


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


class UnsupportedRiskError(RiskInputError):
    """The current portfolio requires risk semantics not implemented in Phase 2."""


@dataclass(frozen=True)
class RiskBudget:
    max_history_symbols: int = 100
    max_pair_operations: int = 4950
    max_wall_seconds: Decimal = Decimal("30")
    provider_timeout_seconds: Decimal = Decimal("5")
    request_interval_seconds: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        integer_bounds = (
            ("max_history_symbols", self.max_history_symbols, 1, 200),
            ("max_pair_operations", self.max_pair_operations, 0, 19900),
        )
        for name, value, lower, upper in integer_bounds:
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise RiskConfigError(f"{name} must be an integer between {lower} and {upper}")
        for name, value, lower, upper in (
            ("max_wall_seconds", self.max_wall_seconds, Decimal("0.01"), Decimal("300")),
            (
                "provider_timeout_seconds",
                self.provider_timeout_seconds,
                Decimal("0.001"),
                Decimal("30"),
            ),
            (
                "request_interval_seconds",
                self.request_interval_seconds,
                Decimal("0"),
                Decimal("5"),
            ),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or not lower <= value <= upper
            ):
                raise RiskConfigError(f"{name} is outside the bounded analysis contract")
        if self.provider_timeout_seconds > self.max_wall_seconds:
            raise RiskConfigError("provider timeout cannot exceed the analysis wall-time budget")


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
    pnl_complete: bool
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
        if position.quantity == 0 or position.market_value_base == 0:
            continue
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
            values = [getattr(source, field) for source in sources]
            if any(value is None for value in values):
                return None
            return sum(values, Decimal("0"))

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
                pnl_complete=all(
                    source.unrealized_pl_base is not None
                    and source.realized_pl_base is not None
                    and source.unrealized_pl_base.is_finite()
                    and source.realized_pl_base.is_finite()
                    for source in sources
                ),
                source_holding_count=len(sources),
            )
        )
    return tuple(holdings)


def load_portfolio_policy(data_home: Any) -> PortfolioPolicy | None:
    try:
        payload = load_optional_json_object(data_home, "risk_policy.json")
    except PrivateConfigError as exc:
        raise PolicyConfigError("risk policy configuration is unavailable or unsafe") from exc
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
    holdings_hhi: Decimal | None = None


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
    date_cutoffs: Mapping[str, str]
    matrix: Mapping[str, Mapping[str, Decimal | None]]
    pairs: tuple[CorrelationPair, ...]
    inverse_pairs: tuple[CorrelationPair, ...]
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
    classification_coverage: Mapping[str, Any] = field(default_factory=dict)

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
    holdings_hhi, effective = holdings_diversification(weights)
    return Concentration(
        "AVAILABLE",
        top(1),
        top(3),
        top(5),
        hhi,
        effective,
        "Top weights and HHI use NAV; holdings_hhi and effective_positions use relative nonnegative holdings weights, excluding cash.",
        holdings_hhi,
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


def _valid_prices(
    points: Sequence[PricePoint] | None, *, start: date, as_of: date, max_points: int
) -> dict[date, Decimal] | None:
    if points is None or len(points) < 2:
        return None
    previous_date: date | None = None
    prices: dict[date, Decimal] = {}
    for point in points:
        try:
            close = Decimal(point.adjusted_close)
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not close.is_finite() or close <= 0:
            return None
        if point.date < start or point.date > as_of:
            return None
        if previous_date is not None and point.date <= previous_date:
            return None
        prices[point.date] = close
        previous_date = point.date
    return dict(tuple(prices.items())[-max_points:])


def _aligned_pair_returns(
    left: Mapping[date, Decimal], right: Mapping[date, Decimal]
) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]]:
    common_dates = sorted(set(left) & set(right))
    left_returns: list[Decimal] = []
    right_returns: list[Decimal] = []
    for start_date, end_date in zip(common_dates, common_dates[1:]):
        with localcontext() as context:
            context.prec = 50
            left_return = left[end_date] / left[start_date] - Decimal("1")
            right_return = right[end_date] / right[start_date] - Decimal("1")
        if not left_return.is_finite() or not right_return.is_finite():
            return (), ()
        left_returns.append(left_return)
        right_returns.append(right_return)
    return tuple(left_returns), tuple(right_returns)


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
        if Decimal("1") - abs(coefficient) <= Decimal("1e-40"):
            return Decimal("1") if coefficient > 0 else Decimal("-1")
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


def _bounded_history_call(
    provider: PriceHistoryProvider,
    position: EconomicPosition,
    *,
    lookback: int,
    as_of: date,
    timeout_seconds: Decimal,
) -> Sequence[PricePoint] | None:
    results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            kwargs = {"market": position.market, "lookback": lookback, "as_of": as_of}
            if "timeout_seconds" in inspect.signature(provider.history).parameters:
                kwargs["timeout_seconds"] = float(timeout_seconds)
            value = provider.history(position.symbol, **kwargs)
        except BaseException as exc:
            results.put((False, exc))
        else:
            results.put((True, value))

    worker = threading.Thread(target=invoke, daemon=True, name="portfolio-price-history")
    worker.start()
    worker.join(float(timeout_seconds))
    if worker.is_alive():
        raise TimeoutError("adjusted price provider exceeded its per-call timeout")
    ok, value = results.get_nowait()
    if not ok:
        raise value
    return value


MARKET_CLOSE_SCHEDULE_VERSION = "REGULAR_SESSIONS_V1"


_MARKET_CLOSE = {
    "US": ("America/New_York", datetime_time(16, 0)),
    "CA": ("America/Toronto", datetime_time(16, 0)),
    "HK": ("Asia/Hong_Kong", datetime_time(16, 0)),
    "CN": ("Asia/Shanghai", datetime_time(15, 0)),
    "SG": ("Asia/Singapore", datetime_time(17, 0)),
    "MY": ("Asia/Kuala_Lumpur", datetime_time(17, 0)),
    "JP": ("Asia/Tokyo", datetime_time(15, 30)),
    "AU": ("Australia/Sydney", datetime_time(16, 0)),
}


def _latest_complete_market_close_date(snapshot_instant: datetime, market: str) -> date:
    timezone_name, close_time = _MARKET_CLOSE[market]
    local = snapshot_instant.astimezone(ZoneInfo(timezone_name))
    candidate = local.date()
    close = datetime.combine(candidate, close_time, tzinfo=local.tzinfo)
    if local < close:
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _first_completed_market_session_on_or_after(event_instant: datetime, market: str) -> date:
    """Return the first modeled regular-session close that cannot precede an event."""

    timezone_name, close_time = _MARKET_CLOSE[market]
    local = event_instant.astimezone(ZoneInfo(timezone_name))
    candidate = local.date()
    close = datetime.combine(candidate, close_time, tzinfo=local.tzinfo)
    if local > close:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _correlation(
    snapshot: PortfolioSnapshot,
    positions: Sequence[EconomicPosition],
    provider: PriceHistoryProvider | None,
    *,
    lookback: int,
    minimum: int,
    high: Decimal,
    medium: Decimal,
    budget: RiskBudget,
    deadline: Decimal,
    clock: Callable[[], Decimal | float],
    sleeper: Callable[[float], None],
) -> CorrelationEvidence:
    identities = {position.identity: position for position in positions}
    symbols = tuple(sorted(identities))
    cutoff_dates = {
        identity: _latest_complete_market_close_date(snapshot.as_of, identities[identity].market)
        for identity in symbols
    }
    serialized_cutoffs = MappingProxyType(
        {identity: cutoff.isoformat() for identity, cutoff in cutoff_dates.items()}
    )
    if provider is None:
        return CorrelationEvidence(
            "UNAVAILABLE",
            lookback,
            minimum,
            medium,
            high,
            True,
            MappingProxyType({"requested": len(symbols), "available": 0}),
            serialized_cutoffs,
            MappingProxyType({}),
            (),
            (),
            (),
            ("Adjusted daily price provider was not supplied; correlation was not inferred.",),
        )
    histories: dict[str, dict[date, Decimal]] = {}
    invalid: list[str] = []
    source_failure: str | None = None
    for identity in symbols:
        position = identities[identity]
        cutoff = cutoff_dates[identity]
        requested_start = cutoff - timedelta(days=max(lookback * 2, 180))
        remaining = deadline - Decimal(str(clock()))
        if remaining <= 0:
            source_failure = (
                "Correlation wall-time budget exhausted; source circuit breaker opened."
            )
            break
        try:
            prices = _valid_prices(
                _bounded_history_call(
                    provider,
                    position,
                    lookback=lookback,
                    as_of=cutoff,
                    timeout_seconds=min(budget.provider_timeout_seconds, remaining),
                ),
                start=requested_start,
                as_of=cutoff,
                max_points=lookback,
            )
        except Exception:
            source_failure = "Adjusted price source failed or timed out; circuit breaker opened."
            break
        if prices is None:
            invalid.append(identity)
        else:
            histories[identity] = prices
        if budget.request_interval_seconds:
            sleeper(float(budget.request_interval_seconds))
    if source_failure:
        invalid.extend(identity for identity in symbols if identity not in histories)
    available_symbols = tuple(sorted(histories))
    matrix: dict[str, dict[str, Decimal | None]] = {
        left: {right: Decimal("1") if left == right else None for right in available_symbols}
        for left in available_symbols
    }
    pairs: list[CorrelationPair] = []
    inverse_pairs: list[CorrelationPair] = []
    high_edges: list[tuple[str, str]] = []
    computed_pairs = 0
    for index, left in enumerate(available_symbols):
        for right in available_symbols[index + 1 :]:
            left_returns, right_returns = _aligned_pair_returns(histories[left], histories[right])
            sample_count = len(left_returns)
            if sample_count < minimum:
                continue
            coefficient = _pearson(left_returns, right_returns)
            if coefficient is None:
                continue
            computed_pairs += 1
            matrix[left][right] = coefficient
            matrix[right][left] = coefficient
            if coefficient >= medium:
                band = "HIGH" if coefficient >= high else "MEDIUM"
                pairs.append(CorrelationPair((left, right), coefficient, sample_count, band))
                if coefficient >= high:
                    high_edges.append((left, right))
            elif coefficient <= -medium:
                band = "HIGH" if coefficient <= -high else "MEDIUM"
                inverse_pairs.append(
                    CorrelationPair((left, right), coefficient, sample_count, band)
                )
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
    if source_failure:
        notes.append(source_failure)
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
        serialized_cutoffs,
        frozen_matrix,
        tuple(sorted(pairs, key=lambda pair: (-pair.coefficient, pair.symbols))),
        tuple(sorted(inverse_pairs, key=lambda pair: (pair.coefficient, pair.symbols))),
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
    pnl_evidence_status: str,
    classification: Mapping[str, Any],
) -> tuple[RiskFinding, ...]:
    positions = sorted(
        economic_positions, key=lambda position: (-abs(position.weight), position.identity)
    )
    if not positions:
        return tuple(
            RiskFinding(
                finding_type,
                "UNRATED",
                summary,
                MappingProxyType({"available_positions": 0}),
                (),
                "P3",
            )
            for finding_type, summary in sorted(
                (
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
        )
    findings: list[RiskFinding] = []
    if concentration.status == "AVAILABLE":
        position_checks = tuple(
            policy_checks[name]
            for name in ("single_position", "top_3", "top_5")
            if name in policy_checks
        )
        findings.append(
            RiskFinding(
                "POSITION_CONCENTRATION",
                (
                    "HIGH"
                    if any(check["breached"] for check in position_checks)
                    else "LOW"
                    if position_checks
                    else "UNRATED"
                ),
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
    for dimension, label_field, priority in (
        ("sector", "sector", "P2"),
        ("region", "region", "P2"),
        ("theme_split", "theme", "P3"),
    ):
        weights = getattr(exposures, dimension)
        known = {label: weight for label, weight in weights.items() if label != "UNKNOWN"}
        coverage = classification[dimension]
        if not known:
            findings.append(
                RiskFinding(
                    f"{label_field.upper()}_CLASSIFICATION_UNKNOWN",
                    "UNRATED",
                    f"{label_field} classification is unavailable; concentration cannot be inferred.",
                    MappingProxyType({"dimension": dimension, **coverage}),
                    tuple(position.identity for position in positions),
                    priority,
                )
            )
            continue
        label, weight = max(known.items(), key=lambda item: (item[1], item[0]))
        check = policy_checks.get(dimension)
        severity = policy_severity([check] if check is not None else [])
        evidence = {
            **(
                {"view": "ADDITIVE_SPLIT_ALLOCATION"}
                if dimension == "theme_split"
                else {"basis": "NAV"}
            ),
            label_field: label,
            "weight": weight,
            "policy_threshold_applied": check is not None,
        }
        findings.append(
            RiskFinding(
                f"{label_field.upper()}_CONCENTRATION",
                severity,
                f"{label} is the largest known {label_field} exposure; constraint status: {coverage['status']}.",
                MappingProxyType(evidence),
                tuple(
                    position.identity
                    for position in positions
                    if (
                        label in position.themes
                        if dimension == "theme_split"
                        else getattr(position, dimension) == label
                    )
                ),
                priority,
                (
                    f"Hypothetical only: simulate lower exposure to the largest known {label_field} and recompute views."
                    if severity == "HIGH"
                    else None
                ),
            )
        )
    if pnl_evidence_status == "COMPLETE":
        pnl_values = {
            position.identity: position.unrealized_pl_base + position.realized_pl_base
            for position in positions
        }
        profits = tuple(
            sorted(
                ((identity, value) for identity, value in pnl_values.items() if value > 0),
                key=lambda item: (-item[1], item[0]),
            )
        )
        losses = tuple(
            sorted(
                ((identity, -value) for identity, value in pnl_values.items() if value < 0),
                key=lambda item: (-item[1], item[0]),
            )
        )
        profit_total = sum((value for _identity, value in profits), Decimal("0"))
        loss_total = sum((value for _identity, value in losses), Decimal("0"))
        if profit_total or loss_total:
            affected = tuple(
                dict.fromkeys(
                    (
                        *((profits[0][0],) if profits else ()),
                        *((losses[0][0],) if losses else ()),
                    )
                )
            )
            findings.append(
                RiskFinding(
                    "PNL_CONCENTRATION",
                    "UNRATED",
                    "Reported profit and loss pools are reviewed separately for concentration.",
                    MappingProxyType(
                        {
                            "basis": "ABSOLUTE_REPORTED_PNL",
                            "profit_total_base": profit_total,
                            "loss_total_abs_base": loss_total,
                            "largest_profit_share": (
                                profits[0][1] / profit_total if profit_total else None
                            ),
                            "largest_loss_share": (
                                losses[0][1] / loss_total if loss_total else None
                            ),
                            "source_fields": (
                                "unrealized_pl_base",
                                "realized_pl_base",
                            ),
                        }
                    ),
                    affected,
                    "P2",
                    "Hypothetical only: simulate removing the largest reported P&L contributor and recompute both pools.",
                )
            )
    roles_configured = bool(positions) and all(position.position_role for position in positions)
    if roles_configured:
        roles: dict[str, Decimal] = {}
        for position in positions:
            role = str(position.position_role).upper()
            roles[role] = roles.get(role, Decimal("0")) + position.weight
        dominant_role, dominant_weight = max(roles.items(), key=lambda item: (item[1], item[0]))
        findings.append(
            RiskFinding(
                "ROLE_IMBALANCE",
                "UNRATED",
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
    if correlation.clusters:
        cluster = correlation.clusters[0]
        findings.append(
            RiskFinding(
                "CORRELATION_CLUSTER",
                "UNRATED",
                "A high-correlation cluster may reduce effective diversification.",
                MappingProxyType({"symbols": cluster, "threshold": high_correlation}),
                cluster,
                "P1",
                "Hypothetical only: simulate a shock shared by the correlated cluster.",
            )
        )
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNRATED": 4}
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                severity_order[finding.severity],
                priority_order[finding.review_priority],
                finding.type,
                finding.affected_positions,
            ),
        )[:3]
    )


def analyze_risk(
    snapshot: PortfolioSnapshot,
    *,
    price_provider: PriceHistoryProvider | None = None,
    lookback: int = 120,
    minimum_observations: int = 60,
    high_correlation: Decimal = Decimal("0.75"),
    medium_correlation: Decimal = Decimal("0.55"),
    policy: PortfolioPolicy | None = None,
    budget: RiskBudget | None = None,
    clock: Callable[[], Decimal | float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> RiskReport:
    started = Decimal(str(clock()))
    active_budget = budget or RiskBudget()
    _validate_risk_config(lookback, minimum_observations, medium_correlation, high_correlation)
    all_economic_positions = aggregate_economic_positions(snapshot)
    if snapshot.short_exposure_base > 0 or any(
        position.weight < 0 for position in all_economic_positions
    ):
        raise UnsupportedRiskError(
            "signed short or hedge portfolios are unsupported by Phase 2 long-only risk semantics"
        )
    ranked_positions = sorted(
        all_economic_positions, key=lambda position: (-position.weight, position.identity)
    )
    economic_positions = tuple(ranked_positions)
    history_count = min(len(economic_positions), active_budget.max_history_symbols)
    while history_count * (history_count - 1) // 2 > active_budget.max_pair_operations:
        history_count -= 1
    history_positions = economic_positions[:history_count]
    budget_reasons = []
    if history_count < len(economic_positions):
        budget_reasons.append("history symbols capped by request or pair-operation budget")
    concentration = _concentration(snapshot, economic_positions)
    exposures = _exposures(economic_positions)
    correlation = _correlation(
        snapshot,
        history_positions,
        price_provider,
        lookback=lookback,
        minimum=minimum_observations,
        high=high_correlation,
        medium=medium_correlation,
        budget=active_budget,
        deadline=started + active_budget.max_wall_seconds,
        clock=clock,
        sleeper=sleeper,
    )
    classification = MappingProxyType(
        {
            dimension: MappingProxyType(
                classification_coverage(
                    getattr(exposures, dimension), getattr(policy, dimension) if policy else None
                )
            )
            for dimension in ("sector", "region", "theme_split")
        }
    )
    checks: dict[str, Any] = {}
    if policy is not None:
        candidates = {
            "single_position": concentration.top_1,
            "top_3": concentration.top_3,
            "top_5": concentration.top_5,
        }
        for name, measured in candidates.items():
            threshold = getattr(policy, name)
            if threshold is not None and measured is not None:
                checks[name] = MappingProxyType(
                    {"measured": measured, "threshold": threshold, "breached": measured > threshold}
                )
    for dimension, evidence in classification.items():
        if evidence["threshold"] is not None:
            checks[dimension] = MappingProxyType(
                {
                    "measured": evidence["lower_bound"],
                    "threshold": evidence["threshold"],
                    "breached": None
                    if evidence["status"] == "UNKNOWN"
                    else evidence["status"] == "BREACHED",
                    "status": evidence["status"],
                }
            )
    frozen_checks = MappingProxyType(dict(sorted(checks.items())))
    pnl_evidence_status = (
        "UNAVAILABLE"
        if not all_economic_positions
        else "PARTIAL"
        if len(economic_positions) < len(all_economic_positions)
        or any(not position.pnl_complete for position in economic_positions)
        else "COMPLETE"
    )
    findings = _findings(
        snapshot,
        economic_positions,
        concentration,
        exposures,
        correlation,
        frozen_checks,
        high_correlation,
        pnl_evidence_status,
        classification,
    )
    notes = tuple(
        dict.fromkeys(
            (
                *snapshot.data_quality.warnings,
                *(f"missing field: {field}" for field in snapshot.data_quality.missing_fields),
                *snapshot.data_quality.source_errors,
                *correlation.notes,
                *(
                    f"{dimension} classification unknown weight: {evidence['unknown_weight']}; constraint status: {evidence['status']}."
                    for dimension, evidence in classification.items()
                    if evidence["unknown_weight"] > 0
                ),
                *(f"BUDGET_LIMIT: {reason}" for reason in budget_reasons),
                *(
                    ("P&L evidence is incomplete; PNL_CONCENTRATION was not emitted.",)
                    if pnl_evidence_status == "PARTIAL"
                    else ()
                ),
            )
        )
    )
    quality_status = snapshot.data_quality.status
    if quality_status == "COMPLETE" and (
        correlation.status != "COMPLETE" or budget_reasons or pnl_evidence_status == "PARTIAL"
    ):
        quality_status = "PARTIAL"
    return RiskReport(
        "1.1",
        snapshot.as_of,
        snapshot.snapshot_id,
        MappingProxyType(
            {
                "source_holdings": len(snapshot.positions),
                "positions": len(economic_positions),
                "total_positions": len(all_economic_positions),
                "omitted_positions": len(all_economic_positions) - len(economic_positions),
                "budget_status": "BUDGET_LIMIT" if budget_reasons else "COMPLETE",
                "budget_reason": "; ".join(budget_reasons) if budget_reasons else None,
                "classified_sector": sum(p.sector != "UNKNOWN" for p in economic_positions),
                "classified_region": sum(p.region != "UNKNOWN" for p in economic_positions),
                "pnl_evidence_status": pnl_evidence_status,
                "price_histories": correlation.coverage,
            }
        ),
        quality_status,
        notes,
        next(
            (
                severity
                for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
                if any(finding.severity == severity for finding in findings)
            ),
            "UNRATED",
        ),
        concentration,
        exposures,
        correlation,
        findings,
        "CONFIGURED" if policy is not None else "NOT_CONFIGURED",
        frozen_checks,
        classification,
    )
