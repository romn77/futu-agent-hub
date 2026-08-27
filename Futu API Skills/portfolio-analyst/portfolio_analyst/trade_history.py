"""Bounded multi-account acquisition through audited historical read commands."""

from __future__ import annotations

from datetime import date
import time
from decimal import Decimal
from typing import Any, Mapping, Protocol, Sequence

from .account_alias import AccountAliaser
from .trade_review import TradePolicy, TradeReview, TradeReviewInputError, build_trade_review


class TradeHistoryReader(Protocol):
    def protect_sensitive_values(self, values: Sequence[str]) -> None: ...

    def read_private_json(
        self, name: str, arguments: Sequence[str], *, timeout: float = 30
    ) -> dict[str, object]: ...


class FutuTradeHistoryService:
    """Read no more than twenty securities accounts and two history endpoints each."""

    MAX_ACCOUNTS = 20
    MAX_WALL_SECONDS = 60.0
    MAX_CONSECUTIVE_FAILURES = 3
    QUERY_MARKETS = frozenset({"US", "HK", "HKCC", "CN", "SG", "MY", "JP"})

    def __init__(self, adapter: TradeHistoryReader, *, aliaser: AccountAliaser) -> None:
        self.adapter = adapter
        self.aliaser = aliaser

    def review(
        self,
        *,
        start: date,
        end: date,
        trd_env: str = "REAL",
        policy: TradePolicy | None = None,
        timeout: float = 10,
        portfolio_nav: Decimal | None = None,
        source_snapshot_id: str | None = None,
    ) -> TradeReview:
        environment = trd_env.strip().upper()
        payload = self.adapter.read_private_json("get_accounts", ["--json"], timeout=timeout)
        raw_accounts = payload.get("accounts")
        if not isinstance(raw_accounts, list):
            raise TradeReviewInputError("account-list response is missing accounts")
        selected: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw in raw_accounts:
            if not isinstance(raw, Mapping):
                raise TradeReviewInputError("account-list response contains a malformed account")
            if str(raw.get("trd_env", "")).upper() != environment:
                continue
            if str(raw.get("ctx_type", "SEC")).upper() != "SEC":
                continue
            if str(raw.get("acc_status", "")).upper() == "DISABLED":
                continue
            account_id = str(raw.get("acc_id", ""))
            if not account_id.isdigit() or len(account_id) < 5:
                raise TradeReviewInputError("account-list response contains an invalid account ID")
            markets = raw.get("trdmarket_auth")
            candidates = markets if isinstance(markets, list) else [markets]
            query_markets = tuple(
                dict.fromkeys(
                    str(item).upper()
                    for item in candidates
                    if str(item).upper() in self.QUERY_MARKETS
                )
            )
            firm = str(raw.get("security_firm", "")).upper()
            if not query_markets or not firm:
                continue
            for market in query_markets:
                key = (account_id, market)
                if key not in seen:
                    selected.append((account_id, market, firm))
                    seen.add(key)
        if len(selected) > self.MAX_ACCOUNTS:
            raise TradeReviewInputError("account count exceeds the bounded history-read budget")
        self.adapter.protect_sensitive_values(tuple(dict.fromkeys(x[0] for x in selected)))
        orders: list[dict[str, Any]] = []
        deals: list[dict[str, Any]] = []
        notes: list[str] = (
            [
                "historical order rows are capped at 200 per account/context; source completeness is unverified"
            ]
            if selected
            else []
        )
        if portfolio_nav is not None:
            notes.append(
                "turnover uses one explicitly selected snapshot NAV rather than average period NAV"
            )
        deadline = time.monotonic() + self.MAX_WALL_SECONDS
        consecutive_failures = 0
        for account_id, market, firm in selected:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                notes.append("history-read work budget stopped remaining account contexts")
                break
            alias = self.aliaser.alias(account_id)
            common = [
                "--acc-id",
                account_id,
                "--market",
                market,
                "--trd-env",
                environment,
                "--start",
                start.isoformat(),
                "--end",
                end.isoformat(),
                "--security-firm",
                firm,
                "--ctx-type",
                "SEC",
            ]
            try:
                order_payload = self.adapter.read_private_json(
                    "get_history_orders",
                    [*common, "--limit", "200", "--json"],
                    timeout=max(0.01, min(timeout, remaining)),
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("history-read wall-time budget exhausted")
                deal_payload = self.adapter.read_private_json(
                    "get_history_order_fill_list",
                    [*common, "--json"],
                    timeout=max(0.01, min(timeout, remaining)),
                )
                account_orders = order_payload.get("orders")
                account_deals = deal_payload.get("deals")
                if not isinstance(account_orders, list) or not isinstance(account_deals, list):
                    raise TradeReviewInputError("historical source response is missing rows")
                raw_order_ids = {
                    str(row.get("order_id", ""))
                    for row in (*account_orders, *account_deals)
                    if isinstance(row, Mapping)
                }
                if "" in raw_order_ids:
                    raise TradeReviewInputError("historical order identity is incomplete")
                order_aliases = {
                    raw_id: f"ord-{market.lower()}-{index:06d}"
                    for index, raw_id in enumerate(sorted(raw_order_ids), 1)
                }
                orders.extend(self._orders(account_orders, alias, order_aliases))
                deals.extend(self._deals(account_deals, alias, order_aliases, market))
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                notes.append(
                    f"history read failed for masked account {alias} ({type(exc).__name__})"
                )
        failed_accounts = sum(note.startswith("history read failed") for note in notes)
        if selected and failed_accounts == len(selected):
            raise TradeReviewInputError(
                "historical reads were unavailable for every selected account"
            )
        if not selected:
            notes.append(f"no {environment} securities accounts were available")
        return build_trade_review(
            orders=orders,
            deals=deals,
            start=start,
            end=end,
            policy=policy,
            source_notes=notes,
            portfolio_nav=portfolio_nav,
            source_snapshot_id=source_snapshot_id,
        )

    @staticmethod
    def _orders(
        rows: Sequence[Any], account_ref: str, order_aliases: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        required = {
            "order_id",
            "code",
            "side",
            "status",
            "qty",
            "price",
            "dealt_qty",
            "create_time",
            "updated_time",
        }
        result = []
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != required:
                raise TradeReviewInputError("historical order contract changed")
            if not str(row["order_id"]).strip() or not str(row["code"]).strip():
                raise TradeReviewInputError("historical order identity is incomplete")
            result.append(
                {
                    **dict(row),
                    "order_id": order_aliases[str(row["order_id"])],
                    "account_ref": account_ref,
                }
            )
        return result

    @staticmethod
    def _deals(
        rows: Sequence[Any],
        account_ref: str,
        order_aliases: Mapping[str, str],
        market: str,
    ) -> list[dict[str, Any]]:
        required = {"deal_id", "order_id", "code", "qty", "price", "create_time"}
        result = []
        for row in rows:
            if not isinstance(row, Mapping) or not required <= set(row):
                raise TradeReviewInputError("historical deal contract changed")
            side = row.get("trd_side", row.get("side"))
            if side is None:
                raise TradeReviewInputError("historical deal side is missing")
            result.append(
                {
                    "deal_id": (f"fill-{account_ref[-8:]}-{market.lower()}-{len(result) + 1:06d}"),
                    "order_id": order_aliases[str(row["order_id"])],
                    "account_ref": account_ref,
                    "code": row["code"],
                    "side": side,
                    "qty": row["qty"],
                    "price": row["price"],
                    "create_time": row["create_time"],
                }
            )
        return result
