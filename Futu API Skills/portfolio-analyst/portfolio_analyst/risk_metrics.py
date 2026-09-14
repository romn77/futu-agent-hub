"""Classification uncertainty and holdings-only diversification over NAV exposures."""

from collections.abc import Mapping, Sequence
from decimal import Context, Decimal, localcontext


def classification_coverage(
    weights: Mapping[str, Decimal], threshold: Decimal | None
) -> dict[str, Decimal | str | None]:
    """Bound the largest actual category, retaining unclassified exposure as uncertainty."""
    known = [weight for label, weight in weights.items() if label != "UNKNOWN"]
    unknown = weights.get("UNKNOWN", Decimal(0))
    lower = max(known, default=Decimal(0))
    upper = lower + unknown
    status = (
        "NOT_CONFIGURED"
        if threshold is None
        else "BREACHED"
        if lower > threshold
        else "WITHIN_LIMIT"
        if upper <= threshold
        else "UNKNOWN"
    )
    return {
        "known_weight": sum(known, Decimal(0)),
        "unknown_weight": unknown,
        "lower_bound": lower,
        "upper_bound": upper,
        "threshold": threshold,
        "status": status,
    }


def holdings_diversification(weights: Sequence[Decimal]) -> tuple[Decimal | None, Decimal | None]:
    """Return holdings HHI and effective count; cash never changes relative allocation."""
    with localcontext(Context(prec=50)):
        total = sum(weights, Decimal(0))
        squares = sum((weight * weight for weight in weights), Decimal(0))
        if not total or not squares:
            return None, None
        return squares / (total * total), (total * total) / squares


def policy_severity(checks: Sequence[Mapping]) -> str:
    if any(check["breached"] is True for check in checks):
        return "HIGH"
    if checks and all(check["breached"] is False for check in checks):
        return "LOW"
    return "UNRATED"
