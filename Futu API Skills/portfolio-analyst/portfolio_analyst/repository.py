"""Parse audited Futu reads into masked, currency-specific portfolio inputs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Protocol, Sequence

from .models import Account, CashBalance, DataQuality, Position
from .skill_trust import audited_files


class SourceDataError(ValueError):
    """Source facts are missing or unsafe to normalize."""


class PrivateJsonReader(Protocol):
    def read_private_json(
        self, name: str, arguments: Sequence[str], *, timeout: float = 30
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class PortfolioInputs:
    accounts: tuple[Account, ...]
    cash_balances: tuple[CashBalance, ...]
    positions: tuple[Position, ...]
    data_quality: DataQuality
    source_version: str


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise SourceDataError(f"invalid numeric source field: {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SourceDataError(f"invalid numeric source field: {field}") from exc
    if not parsed.is_finite():
        raise SourceDataError(f"non-finite numeric source field: {field}")
    return parsed


def _account_key(value: object) -> str:
    text = str(value)
    if not text.isdigit() or len(text) < 5:
        raise SourceDataError("invalid source account identifier")
    return text


def _masked_ref(account_id: str) -> str:
    digest = hashlib.sha256(f"portfolio-account:{account_id}".encode("ascii")).hexdigest()[:8]
    return f"acct-{digest}-***{account_id[-4:]}"


def _source_version() -> str:
    identity = "|".join(f"{item.relative_path}:{item.sha256}" for item in audited_files())
    return f"futuapi-audited-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


class FutuPortfolioRepository:
    CASH_FIELDS = {"us_cash": "USD", "hk_cash": "HKD", "cn_cash": "CNH", "ca_cash": "CAD"}

    def __init__(self, adapter: PrivateJsonReader) -> None:
        self.adapter = adapter

    def read(self, *, trd_env: str = "REAL", timeout: float = 30) -> PortfolioInputs:
        environment = trd_env.upper()
        accounts_payload = self.adapter.read_private_json(
            "get_accounts", ["--json"], timeout=timeout
        )
        source_accounts = accounts_payload.get("accounts")
        if not isinstance(source_accounts, list):
            raise SourceDataError("account-list response is missing accounts")

        selected: dict[str, tuple[Account, Mapping[str, object]]] = {}
        for raw in source_accounts:
            if not isinstance(raw, Mapping):
                raise SourceDataError("account-list response contains a malformed account")
            if str(raw.get("trd_env", "")).upper() != environment:
                continue
            if str(raw.get("ctx_type", "SEC")).upper() != "SEC":
                continue
            if str(raw.get("acc_status", "")).upper() == "DISABLED":
                continue
            account_id = _account_key(raw.get("acc_id"))
            markets_raw = raw.get("trdmarket_auth", [])
            markets = (
                tuple(str(item).upper() for item in markets_raw)
                if isinstance(markets_raw, list)
                else (str(markets_raw).upper(),)
            )
            account = Account(
                account_ref=_masked_ref(account_id),
                trd_env=environment,
                account_type=str(raw.get("acc_type", "UNKNOWN")) or "UNKNOWN",
                account_role=str(raw.get("acc_role", "UNKNOWN")) or "UNKNOWN",
                markets=markets,
                status=str(raw.get("acc_status", "UNKNOWN")) or "UNKNOWN",
                context_type="SEC",
                security_firm=str(raw.get("security_firm", "UNKNOWN")) or "UNKNOWN",
            )
            selected[account_id] = (account, raw)

        portfolios_payload = self.adapter.read_private_json(
            "get_all_portfolios", ["--trd-env", environment, "--json"], timeout=timeout
        )
        source_portfolios = portfolios_payload.get("accounts")
        if not isinstance(source_portfolios, list):
            raise SourceDataError("portfolio response is missing accounts")

        cash_balances: list[CashBalance] = []
        positions: list[Position] = []
        read_ids: set[str] = set()
        for raw in source_portfolios:
            if not isinstance(raw, Mapping):
                raise SourceDataError("portfolio response contains a malformed account")
            account_id = _account_key(raw.get("acc_id"))
            selected_record = selected.get(account_id)
            if selected_record is None:
                continue
            account, _account_source = selected_record
            read_ids.add(account_id)
            self._parse_cash(raw.get("funds"), account.account_ref, cash_balances)
            self._parse_positions(raw.get("positions"), account.account_ref, positions)

        expected = len(selected)
        read = len(read_ids)
        missing = sorted(set(selected) - read_ids)
        source_errors = (
            (f"portfolio data unavailable for {len(missing)} masked account(s)",) if missing else ()
        )
        if expected == 0:
            status = "UNAVAILABLE"
            source_errors = (f"no {environment} securities accounts were returned",)
        elif read < expected:
            status = "PARTIAL"
        else:
            status = "COMPLETE"
        return PortfolioInputs(
            accounts=tuple(selected[account_id][0] for account_id in selected),
            cash_balances=tuple(cash_balances),
            positions=tuple(positions),
            data_quality=DataQuality(
                status=status,
                source_errors=source_errors,
                coverage={"accounts_expected": expected, "accounts_read": read},
            ),
            source_version=_source_version(),
        )

    def _parse_cash(
        self, raw_funds: object, account_ref: str, destination: list[CashBalance]
    ) -> None:
        if not isinstance(raw_funds, Mapping) or not raw_funds:
            raise SourceDataError("portfolio funds are missing")
        explicit_currency = str(raw_funds.get("currency", "")).upper()
        if explicit_currency and explicit_currency not in {"N/A", "NONE", "UNKNOWN"}:
            destination.append(
                CashBalance(
                    account_ref, explicit_currency, _decimal(raw_funds.get("cash", 0), "cash")
                )
            )
            return
        found_component = False
        for field, currency in self.CASH_FIELDS.items():
            if field not in raw_funds:
                continue
            amount = _decimal(raw_funds[field], field)
            if amount:
                destination.append(CashBalance(account_ref, currency, amount))
                found_component = True
        generic_cash = _decimal(raw_funds.get("cash", 0), "cash")
        if generic_cash and not found_component:
            raise SourceDataError("nonzero cash has no source currency; aggregation is unsafe")

    @staticmethod
    def _parse_positions(
        raw_positions: object, account_ref: str, destination: list[Position]
    ) -> None:
        if not isinstance(raw_positions, list):
            raise SourceDataError("portfolio positions are missing")
        for raw in raw_positions:
            if not isinstance(raw, Mapping):
                raise SourceDataError("portfolio response contains a malformed position")
            quantity = _decimal(raw.get("qty"), "qty")
            market_value = _decimal(raw.get("market_val"), "market_val")
            position_role = str(raw.get("position_type", "")).upper()
            notes: tuple[str, ...] = ()
            if (quantity < 0 or position_role == "SHORT") and market_value > 0:
                market_value = -market_value
                notes = ("market value sign normalized from source short semantics",)
            destination.append(
                Position(
                    account_ref=account_ref,
                    code=str(raw.get("code", "")),
                    name=str(raw.get("name", "")),
                    security_type=str(raw.get("security_type", "UNKNOWN")) or "UNKNOWN",
                    quantity=quantity,
                    can_sell_quantity=_decimal(raw.get("can_sell_qty", 0), "can_sell_qty"),
                    average_cost_local=_decimal(raw.get("average_cost", 0), "average_cost"),
                    last_price_local=_decimal(raw.get("nominal_price", 0), "nominal_price"),
                    market_value_local=market_value,
                    unrealized_pl_local=(
                        _decimal(raw["unrealized_pl"], "unrealized_pl")
                        if raw.get("unrealized_pl") is not None
                        else None
                    ),
                    realized_pl_local=(
                        _decimal(raw["realized_pl"], "realized_pl")
                        if raw.get("realized_pl") is not None
                        else None
                    ),
                    position_role=position_role or None,
                    data_quality_notes=notes,
                )
            )
