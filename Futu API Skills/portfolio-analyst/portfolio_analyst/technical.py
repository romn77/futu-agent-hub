"""Deterministic support/resistance context for selected held US equities."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from .futu_adapter import FutuReadError
from .models import PortfolioSnapshot, _decimal_text, _json_value, decimal_fits_report
from .risk import _canonical_identity, _latest_complete_market_close_date


MAX_TECHNICAL_SYMBOLS = 8
DAILY_START_DAYS = 200
WEEKLY_START_DAYS = 735
DAILY_BAR_CAP = 160
WEEKLY_BAR_CAP = 110
_DERIVATION_CONTEXT = Context(prec=600, rounding=ROUND_HALF_EVEN)
_PRICE_QUANTA = {
    2: Decimal("0.01"),
    3: Decimal("0.001"),
    4: Decimal("0.0001"),
}


class TechnicalInputError(ValueError):
    """Input cannot satisfy the bounded technical-evidence contract."""


class TechnicalHistoryProvider(Protocol):
    def bars(
        self,
        symbol: str,
        *,
        market: str,
        interval: str,
        as_of: date,
        timeout_seconds: float,
    ) -> Sequence["TechnicalBar"] | None: ...


@dataclass(frozen=True)
class TechnicalBar:
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "date": self.date.isoformat(),
            "open": _decimal_text(self.open),
            "high": _decimal_text(self.high),
            "low": _decimal_text(self.low),
            "close": _decimal_text(self.close),
            "volume": _decimal_text(self.volume),
        }


@dataclass(frozen=True)
class SelectedHolding:
    identity: str
    symbol: str
    security_type: str
    weight: Decimal
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TechnicalSelection:
    selected: tuple[SelectedHolding, ...]
    explicit_requested: tuple[str, ...]
    omitted: tuple[str, ...]
    eligible_count: int
    truncated: bool
    tie_breaker: str = "rule_priority_then_weight_desc_then_identity_asc"


@dataclass(frozen=True)
class TechnicalReport:
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _json_value(dict(self.payload))

    def to_markdown(self) -> str:
        payload = self.to_dict()
        lines = [
            "# Technical context",
            "",
            f"Snapshot: `{payload['source_snapshot_id']}`  ",
            f"As of: {payload['as_of']}  ",
            "Observed support/resistance context; zones are not forecasts or trade signals.",
            "",
            "| Symbol | Evidence | Daily 20/50 MA | Weekly 20/40 MA | Support zones | Resistance zones |",
            "|---|---|---:|---:|---|---|",
        ]
        for item in payload["instruments"]:
            averages = item["moving_averages"]
            support = _zones_text(item["support_zones"])
            resistance = _zones_text(item["resistance_zones"])
            lines.append(
                "| "
                + " | ".join(
                    (
                        item["identity"],
                        item["status"],
                        f"{averages['daily_20'] or 'Unknown'} / {averages['daily_50'] or 'Unknown'}",
                        f"{averages['weekly_20'] or 'Unknown'} / {averages['weekly_40'] or 'Unknown'}",
                        support,
                        resistance,
                    )
                )
                + " |"
            )
        if payload["selection"]["omitted"]:
            lines.extend(
                (
                    "",
                    "Selection cap disclosure: omitted "
                    + ", ".join(payload["selection"]["omitted"])
                    + ".",
                )
            )
        if payload["data_quality_notes"]:
            lines.extend(("", "Data gaps: " + "; ".join(payload["data_quality_notes"])))
        return "\n".join(lines) + "\n"


def _zones_text(zones: Sequence[Mapping[str, Any]]) -> str:
    if not zones:
        return "Unknown"
    return ", ".join(f"{zone['lower']}-{zone['upper']}" for zone in zones)


class FutuTechnicalHistoryProvider:
    """Read exact bounded forward-adjusted OHLCV series through the audited adapter."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    def bars(
        self,
        symbol: str,
        *,
        market: str,
        interval: str,
        as_of: date,
        timeout_seconds: float = 5,
    ) -> tuple[TechnicalBar, ...]:
        if market != "US" or interval not in {"1d", "1w"}:
            raise FutuReadError("technical history identity is outside the supported scope")
        start_days, maximum = (
            (DAILY_START_DAYS, DAILY_BAR_CAP)
            if interval == "1d"
            else (WEEKLY_START_DAYS, WEEKLY_BAR_CAP)
        )
        code = f"US.{symbol.upper()}"
        payload = self.adapter.read_private_json(
            "get_kline",
            [
                code,
                "--ktype",
                interval,
                "--start",
                (as_of - timedelta(days=start_days)).isoformat(),
                "--end",
                as_of.isoformat(),
                "--rehab",
                "forward",
                "--session",
                "NONE",
                "--num",
                str(maximum),
                "--json",
            ],
            timeout=timeout_seconds,
        )
        if (
            payload.get("code") != code
            or payload.get("ktype") != interval
            or payload.get("source") != "history"
        ):
            raise FutuReadError("audited K-line response identity is invalid")
        records = payload.get("data")
        if not isinstance(records, list) or len(records) > maximum:
            raise FutuReadError("audited K-line response data is invalid")
        parsed: list[TechnicalBar] = []
        for record in records:
            if not isinstance(record, Mapping):
                raise FutuReadError("audited K-line response contains an invalid row")
            try:
                parsed.append(
                    TechnicalBar(
                        date=datetime.strptime(str(record["time"]), "%Y-%m-%d %H:%M:%S").date(),
                        open=Decimal(str(record["open"])),
                        high=Decimal(str(record["high"])),
                        low=Decimal(str(record["low"])),
                        close=Decimal(str(record["close"])),
                        volume=Decimal(str(record["volume"])),
                    )
                )
            except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                raise FutuReadError("audited K-line response contains an invalid row") from exc
        try:
            return _normalize_bars(
                parsed,
                start=as_of - timedelta(days=start_days),
                cutoff=as_of,
                maximum=maximum,
            )
        except TechnicalInputError as exc:
            raise FutuReadError("audited K-line response contains invalid OHLCV evidence") from exc


def select_technical_holdings(
    snapshot: PortfolioSnapshot,
    *,
    risk_document: Mapping[str, Any] | None = None,
    explicit_symbols: Sequence[str] = (),
) -> TechnicalSelection:
    """Select at most eight held US stock/ETF identities by the documented stable rules."""

    with localcontext(_DERIVATION_CONTEXT):
        return _select_technical_holdings_in_context(
            snapshot,
            risk_document=risk_document,
            explicit_symbols=explicit_symbols,
        )


def _select_technical_holdings_in_context(
    snapshot: PortfolioSnapshot,
    *,
    risk_document: Mapping[str, Any] | None,
    explicit_symbols: Sequence[str],
) -> TechnicalSelection:
    if len(explicit_symbols) > MAX_TECHNICAL_SYMBOLS:
        raise TechnicalInputError("explicit technical selection accepts at most eight symbols")

    grouped: dict[str, dict[str, Any]] = {}
    for position in snapshot.positions:
        if position.quantity == 0:
            continue
        identity, market, symbol = _canonical_identity(position)
        if market != "US" or position.security_type not in {"STOCK", "ETF"}:
            continue
        record = grouped.setdefault(
            identity,
            {
                "identity": identity,
                "symbol": symbol,
                "security_type": position.security_type,
                "weight": Decimal("0"),
            },
        )
        if record["security_type"] != position.security_type:
            raise TechnicalInputError(f"conflicting security type for {identity}")
        with localcontext(_DERIVATION_CONTEXT):
            record["weight"] = record["weight"] + position.weight.copy_abs()

    ordered = sorted(
        grouped.values(),
        key=lambda item: (item["weight"].copy_negate(), item["identity"]),
    )
    reasons: dict[str, list[str]] = {}

    def include(identity: str, reason: str) -> None:
        if identity in grouped:
            reasons.setdefault(identity, [])
            if reason not in reasons[identity]:
                reasons[identity].append(reason)

    for item in ordered:
        if item["weight"] >= Decimal("0.05"):
            include(item["identity"], "WEIGHT_AT_LEAST_5_PERCENT_NAV")
    if len(reasons) < 5:
        for item in ordered:
            if item["identity"] in reasons:
                continue
            include(item["identity"], "TOP_WEIGHT_FILL_TO_FIVE")
            if len(reasons) >= min(5, len(ordered)):
                break

    explicit_identities: list[str] = []
    for raw in explicit_symbols:
        normalized = str(raw).strip().upper()
        identity = normalized if normalized.startswith("US.") else f"US.{normalized}"
        if identity not in grouped:
            raise TechnicalInputError(
                f"explicit symbol {normalized!r} must be a held US stock or ETF"
            )
        if identity not in explicit_identities:
            explicit_identities.append(identity)
    for identity in sorted(
        explicit_identities,
        key=lambda value: (grouped[value]["weight"].copy_negate(), value),
    ):
        include(identity, "EXPLICIT_HELD_SYMBOL")

    if risk_document is not None:
        if risk_document.get("source_snapshot_id") != snapshot.snapshot_id:
            raise TechnicalInputError("risk report does not bind the selected snapshot")
        findings = risk_document.get("top_risks", ())
        if not isinstance(findings, Sequence) or isinstance(findings, (str, bytes)):
            raise TechnicalInputError("risk report top_risks is invalid")
        for finding_index, finding in enumerate(findings[:3], 1):
            if not isinstance(finding, Mapping):
                raise TechnicalInputError("risk report finding is invalid")
            affected = finding.get("affected_positions", ())
            if not isinstance(affected, Sequence) or isinstance(affected, (str, bytes)):
                raise TechnicalInputError("risk report affected positions are invalid")
            for identity in sorted(set(map(str, affected))):
                include(identity, f"PRINCIPAL_RISK_{finding_index}")

    priority = {
        "WEIGHT_AT_LEAST_5_PERCENT_NAV": 0,
        "TOP_WEIGHT_FILL_TO_FIVE": 1,
        "EXPLICIT_HELD_SYMBOL": 2,
        "PRINCIPAL_RISK_1": 3,
        "PRINCIPAL_RISK_2": 4,
        "PRINCIPAL_RISK_3": 5,
    }
    requested = sorted(
        reasons,
        key=lambda identity: (
            min(priority[reason] for reason in reasons[identity]),
            grouped[identity]["weight"].copy_negate(),
            identity,
        ),
    )
    chosen = requested[:MAX_TECHNICAL_SYMBOLS]
    selected = tuple(
        SelectedHolding(
            identity=identity,
            symbol=grouped[identity]["symbol"],
            security_type=grouped[identity]["security_type"],
            weight=grouped[identity]["weight"],
            reasons=tuple(sorted(reasons[identity], key=lambda reason: priority[reason])),
        )
        for identity in chosen
    )
    return TechnicalSelection(
        selected=selected,
        explicit_requested=tuple(
            sorted(
                explicit_identities,
                key=lambda value: (grouped[value]["weight"].copy_negate(), value),
            )
        ),
        omitted=tuple(requested[MAX_TECHNICAL_SYMBOLS:]),
        eligible_count=len(ordered),
        truncated=len(requested) > MAX_TECHNICAL_SYMBOLS,
    )


def _normalize_bars(
    bars: Sequence[TechnicalBar], *, cutoff: date, maximum: int, start: date | None = None
) -> tuple[TechnicalBar, ...]:
    if len(bars) > 500:
        raise TechnicalInputError("bar evidence exceeds the input bound")
    by_date: dict[date, TechnicalBar] = {}
    for bar in bars:
        if (
            not isinstance(bar, TechnicalBar)
            or bar.date > cutoff
            or (start is not None and bar.date < start)
        ):
            raise TechnicalInputError("bar evidence is outside its requested window or malformed")
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if any(
            not isinstance(value, Decimal)
            or not value.is_finite()
            or not decimal_fits_report(value)
            for value in values
        ):
            raise TechnicalInputError("bar evidence contains an unsafe decimal")
        if (
            min(bar.open, bar.high, bar.low, bar.close) <= 0
            or bar.volume < 0
            or bar.high < max(bar.open, bar.close, bar.low)
            or bar.low > min(bar.open, bar.close, bar.high)
        ):
            raise TechnicalInputError("bar evidence violates OHLCV semantics")
        previous = by_date.get(bar.date)
        if previous is not None and previous != bar:
            raise TechnicalInputError("bar evidence has conflicting duplicate dates")
        by_date[bar.date] = bar
    return tuple(by_date[day] for day in sorted(by_date))[-maximum:]


def _mean(values: Sequence[Decimal], count: int) -> Decimal | None:
    if len(values) < count:
        return None
    with localcontext(_DERIVATION_CONTEXT):
        return +(sum(values[-count:], Decimal("0")) / Decimal(count))


def _price_quantum(current: Decimal, *series: Sequence[TechnicalBar]) -> Decimal:
    values = [current]
    for bars in series:
        for bar in bars:
            values.extend((bar.open, bar.high, bar.low, bar.close))
    places = max((_effective_decimal_places(value) for value in values), default=2)
    return _PRICE_QUANTA[min(4, max(2, places))]


def _effective_decimal_places(value: Decimal) -> int:
    if not value.is_finite() or value.is_zero():
        return 0
    decimal_tuple = value.as_tuple()
    exponent = int(decimal_tuple.exponent)
    if exponent >= 0:
        return 0
    trailing_zeroes = 0
    for digit in reversed(decimal_tuple.digits):
        if digit != 0:
            break
        trailing_zeroes += 1
    return max(0, -(exponent + trailing_zeroes))


def _quantize_price(value: Decimal, quantum: Decimal) -> Decimal:
    with localcontext(_DERIVATION_CONTEXT):
        return value.quantize(quantum, rounding=ROUND_HALF_EVEN)


def _pivot_evidence(bars: Sequence[TechnicalBar], quantum: Decimal) -> list[dict[str, str]]:
    pivots: list[dict[str, str]] = []
    for index in range(2, len(bars) - 2):
        neighbors = bars[index - 2 : index] + bars[index + 1 : index + 3]
        current = bars[index]
        if current.low <= min(item.low for item in neighbors) and current.low < max(
            item.low for item in neighbors
        ):
            pivots.append(
                {
                    "date": current.date.isoformat(),
                    "kind": "SWING_LOW",
                    "price": _decimal_text(_quantize_price(current.low, quantum)),
                }
            )
        if current.high >= max(item.high for item in neighbors) and current.high > min(
            item.high for item in neighbors
        ):
            pivots.append(
                {
                    "date": current.date.isoformat(),
                    "kind": "SWING_HIGH",
                    "price": _decimal_text(_quantize_price(current.high, quantum)),
                }
            )
    return pivots[-12:]


def _congestion(
    bars: Sequence[TechnicalBar], quantum: Decimal
) -> tuple[str, list[dict[str, str | int]]]:
    with localcontext(_DERIVATION_CONTEXT):
        return _congestion_in_context(bars, quantum)


def _congestion_in_context(
    bars: Sequence[TechnicalBar], quantum: Decimal
) -> tuple[str, list[dict[str, str | int]]]:
    if not bars or sum((item.volume for item in bars), Decimal("0")) <= 0:
        return "UNAVAILABLE", []
    low = min(item.low for item in bars)
    high = max(item.high for item in bars)
    width = (high - low) / Decimal("8")
    if width <= 0:
        return "UNAVAILABLE", []
    buckets = [Decimal("0") for _ in range(8)]
    observations = [0 for _ in range(8)]
    for bar in bars:
        typical = (bar.high + bar.low + bar.close) / Decimal("3")
        index = min(7, int((typical - low) / width))
        buckets[index] += bar.volume
        observations[index] += 1
    order = sorted(range(8), key=lambda index: (-buckets[index], index))[:3]
    nodes = [
        {
            "price": _decimal_text(
                _quantize_price(low + width * (Decimal(index) + Decimal("0.5")), quantum)
            ),
            "volume": _decimal_text(buckets[index]),
            "observations": observations[index],
        }
        for index in order
        if buckets[index] > 0
    ]
    return ("AVAILABLE" if nodes else "UNAVAILABLE"), nodes


def _atr(bars: Sequence[TechnicalBar]) -> Decimal | None:
    with localcontext(_DERIVATION_CONTEXT):
        return _atr_in_context(bars)


def _atr_in_context(bars: Sequence[TechnicalBar]) -> Decimal | None:
    if len(bars) < 15:
        return None
    ranges: list[Decimal] = []
    for previous, current in zip(bars[-15:-1], bars[-14:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(ranges, Decimal("0")) / Decimal(len(ranges))


def _zones(
    *,
    current: Decimal,
    daily: Sequence[TechnicalBar],
    pivots: Sequence[Mapping[str, str]],
    congestion_nodes: Sequence[Mapping[str, Any]],
    as_of: str,
    atr: Decimal | None,
    quantum: Decimal,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with localcontext(_DERIVATION_CONTEXT):
        return _zones_in_context(
            current=current,
            daily=daily,
            pivots=pivots,
            congestion_nodes=congestion_nodes,
            as_of=as_of,
            atr=atr,
            quantum=quantum,
        )


def _zones_in_context(
    *,
    current: Decimal,
    daily: Sequence[TechnicalBar],
    pivots: Sequence[Mapping[str, str]],
    congestion_nodes: Sequence[Mapping[str, Any]],
    as_of: str,
    atr: Decimal | None,
    quantum: Decimal,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(daily) < 20 or atr is None:
        return [], []
    width = max(atr / Decimal("2"), current * Decimal("0.005"))
    width = min(width, current * Decimal("0.05"))
    width = max(quantum, _quantize_price(width, quantum))
    candidates: list[tuple[Decimal, str]] = [
        (Decimal(item["price"]), item["kind"]) for item in pivots
    ]
    candidates.extend(
        (Decimal(str(item["price"])), "VOLUME_CONGESTION") for item in congestion_nodes
    )

    def build(kind: str, predicate: Any, reverse: bool) -> list[dict[str, Any]]:
        filtered = sorted(
            ((price, method) for price, method in candidates if predicate(price)),
            key=lambda item: item[0],
            reverse=reverse,
        )
        clusters: list[list[tuple[Decimal, str]]] = []
        for candidate in filtered:
            if clusters and abs(candidate[0] - clusters[-1][0][0]) <= width:
                clusters[-1].append(candidate)
            else:
                clusters.append([candidate])
        result: list[dict[str, Any]] = []
        for cluster in clusters[:2]:
            center = _quantize_price(
                sum((item[0] for item in cluster), Decimal("0")) / Decimal(len(cluster)),
                quantum,
            )
            lower = _quantize_price(max(Decimal("0"), center - width), quantum)
            upper = _quantize_price(center + width, quantum)
            if kind == "SUPPORT":
                upper = min(upper, current)
            else:
                lower = max(lower, current)
            if lower > upper:
                if kind == "SUPPORT":
                    lower = upper
                else:
                    upper = lower
            result.append(
                {
                    "lower": _decimal_text(lower),
                    "upper": _decimal_text(upper),
                    "kind": kind,
                    "method": "ATR_HALF_OR_HALF_PERCENT_CLUSTER",
                    "evidence": sorted(set(item[1] for item in cluster)),
                    "observations": len(cluster),
                    "as_of": as_of,
                }
            )
        return result

    return (
        build("SUPPORT", lambda price: price <= current, True),
        build("RESISTANCE", lambda price: price >= current, False),
    )


def derive_technical_context(
    *,
    identity: str,
    current_price: Decimal,
    daily: Sequence[TechnicalBar],
    weekly: Sequence[TechnicalBar],
    cutoff: date,
) -> dict[str, Any]:
    with localcontext(_DERIVATION_CONTEXT):
        return _derive_technical_context_in_context(
            identity=identity,
            current_price=current_price,
            daily=daily,
            weekly=weekly,
            cutoff=cutoff,
        )


def _derive_technical_context_in_context(
    *,
    identity: str,
    current_price: Decimal,
    daily: Sequence[TechnicalBar],
    weekly: Sequence[TechnicalBar],
    cutoff: date,
) -> dict[str, Any]:
    if (
        not current_price.is_finite()
        or current_price <= 0
        or not decimal_fits_report(current_price)
    ):
        raise TechnicalInputError(f"current price is invalid for {identity}")
    normalized_daily = _normalize_bars(daily, cutoff=cutoff, maximum=DAILY_BAR_CAP)
    normalized_weekly = _normalize_bars(weekly, cutoff=cutoff, maximum=WEEKLY_BAR_CAP)
    quantum = _price_quantum(current_price, normalized_daily, normalized_weekly)
    quantized_current = _quantize_price(current_price, quantum)
    daily_closes = [item.close for item in normalized_daily]
    weekly_closes = [item.close for item in normalized_weekly]
    pivots = _pivot_evidence(normalized_daily, quantum)
    congestion_status, congestion_nodes = _congestion(normalized_daily, quantum)
    atr = _atr(normalized_daily)
    as_of = normalized_daily[-1].date.isoformat() if normalized_daily else cutoff.isoformat()
    support, resistance = _zones(
        current=quantized_current,
        daily=normalized_daily,
        pivots=pivots,
        congestion_nodes=congestion_nodes,
        as_of=as_of,
        atr=atr,
        quantum=quantum,
    )
    reasons: list[str] = []
    if len(normalized_daily) < 50 or len(normalized_weekly) < 40:
        reasons.append("INSUFFICIENT_HISTORY")
    if congestion_status == "UNAVAILABLE":
        reasons.append("VOLUME_CONGESTION_UNAVAILABLE")
    return {
        "price_quantum": _decimal_text(quantum),
        "atr14": _decimal_text(_quantize_price(atr, quantum)) if atr is not None else None,
        "moving_averages": {
            "daily_20": _decimal_text(_quantize_price(value, quantum))
            if (value := _mean(daily_closes, 20)) is not None
            else None,
            "daily_50": _decimal_text(_quantize_price(value, quantum))
            if (value := _mean(daily_closes, 50)) is not None
            else None,
            "weekly_20": _decimal_text(_quantize_price(value, quantum))
            if (value := _mean(weekly_closes, 20)) is not None
            else None,
            "weekly_40": _decimal_text(_quantize_price(value, quantum))
            if (value := _mean(weekly_closes, 40)) is not None
            else None,
        },
        "swing_pivots": pivots,
        "volume_congestion": {
            "status": congestion_status,
            "method": "EIGHT_EQUAL_PRICE_BINS_BY_TRADED_VOLUME",
            "nodes": congestion_nodes,
        },
        "support_zones": support,
        "resistance_zones": resistance,
        "quality_reasons": reasons,
    }


def _bars_digest(bars: Sequence[TechnicalBar]) -> str:
    body = json.dumps([bar.to_dict() for bar in bars], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _unavailable_evidence(interval: str, start: date, end: date, reason: str) -> dict[str, Any]:
    return {
        "status": "UNAVAILABLE",
        "interval": interval,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "observed_start": None,
        "observed_end": None,
        "bar_count": 0,
        "sha256": _bars_digest(()),
        "bars": [],
        "reason": reason,
    }


def _available_evidence(
    interval: str, start: date, end: date, bars: Sequence[TechnicalBar]
) -> dict[str, Any]:
    return {
        "status": "AVAILABLE",
        "interval": interval,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "observed_start": bars[0].date.isoformat() if bars else None,
        "observed_end": bars[-1].date.isoformat() if bars else None,
        "bar_count": len(bars),
        "sha256": _bars_digest(bars),
        "bars": [bar.to_dict() for bar in bars],
        "reason": None,
    }


def _current_price(snapshot: PortfolioSnapshot, identity: str) -> Decimal:
    prices = []
    for position in snapshot.positions:
        if position.quantity == 0:
            continue
        position_identity, _market, _symbol = _canonical_identity(position)
        if position_identity == identity:
            prices.append(position.last_price_local)
    if not prices or any(price is None for price in prices):
        raise TechnicalInputError(f"current price evidence is unavailable for {identity}")
    if any(price != prices[0] for price in prices[1:]):
        raise TechnicalInputError(f"current price evidence conflicts for {identity}")
    return prices[0]


def _risk_digest(risk_document: Mapping[str, Any] | None) -> str | None:
    if risk_document is None:
        return None
    body = json.dumps(risk_document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class _EmbeddedBarsProvider:
    def __init__(self, evidence: Mapping[str, Mapping[str, Any]]) -> None:
        self.evidence = evidence

    def bars(self, symbol: str, *, market: str, interval: str, as_of: date, timeout_seconds: float):
        del as_of, timeout_seconds
        item = self.evidence[f"{market}.{symbol}"]["daily" if interval == "1d" else "weekly"]
        if item["status"] != "AVAILABLE":
            if item["reason"] == "SOURCE_TIMEOUT":
                raise TimeoutError("replayed source timeout")
            return None
        return tuple(
            TechnicalBar(
                date=date.fromisoformat(bar["date"]),
                open=Decimal(bar["open"]),
                high=Decimal(bar["high"]),
                low=Decimal(bar["low"]),
                close=Decimal(bar["close"]),
                volume=Decimal(bar["volume"]),
            )
            for bar in item["bars"]
        )


def analyze_technical(
    snapshot: PortfolioSnapshot,
    *,
    provider: TechnicalHistoryProvider | None,
    risk_document: Mapping[str, Any] | None = None,
    explicit_symbols: Sequence[str] = (),
    _replay_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> TechnicalReport:
    with localcontext(_DERIVATION_CONTEXT):
        return _analyze_technical_in_context(
            snapshot,
            provider=provider,
            risk_document=risk_document,
            explicit_symbols=explicit_symbols,
            _replay_evidence=_replay_evidence,
        )


def _analyze_technical_in_context(
    snapshot: PortfolioSnapshot,
    *,
    provider: TechnicalHistoryProvider | None,
    risk_document: Mapping[str, Any] | None,
    explicit_symbols: Sequence[str],
    _replay_evidence: Mapping[str, Mapping[str, Any]] | None,
) -> TechnicalReport:
    if risk_document is not None:
        from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

        from .schema import ReportSemanticError, validate_risk_document

        try:
            validate_risk_document(dict(risk_document))
        except (JsonSchemaValidationError, ReportSemanticError) as exc:
            raise TechnicalInputError("risk report is invalid") from exc
    selection = select_technical_holdings(
        snapshot, risk_document=risk_document, explicit_symbols=explicit_symbols
    )
    cutoff = _latest_complete_market_close_date(snapshot.as_of, "US")
    source = _EmbeddedBarsProvider(_replay_evidence) if _replay_evidence is not None else provider
    instruments: list[dict[str, Any]] = []
    notes: list[str] = []
    for selected in selection.selected:
        evidence: dict[str, Any] = {}
        parsed: dict[str, tuple[TechnicalBar, ...]] = {}
        for interval, label, start_days, maximum in (
            ("1d", "daily", DAILY_START_DAYS, DAILY_BAR_CAP),
            ("1w", "weekly", WEEKLY_START_DAYS, WEEKLY_BAR_CAP),
        ):
            start = cutoff - timedelta(days=start_days)
            try:
                rows = (
                    None
                    if source is None
                    else source.bars(
                        selected.symbol,
                        market="US",
                        interval=interval,
                        as_of=cutoff,
                        timeout_seconds=5,
                    )
                )
                if rows is None:
                    raise FutuReadError("source unavailable")
                normalized = _normalize_bars(rows, start=start, cutoff=cutoff, maximum=maximum)
                if not normalized:
                    raise FutuReadError("source returned no bars")
                parsed[interval] = normalized
                evidence[label] = _available_evidence(interval, start, cutoff, normalized)
            except (TimeoutError, subprocess.TimeoutExpired):
                evidence[label] = _unavailable_evidence(interval, start, cutoff, "SOURCE_TIMEOUT")
            except (FutuReadError, TechnicalInputError, KeyError, TypeError, ValueError):
                evidence[label] = _unavailable_evidence(
                    interval, start, cutoff, "SOURCE_OR_EVIDENCE_INVALID"
                )
        daily = parsed.get("1d", ())
        weekly = parsed.get("1w", ())
        try:
            current_price = _current_price(snapshot, selected.identity)
            if (
                not current_price.is_finite()
                or current_price <= 0
                or not decimal_fits_report(current_price)
            ):
                raise TechnicalInputError(
                    f"current price evidence is invalid for {selected.identity}"
                )
            context = derive_technical_context(
                identity=selected.identity,
                current_price=current_price,
                daily=daily,
                weekly=weekly,
                cutoff=cutoff,
            )
        except TechnicalInputError:
            current_price = None
            fallback_quantum = _price_quantum(Decimal("0"), daily, weekly)
            fallback_atr = _atr(daily)
            context = {
                "price_quantum": _decimal_text(fallback_quantum),
                "atr14": (
                    _decimal_text(_quantize_price(fallback_atr, fallback_quantum))
                    if fallback_atr is not None
                    else None
                ),
                "moving_averages": {
                    "daily_20": None,
                    "daily_50": None,
                    "weekly_20": None,
                    "weekly_40": None,
                },
                "swing_pivots": [],
                "volume_congestion": {
                    "status": "UNAVAILABLE",
                    "method": "EIGHT_EQUAL_PRICE_BINS_BY_TRADED_VOLUME",
                    "nodes": [],
                },
                "support_zones": [],
                "resistance_zones": [],
                "quality_reasons": ["CURRENT_PRICE_EVIDENCE_INVALID"],
            }
        available = sum(evidence[label]["status"] == "AVAILABLE" for label in ("daily", "weekly"))
        status = "COMPLETE" if available == 2 else "PARTIAL" if available else "UNAVAILABLE"
        if current_price is None and status == "COMPLETE":
            status = "PARTIAL"
        if current_price is None:
            notes.append(f"{selected.identity}: current price evidence is unavailable or invalid")
        elif status != "COMPLETE":
            notes.append(f"{selected.identity}: one or more technical timeframes unavailable")
        latest = daily[-1] if daily else weekly[-1] if weekly else None
        instruments.append(
            {
                "identity": selected.identity,
                "security_type": selected.security_type,
                "selection_reasons": list(selected.reasons),
                "status": status,
                "current_price": (
                    _decimal_text(_quantize_price(current_price, Decimal(context["price_quantum"])))
                    if current_price is not None and context["price_quantum"] is not None
                    else None
                ),
                "latest_bar": (
                    {
                        "date": latest.date.isoformat(),
                        "close": _decimal_text(
                            _quantize_price(latest.close, Decimal(context["price_quantum"]))
                        ),
                    }
                    if latest and context["price_quantum"] is not None
                    else None
                ),
                **context,
                "evidence": evidence,
            }
        )
    statuses = [item["status"] for item in instruments]
    quality = (
        "COMPLETE"
        if statuses and all(status == "COMPLETE" for status in statuses)
        else "UNAVAILABLE"
        if not statuses or all(status == "UNAVAILABLE" for status in statuses)
        else "PARTIAL"
    )
    risk_copy = _json_value(dict(risk_document)) if risk_document is not None else None
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "technical_report_id": "",
        "as_of": snapshot.to_dict()["as_of"],
        "analysis_cutoff": cutoff.isoformat(),
        "source_snapshot_id": snapshot.snapshot_id,
        "source_risk_digest": _risk_digest(risk_document),
        "selection": {
            "eligible_count": selection.eligible_count,
            "max_symbols": MAX_TECHNICAL_SYMBOLS,
            "tie_breaker": selection.tie_breaker,
            "selected": [
                {
                    "identity": item.identity,
                    "weight": _decimal_text(item.weight),
                    "reasons": list(item.reasons),
                }
                for item in selection.selected
            ],
            "explicit_requested": list(selection.explicit_requested),
            "omitted": list(selection.omitted),
            "truncated": selection.truncated,
        },
        "instruments": instruments,
        "data_quality_status": quality,
        "data_quality_notes": notes,
        "provenance": {
            "source_snapshot": snapshot.to_dict(),
            "source_risk_report": risk_copy,
            "adjustment": "FORWARD",
            "session": "REGULAR_ONLY",
            "algorithm": "SUPPORT_RESISTANCE_CONTEXT_V1",
        },
    }
    identity_body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["technical_report_id"] = (
        "ta-" + hashlib.sha256(identity_body.encode("utf-8")).hexdigest()[:24]
    )
    return TechnicalReport(MappingProxyType(payload))


def replay_technical_document(payload: Mapping[str, Any]) -> dict[str, Any]:
    from .snapshot_io import snapshot_from_document

    snapshot = snapshot_from_document(dict(payload["provenance"]["source_snapshot"]))
    evidence = {item["identity"]: item["evidence"] for item in payload["instruments"]}
    explicit = tuple(payload["selection"]["explicit_requested"])
    return analyze_technical(
        snapshot,
        provider=None,
        risk_document=payload["provenance"]["source_risk_report"],
        explicit_symbols=explicit,
        _replay_evidence=evidence,
    ).to_dict()
