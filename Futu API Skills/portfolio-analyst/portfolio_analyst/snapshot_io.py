"""Confined immutable snapshot loading and full-body identity revalidation."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .identity import snapshot_identity
from .models import (
    Account,
    CashBalance,
    DataQuality,
    FxRateEvidence,
    NormalizedPosition,
    PortfolioSnapshot,
)
from .schema import validate_snapshot_document


class InvalidSnapshot(ValueError):
    """Saved snapshot does not satisfy its public schema and content identity."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSnapshot("snapshot JSON contains duplicate object keys")
        result[key] = value
    return result


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidSnapshot("snapshot timestamps must be timezone-aware")
    return parsed


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def snapshot_from_document(payload: dict[str, Any]) -> PortfolioSnapshot:
    try:
        validate_snapshot_document(payload)
        snapshot = PortfolioSnapshot(
            schema_version=payload["schema_version"],
            snapshot_id=payload["snapshot_id"],
            as_of=_datetime(payload["as_of"]),
            source=payload["source"],
            source_version=payload["source_version"],
            trd_env=payload["trd_env"],
            base_currency=payload["base_currency"],
            fx_evidence=tuple(
                FxRateEvidence(
                    item["source_currency"],
                    item["target_currency"],
                    Decimal(item["rate"]),
                    item["source"],
                    _datetime(item["effective_at"]),
                )
                for item in payload["fx_evidence"]
            ),
            accounts=tuple(Account(**item) for item in payload["accounts"]),
            cash_balances=tuple(
                CashBalance(
                    account_ref=item["account_ref"],
                    currency=item["currency"],
                    amount_local=Decimal(item["amount_local"]),
                    fx_to_base=_decimal(item["fx_to_base"]),
                    amount_base=_decimal(item["amount_base"]),
                    weight=_decimal(item["weight"]),
                    data_quality_notes=tuple(item["data_quality_notes"]),
                )
                for item in payload["cash_balances"]
            ),
            positions=tuple(
                NormalizedPosition(
                    **{
                        **item,
                        **{
                            field: _decimal(item[field])
                            for field in (
                                "quantity",
                                "can_sell_quantity",
                                "average_cost_local",
                                "last_price_local",
                                "market_value_local",
                                "unrealized_pl_local",
                                "realized_pl_local",
                                "fx_to_base",
                                "market_value_base",
                                "unrealized_pl_base",
                                "realized_pl_base",
                                "weight",
                            )
                        },
                        "themes": tuple(item["themes"]),
                        "data_quality_notes": tuple(item["data_quality_notes"]),
                    }
                )
                for item in payload["positions"]
            ),
            nav_base=Decimal(payload["nav_base"]),
            cash_base=Decimal(payload["cash_base"]),
            cash_weight=Decimal(payload["cash_weight"]),
            long_exposure_base=Decimal(payload["long_exposure_base"]),
            short_exposure_base=Decimal(payload["short_exposure_base"]),
            gross_exposure_base=Decimal(payload["gross_exposure_base"]),
            net_exposure_base=Decimal(payload["net_exposure_base"]),
            exposures={
                dimension: {label: Decimal(value) for label, value in weights.items()}
                for dimension, weights in payload["exposures"].items()
            },
            data_quality=DataQuality(
                status=payload["data_quality"]["status"],
                warnings=tuple(payload["data_quality"]["warnings"]),
                missing_fields=tuple(payload["data_quality"]["missing_fields"]),
                source_errors=tuple(payload["data_quality"]["source_errors"]),
                coverage=payload["data_quality"]["coverage"],
            ),
        )
    except InvalidSnapshot:
        raise
    except Exception as exc:
        raise InvalidSnapshot("snapshot schema or value validation failed") from exc
    if snapshot_identity(snapshot) != snapshot.snapshot_id:
        raise InvalidSnapshot("snapshot content identity does not match its complete body")
    return snapshot


def _confined_file(data_home: Path, relative_path: str | Path) -> Path:
    requested = Path(relative_path)
    if requested.is_absolute() or any(part in {"", ".", ".."} for part in requested.parts):
        raise InvalidSnapshot("snapshot path must remain below PORTFOLIO_AGENT_HOME")
    home = data_home.expanduser().resolve()
    candidate = (home / requested).resolve(strict=True)
    if not candidate.is_relative_to(home) or not candidate.is_file():
        raise InvalidSnapshot("snapshot path must name a confined regular file")
    current = home
    for part in requested.parts:
        current = current / part
        if current.is_symlink():
            raise InvalidSnapshot("snapshot paths must not contain symlinks")
    return candidate


def load_snapshot(data_home: Path, relative_path: str | Path) -> PortfolioSnapshot:
    try:
        source = _confined_file(data_home, relative_path)
        payload = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except InvalidSnapshot:
        raise
    except Exception as exc:
        raise InvalidSnapshot("snapshot could not be read as JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidSnapshot("snapshot document must be an object")
    return snapshot_from_document(payload)


def load_latest_snapshot(data_home: Path) -> PortfolioSnapshot:
    directory = data_home.expanduser().resolve() / "snapshots"
    if not directory.is_dir() or directory.is_symlink():
        raise InvalidSnapshot("no confined snapshots directory is available")
    candidates: list[tuple[datetime, str, str, PortfolioSnapshot]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            loaded = load_snapshot(data_home, path.relative_to(data_home))
        except InvalidSnapshot:
            continue
        candidates.append((loaded.as_of, loaded.snapshot_id, path.name, loaded))
    if not candidates:
        raise InvalidSnapshot("no valid immutable snapshots are available")
    return max(candidates, key=lambda item: item[:3])[3]
