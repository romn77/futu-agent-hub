"""Snapshot application service and local deterministic configuration loaders."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .account_alias import AccountAliasError, AccountAliasStore, AccountAliaser
from .classification import Classification, ClassificationMapper
from .futu_adapter import FutuAdapter
from .fx import FxEvidenceError, StaticCurrencyConverter
from .models import PortfolioSnapshot
from .repository import FutuPortfolioRepository, PrivateJsonReader, SourceDataError
from .schema import validate_snapshot_document
from .security_metadata import SecurityMetadata, SecurityMetadataMapper
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
    source_label = payload.get("source")
    effective_at_raw = payload.get("effective_at")
    max_age_hours = payload.get("max_age_hours")
    if not isinstance(source_label, str) or not source_label.strip():
        raise ConfigError("fx_rates.json requires explicit source provenance")
    if not isinstance(effective_at_raw, str):
        raise ConfigError("fx_rates.json requires effective_at")
    try:
        effective_at = datetime.fromisoformat(effective_at_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError("fx_rates.json effective_at must be an ISO date-time") from exc
    if not isinstance(max_age_hours, int) or isinstance(max_age_hours, bool):
        raise ConfigError("fx_rates.json max_age_hours must be an integer")
    rates: dict[tuple[str, str], Decimal] = {}
    for pair, raw_rate in raw_rates.items():
        source_currency, separator, target_currency = str(pair).upper().partition("/")
        if not separator or len(source_currency) != 3 or len(target_currency) != 3:
            raise ConfigError(f"invalid FX pair in fx_rates.json: {pair}")
        try:
            rate = Decimal(str(raw_rate))
        except (InvalidOperation, ValueError) as exc:
            raise ConfigError(f"invalid FX rate in fx_rates.json: {pair}") from exc
        if not rate.is_finite() or rate <= 0:
            raise ConfigError(f"FX rate must be finite and positive: {pair}")
        rates[(source_currency, target_currency)] = rate
    try:
        return StaticCurrencyConverter(
            rates,
            source=source_label,
            effective_at=effective_at,
            max_age=timedelta(hours=max_age_hours),
        )
    except FxEvidenceError as exc:
        raise ConfigError(str(exc)) from exc


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


def load_security_metadata(data_home: Path) -> SecurityMetadataMapper:
    path = data_home / "config" / "security_metadata.json"
    if not path.exists():
        return SecurityMetadataMapper({})
    payload = _load_json_object(path)
    if payload.get("provenance") != "USER_CONFIG":
        raise ConfigError("security_metadata.json provenance must be USER_CONFIG")
    raw_securities = payload.get("securities")
    if not isinstance(raw_securities, dict):
        raise ConfigError("security_metadata.json requires an object named securities")
    mappings: dict[str, SecurityMetadata] = {}
    for code, raw in raw_securities.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"security metadata must be an object: {code}")
        currency = str(raw.get("currency", "")).upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ConfigError(f"security currency must be an ISO-style code: {code}")
        security_type = str(raw.get("security_type", "UNKNOWN")).upper() or "UNKNOWN"
        mappings[str(code).upper()] = SecurityMetadata(
            currency=currency,
            security_type=security_type,
            provenance="USER_CONFIG",
        )
    return SecurityMetadataMapper(mappings)


def load_account_currencies(data_home: Path) -> dict[str, str]:
    path = data_home / "config" / "account_currencies.json"
    payload = _load_json_object(path)
    if payload.get("provenance") != "USER_CONFIG":
        raise ConfigError("account_currencies.json provenance must be USER_CONFIG")
    raw_accounts = payload.get("accounts")
    if not isinstance(raw_accounts, dict):
        raise ConfigError("account_currencies.json requires an object named accounts")
    supported = {"HKD", "USD", "CNH", "JPY", "AUD", "CAD", "MYR", "SGD"}
    mappings: dict[str, str] = {}
    for raw_id, raw_currency in raw_accounts.items():
        account_id = str(raw_id)
        currency = str(raw_currency).upper()
        if not account_id.isdigit() or len(account_id) < 5:
            raise ConfigError("account_currencies.json contains an invalid private account ID")
        if currency not in supported:
            raise ConfigError("account_currencies.json contains an unsupported currency")
        mappings[account_id] = currency
    return mappings


def generate_snapshot(
    settings: Settings,
    *,
    trd_env: str = "REAL",
    base_currency: str = "USD",
    adapter: PrivateJsonReader | None = None,
    as_of: datetime | None = None,
    aliaser: AccountAliaser | None = None,
    discovery_complete: bool = False,
) -> PortfolioSnapshot:
    reader = adapter
    if reader is None:
        skill_dir = discover_futuapi_skill()
        if skill_dir is None:
            raise ConfigError("audited-compatible futuapi Skill is unavailable")
        reader = FutuAdapter(skill_dir)
    account_currencies = load_account_currencies(settings.data_home)
    try:
        private_aliaser = aliaser or AccountAliasStore(settings.data_home).load_or_create()
    except AccountAliasError as exc:
        raise ConfigError(str(exc)) from exc
    inputs = FutuPortfolioRepository(
        reader,
        aliaser=private_aliaser,
        account_currencies=account_currencies,
        discovery_complete=discovery_complete,
    ).read(trd_env=trd_env)
    if not inputs.accounts:
        raise SourceDataError(inputs.data_quality.source_errors[0])
    if inputs.data_quality.status == "UNAVAILABLE":
        raise SourceDataError("no account portfolio data was available for snapshot valuation")
    builder = PortfolioBuilder(
        base_currency=base_currency,
        converter=load_currency_converter(settings.data_home),
        classifications=load_classifications(settings.data_home),
        security_metadata=load_security_metadata(settings.data_home),
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
