"""Snapshot application service and local deterministic configuration loaders."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .classification import Classification, ClassificationMapper
from .futu_adapter import FutuAdapter
from .fx import StaticCurrencyConverter
from .models import PortfolioSnapshot
from .repository import FutuPortfolioRepository, PrivateJsonReader, SourceDataError
from .schema import validate_snapshot_document
from .settings import Settings, discover_futuapi_skill
from .snapshot import PortfolioBuilder


class ConfigError(ValueError):
    pass


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"required configuration is missing: {path.name}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"configuration is unreadable or invalid JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"configuration root must be an object: {path.name}")
    return payload


def load_currency_converter(data_home: Path) -> StaticCurrencyConverter:
    path = data_home / "config" / "fx_rates.json"
    payload = _load_json_object(path)
    raw_rates = payload.get("rates")
    if not isinstance(raw_rates, dict):
        raise ConfigError("fx_rates.json requires an object named rates")
    rates: dict[tuple[str, str], Decimal] = {}
    for pair, raw_rate in raw_rates.items():
        source, separator, target = str(pair).upper().partition("/")
        if not separator or len(source) != 3 or len(target) != 3:
            raise ConfigError(f"invalid FX pair in fx_rates.json: {pair}")
        try:
            rate = Decimal(str(raw_rate))
        except (InvalidOperation, ValueError) as exc:
            raise ConfigError(f"invalid FX rate in fx_rates.json: {pair}") from exc
        if not rate.is_finite() or rate <= 0:
            raise ConfigError(f"FX rate must be finite and positive: {pair}")
        rates[(source, target)] = rate
    return StaticCurrencyConverter(rates)


def load_classifications(data_home: Path) -> ClassificationMapper:
    path = data_home / "config" / "classifications.json"
    if not path.exists():
        return ClassificationMapper({})
    payload = _load_json_object(path)
    mappings: dict[str, Classification] = {}
    for code, raw in payload.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"classification must be an object: {code}")
        themes = raw.get("themes", ["UNKNOWN"])
        if not isinstance(themes, list) or not all(isinstance(item, str) for item in themes):
            raise ConfigError(f"classification themes must be a string array: {code}")
        mappings[str(code).upper()] = Classification(
            sector=str(raw.get("sector", "UNKNOWN")) or "UNKNOWN",
            industry=str(raw.get("industry", "UNKNOWN")) or "UNKNOWN",
            region=str(raw.get("region", "UNKNOWN")) or "UNKNOWN",
            themes=tuple(themes) or ("UNKNOWN",),
            asset_class=str(raw.get("asset_class", "UNKNOWN")) or "UNKNOWN",
        )
    return ClassificationMapper(mappings)


def generate_snapshot(
    settings: Settings,
    *,
    trd_env: str = "REAL",
    base_currency: str = "USD",
    adapter: PrivateJsonReader | None = None,
    as_of: datetime | None = None,
) -> PortfolioSnapshot:
    reader = adapter
    if reader is None:
        skill_dir = discover_futuapi_skill()
        if skill_dir is None:
            raise ConfigError("audited-compatible futuapi Skill is unavailable")
        reader = FutuAdapter(skill_dir)
    inputs = FutuPortfolioRepository(reader).read(trd_env=trd_env)
    if not inputs.accounts:
        raise SourceDataError(inputs.data_quality.source_errors[0])
    builder = PortfolioBuilder(
        base_currency=base_currency,
        converter=load_currency_converter(settings.data_home),
        classifications=load_classifications(settings.data_home),
    )
    snapshot = builder.build(
        accounts=inputs.accounts,
        cash_balances=inputs.cash_balances,
        positions=inputs.positions,
        as_of=as_of or datetime.now(timezone.utc),
        trd_env=trd_env.upper(),
        source_version=inputs.source_version,
        data_quality=inputs.data_quality,
    )
    validate_snapshot_document(snapshot.to_dict())
    return snapshot
