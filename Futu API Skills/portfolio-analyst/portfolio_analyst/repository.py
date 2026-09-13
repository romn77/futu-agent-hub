"""Parse audited per-account Futu reads into masked portfolio inputs."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Protocol, Sequence

from .account_alias import AccountAliaser
from .child_environment import validate_account_id
from .models import Account, AccountNAV, CashBalance, DataQuality, Position
from .skill_trust import audited_files


class SourceDataError(ValueError):
    """Source facts are missing or unsafe to normalize."""


class SourceNAVError(SourceDataError):
    """An account's authoritative NAV is unavailable; reconstruction is forbidden."""


class AccountSelectionError(SourceDataError):
    """The requested account population cannot be selected safely."""


class AccountSelectionRequired(AccountSelectionError):
    """More than one eligible account requires explicit user selection."""


class PrivateJsonReader(Protocol):
    def protect_sensitive_values(self, values: Sequence[str]) -> None: ...

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
    account_navs: tuple[AccountNAV, ...]


@dataclass(frozen=True)
class _SelectedAccount:
    private_id: str
    public: Account
    query_market: str | None
    security_firm: str | None
    query_currency: str | None


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
    try:
        return validate_account_id(str(value))
    except ValueError:
        raise SourceDataError("invalid source account identifier") from None


def _optional_decimal(value: object, field: str, notes: list[str]) -> Decimal | None:
    try:
        return _decimal(value, field)
    except SourceDataError:
        notes.append(f"{field} is unavailable or invalid in source; retained as unknown")
        return None


def _source_version() -> str:
    identity = "|".join(f"{item.relative_path}:{item.sha256}" for item in audited_files())
    return f"futuapi-audited-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


class FutuPortfolioRepository:
    QUERY_MARKETS = frozenset({"US", "HK", "HKCC", "CN", "SG", "MY", "JP"})

    def __init__(
        self,
        adapter: PrivateJsonReader,
        *,
        aliaser: AccountAliaser,
        account_currencies: Mapping[str, str],
        discovery_complete: bool = False,
    ) -> None:
        self.adapter = adapter
        self.aliaser = aliaser
        self.account_currencies = {
            str(account_id): str(currency).upper()
            for account_id, currency in account_currencies.items()
        }
        self.discovery_complete = discovery_complete

    def read(
        self,
        *,
        trd_env: str = "REAL",
        timeout: float = 30,
        account_ids: Sequence[str] | None = None,
    ) -> PortfolioInputs:
        environment = trd_env.upper()
        requested: set[str] | None = None
        if account_ids is not None:
            if isinstance(account_ids, (str, bytes)) or not account_ids:
                raise AccountSelectionError("account selection must be a nonempty list")
            try:
                requested = {_account_key(value) for value in account_ids}
            except SourceDataError:
                raise AccountSelectionError(
                    "account selection contains an invalid identifier"
                ) from None
            if len(requested) != len(account_ids):
                raise AccountSelectionError("account selection contains duplicate identifiers")
        accounts_payload = self.adapter.read_private_json(
            "get_accounts", ["--json"], timeout=timeout
        )
        source_accounts = accounts_payload.get("accounts")
        if not isinstance(source_accounts, list):
            raise SourceDataError("account-list response is missing accounts")

        selected: list[_SelectedAccount] = []
        seen_ids: set[str] = set()
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
            if account_id in seen_ids:
                continue
            seen_ids.add(account_id)
            markets_raw = raw.get("trdmarket_auth", [])
            markets = (
                tuple(str(item).upper() for item in markets_raw)
                if isinstance(markets_raw, list)
                else (str(markets_raw).upper(),)
            )
            query_market = next(
                (market for market in markets if market in self.QUERY_MARKETS), None
            )
            firm = str(raw.get("security_firm", "")).upper() or None
            selected.append(
                _SelectedAccount(
                    private_id=account_id,
                    public=Account(
                        account_ref=self.aliaser.alias(account_id),
                        trd_env=environment,
                        account_type=str(raw.get("acc_type", "UNKNOWN")) or "UNKNOWN",
                        account_role=str(raw.get("acc_role", "UNKNOWN")) or "UNKNOWN",
                        markets=markets,
                        status=str(raw.get("acc_status", "UNKNOWN")) or "UNKNOWN",
                        context_type="SEC",
                        security_firm=firm or "UNKNOWN",
                        alias_provenance=self.aliaser.provenance,
                    ),
                    query_market=query_market,
                    security_firm=firm,
                    query_currency=self.account_currencies.get(account_id),
                )
            )

        self.adapter.protect_sensitive_values(tuple(account.private_id for account in selected))
        if requested is not None:
            if not requested <= {account.private_id for account in selected}:
                raise AccountSelectionError(
                    "selected account is unavailable or outside the requested securities environment"
                )
            selected = [account for account in selected if account.private_id in requested]
        elif len(selected) > 1:
            raise AccountSelectionRequired(
                "multiple eligible securities accounts; select with --acc-id "
                "(repeat only for explicitly requested aggregation)"
            )
        cash_balances: list[CashBalance] = []
        positions: list[Position] = []
        source_errors: list[str] = []
        accounts_read = 0
        account_navs: list[AccountNAV] = []
        for account in selected:
            try:
                if (
                    account.query_market is None
                    or account.security_firm is None
                    or account.query_currency is None
                ):
                    raise SourceDataError("account query metadata is incomplete")
                arguments = [
                    "--acc-id",
                    account.private_id,
                    "--market",
                    account.query_market,
                    "--trd-env",
                    environment,
                    "--currency",
                    account.query_currency,
                    "--security-firm",
                    account.security_firm,
                    "--ctx-type",
                    "SEC",
                    "--json",
                ]
                payload = self.adapter.read_private_json(
                    "get_portfolio", arguments, timeout=timeout
                )
                account_nav = self._parse_nav(payload.get("funds"), account.public.account_ref)
                account_cash: list[CashBalance] = []
                account_positions: list[Position] = []
                self._parse_cash(payload.get("funds"), account.public.account_ref, account_cash)
                self._parse_positions(
                    payload.get("positions"),
                    account.public.account_ref,
                    account.private_id,
                    account_positions,
                )
            except SourceNAVError:
                raise
            except Exception as exc:
                raise SourceDataError(
                    f"selected account portfolio is incomplete for masked account {account.public.account_ref} "
                    f"({type(exc).__name__})"
                ) from None
            cash_balances.extend(account_cash)
            positions.extend(account_positions)
            account_navs.append(account_nav)
            accounts_read += 1

        expected = len(selected)
        if expected == 0:
            status = "UNAVAILABLE"
            source_errors = [f"no {environment} securities accounts were returned"]
        elif not self.discovery_complete:
            status = "PARTIAL"
        else:
            status = "COMPLETE"
        warnings = ()
        if not self.discovery_complete:
            warnings = (
                "account discovery is UNVERIFIED because the audited source suppresses context errors",
            )
        if self.aliaser.provenance == "EPHEMERAL":
            warnings = (
                *warnings,
                "account aliases are EPHEMERAL for this non-persisting invocation",
            )
        return PortfolioInputs(
            accounts=tuple(account.public for account in selected),
            cash_balances=tuple(cash_balances),
            positions=tuple(positions),
            data_quality=DataQuality(
                status=status,
                warnings=warnings,
                source_errors=tuple(source_errors),
                coverage={
                    "accounts_returned": expected,
                    "accounts_returned_read": accounts_read,
                    "discovery_status": "COMPLETE" if self.discovery_complete else "UNVERIFIED",
                    "alias_provenance": self.aliaser.provenance,
                },
            ),
            source_version=_source_version(),
            account_navs=tuple(account_navs),
        )

    @staticmethod
    def _parse_nav(raw_funds: object, account_ref: str) -> AccountNAV:
        if not isinstance(raw_funds, Mapping) or not raw_funds:
            raise SourceNAVError("OpenD account NAV is missing")
        currency = str(raw_funds.get("currency", "")).upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise SourceNAVError("OpenD account NAV currency is unavailable")
        try:
            total_assets = _decimal(raw_funds.get("total_assets"), "total_assets")
        except SourceDataError as exc:
            raise SourceNAVError("OpenD total_assets is missing or invalid") from exc
        return AccountNAV(account_ref, currency, total_assets)

    @staticmethod
    def _parse_cash(raw_funds: object, account_ref: str, destination: list[CashBalance]) -> None:
        if not isinstance(raw_funds, Mapping) or not raw_funds:
            raise SourceDataError("portfolio funds are missing")
        currency = str(raw_funds.get("currency", "")).upper()
        notes: list[str] = []
        cash = _optional_decimal(raw_funds.get("cash"), "cash", notes)
        if currency in {"", "N/A", "NONE", "UNKNOWN"}:
            if cash:
                raise SourceDataError("nonzero cash has no audited source currency")
            return
        destination.append(
            CashBalance(account_ref, currency, cash, data_quality_notes=tuple(notes))
        )

    @staticmethod
    def _parse_positions(
        raw_positions: object,
        account_ref: str,
        private_account_id: str,
        destination: list[Position],
    ) -> None:
        if not isinstance(raw_positions, list):
            raise SourceDataError("portfolio positions are missing")
        for raw in raw_positions:
            if not isinstance(raw, Mapping):
                raise SourceDataError("portfolio response contains a malformed position")
            row_account = raw.get("acc_id")
            if row_account not in (None, 0, "", int(private_account_id), private_account_id):
                raise SourceDataError("position account identity does not match its query")
            quantity = _decimal(raw.get("qty"), "qty")
            market_value = _decimal(raw.get("market_val"), "market_val")
            position_role = str(raw.get("position_type", "")).upper()
            notes: list[str] = []
            if position_role == "LONG" and quantity < 0:
                raise SourceDataError("position direction conflicts with signed quantity")
            if position_role == "SHORT" and quantity > 0:
                quantity = -quantity
                notes.append("quantity sign normalized from source SHORT semantics")
            if quantity > 0 and market_value < 0:
                raise SourceDataError("position direction conflicts with signed market value")
            if quantity < 0 and market_value > 0:
                market_value = -market_value
                notes.append("market value sign normalized from source short semantics")
            currency = raw.get("currency")
            if (
                not isinstance(currency, str)
                or not re.fullmatch(r"[A-Z]{3}", currency)
                or currency == "NAN"
            ):
                currency = None
            destination.append(
                Position(
                    account_ref=account_ref,
                    code=str(raw.get("code", "")),
                    name=str(raw.get("name", "")),
                    security_type="UNKNOWN",
                    currency=currency,
                    currency_provenance="AUDITED_SOURCE" if currency else "UNAVAILABLE",
                    quantity=quantity,
                    can_sell_quantity=_optional_decimal(
                        raw.get("can_sell_qty"), "can_sell_qty", notes
                    ),
                    average_cost_local=_optional_decimal(
                        raw.get("average_cost"), "average_cost", notes
                    ),
                    last_price_local=_optional_decimal(
                        raw.get("nominal_price"), "nominal_price", notes
                    ),
                    market_value_local=market_value,
                    unrealized_pl_local=(
                        _optional_decimal(raw["unrealized_pl"], "unrealized_pl", notes)
                        if raw.get("unrealized_pl") is not None
                        else None
                    ),
                    realized_pl_local=(
                        _optional_decimal(raw["realized_pl"], "realized_pl", notes)
                        if raw.get("realized_pl") is not None
                        else None
                    ),
                    position_role=position_role or None,
                    data_quality_notes=tuple(notes),
                )
            )
