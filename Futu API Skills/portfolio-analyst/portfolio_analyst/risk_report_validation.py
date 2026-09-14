"""Independent replay of version 1.1 risk metrics from serialized evidence."""

from decimal import Decimal, localcontext


def validate_current_metrics(payload: dict) -> None:
    # Deliberately independent of production risk_metrics derivation.
    classification = payload["classification_coverage"]
    checks = payload["policy_checks"]
    for dimension in ("sector", "region", "theme_split"):
        weights = {
            key: Decimal(value) for key, value in payload["exposures"][dimension]["weights"].items()
        }
        known = [value for key, value in weights.items() if key != "UNKNOWN"]
        unknown = weights.get("UNKNOWN", Decimal(0))
        lower = max(known, default=Decimal(0))
        upper = lower + unknown
        evidence = classification[dimension]
        for key, value in {
            "known_weight": sum(known, Decimal(0)),
            "unknown_weight": unknown,
            "lower_bound": lower,
            "upper_bound": upper,
        }.items():
            if Decimal(evidence[key]) != value:
                raise ValueError(f"{dimension} classification {key} does not reconcile")
        check = checks.get(dimension)
        threshold = Decimal(check["threshold"]) if check else None
        expected = (
            "NOT_CONFIGURED"
            if threshold is None
            else "BREACHED"
            if lower > threshold
            else "WITHIN_LIMIT"
            if upper <= threshold
            else "UNKNOWN"
        )
        if evidence["status"] != expected or (
            (threshold is None) != (evidence["threshold"] is None)
        ):
            raise ValueError(f"{dimension} classification status/threshold is inconsistent")
        if threshold is not None and (
            not 0 < threshold <= 1 or Decimal(evidence["threshold"]) != threshold
        ):
            raise ValueError(f"{dimension} classification threshold is invalid")
        if check:
            breached = None if expected == "UNKNOWN" else expected == "BREACHED"
            if (
                check["status"] != expected
                or check["breached"] is not breached
                or Decimal(check["measured"]) != lower
            ):
                raise ValueError(f"{dimension} policy check is not supported by bounds")
    for name in ("single_position", "top_3", "top_5"):
        check = checks.get(name)
        if check:
            source = "top_1" if name == "single_position" else name
            if Decimal(check["measured"]) != Decimal(payload["concentration"][source]):
                raise ValueError(f"{name} measured value does not match concentration")
    for finding in payload.get("top_risks", payload.get("largest_risks")):
        for label, dimension in (
            ("sector", "sector"),
            ("region", "region"),
            ("theme", "theme_split"),
        ):
            if finding["type"] == f"{label.upper()}_CLASSIFICATION_UNKNOWN":
                weights = payload["exposures"][dimension]["weights"]
                if any(key != "UNKNOWN" for key in weights) or finding["evidence"] != {
                    "dimension": dimension,
                    **classification[dimension],
                }:
                    raise ValueError("classification gap is not supported by exposure evidence")
                if finding["severity"] != "UNRATED" or finding["simulation_direction"] is not None:
                    raise ValueError("classification gap cannot authorize risk or a simulation")
            if finding["type"] == f"{label.upper()}_CONCENTRATION":
                known = {
                    key: Decimal(value)
                    for key, value in payload["exposures"][dimension]["weights"].items()
                    if key != "UNKNOWN"
                }
                if not known:
                    raise ValueError("UNKNOWN is not an evidenced concentration category")
                key, value = max(known.items(), key=lambda item: (item[1], item[0]))
                if (
                    finding["evidence"][label] != key
                    or Decimal(finding["evidence"]["weight"]) != value
                ):
                    raise ValueError("concentration finding does not match largest known category")
                if (
                    classification[dimension]["status"] == "UNKNOWN"
                    and finding["simulation_direction"] is not None
                ):
                    raise ValueError(
                        "unknown constraint cannot authorize a concentration simulation"
                    )
    concentration = payload["concentration"]
    total = sum(
        (Decimal(value) for value in payload["exposures"]["sector"]["weights"].values()), Decimal(0)
    )
    with localcontext() as context:
        context.prec = 50
        nav_hhi = Decimal(concentration["hhi"]) if concentration["hhi"] is not None else None
        if not total:
            if (
                concentration["holdings_hhi"] is not None
                or concentration["effective_positions"] is not None
            ):
                raise ValueError("empty holdings cannot have diversification metrics")
            return
        if nav_hhi is None or nav_hhi <= 0:
            raise ValueError("nonempty holdings require positive NAV HHI")
        for key, expected in (
            ("holdings_hhi", nav_hhi / total**2),
            ("effective_positions", total**2 / nav_hhi),
        ):
            # The NAV aggregates preserve 28-digit legacy accumulation (up to
            # 10,000 positions); relative error grows when replayed as a count.
            tolerance = max(Decimal(1), abs(expected)) * Decimal("1e-22")
            if (
                concentration[key] is None
                or abs(Decimal(concentration[key]) - expected) > tolerance
            ):
                raise ValueError(f"{key} is inconsistent with holdings-only weights")
