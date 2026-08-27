"""Bounded multi-account acquisition through audited historical read commands."""

from __future__ import annotations

from datetime import date, datetime, timezone
import time
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol, Sequence

from .account_alias import AccountAliaser
from .models import PortfolioSnapshot
from .skill_trust import audited_files
from .trade_review import (
    TradePolicy,
    TradeReview,
    TradeReviewInputError,
    build_trade_review,
    canonical_context_market,
    normalize_order_status,
    validate_trade_context_rows,
)


class TradeHistoryReader(Protocol):
    def protect_sensitive_values(self, values: Sequence[str]) -> None: ...

    def read_private_json(
        self, name: str, arguments: Sequence[str], *, timeout: float = 30
    ) -> dict[str, object]: ...


class HistoryBudgetLimit(TradeReviewInputError):
    """A bounded history response exceeded the documented row budget."""


class FutuTradeHistoryService:
    """Read no more than twenty securities accounts and two history endpoints each."""

    MAX_ACCOUNTS = 20
    MAX_WALL_SECONDS = 60.0
    MAX_CONSECUTIVE_FAILURES = 3
    MAX_ROWS_PER_CONTEXT = 2_000
    MAX_ROWS_TOTAL = 20_000
    QUERY_MARKETS = frozenset({"US", "HK", "HKCC", "CN", "SG", "MY", "JP"})

    def __init__(
        self,
        adapter: TradeHistoryReader,
        *,
        aliaser: AccountAliaser,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.adapter = adapter
        self.aliaser = aliaser
        self.clock = clock or (lambda: datetime.now(timezone.utc))

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
        portfolio_snapshot: PortfolioSnapshot | None = None,
    ) -> TradeReview:
        environment = trd_env.strip().upper()
        if portfolio_snapshot is not None and portfolio_snapshot.as_of.date() < end:
            raise TradeReviewInputError("turnover snapshot as-of precedes the review end")
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
                key = (account_id, canonical_context_market(market))
                if key not in seen:
                    selected.append((account_id, market, firm))
                    seen.add(key)
        selected.sort(
            key=lambda item: (item[0], canonical_context_market(item[1]), item[1], item[2])
        )
        if len(selected) > self.MAX_ACCOUNTS:
            raise TradeReviewInputError("account count exceeds the bounded history-read budget")
        self.adapter.protect_sensitive_values(tuple(dict.fromkeys(x[0] for x in selected)))
        selected_contexts = [
            (account_id, market, firm, self.aliaser.alias(account_id))
            for account_id, market, firm in selected
        ]
        requested_contexts = [
            {"account_ref": alias, "market": canonical_context_market(market)}
            for _, market, _, alias in selected_contexts
        ]
        successful_contexts: list[dict[str, str]] = []
        failed_contexts: list[dict[str, str]] = []
        omitted_contexts: list[dict[str, str]] = []
        orders: list[dict[str, Any]] = []
        deals: list[dict[str, Any]] = []
        rows_seen_total = 0
        notes: list[str] = (
            [
                "historical order rows are capped at 200 per account/context; source completeness is unverified"
            ]
            if selected
            else []
        )
        if portfolio_snapshot is not None:
            notes.append(
                "turnover uses one explicitly selected snapshot NAV rather than average period NAV"
            )
        elif portfolio_nav is not None or source_snapshot_id is not None:
            raise TradeReviewInputError(
                "base monetary analysis requires the complete immutable source snapshot"
            )
        monetary_evidence: dict[str, tuple[str, Decimal]] = {}
        if portfolio_snapshot is not None:
            for position in portfolio_snapshot.positions:
                identity = f"{position.market}.{position.symbol}".upper()
                evidence = (position.currency.upper(), position.fx_to_base)
                if identity in monetary_evidence and monetary_evidence[identity] != evidence:
                    raise TradeReviewInputError("snapshot monetary evidence conflicts by identity")
                monetary_evidence[identity] = evidence
        deadline = time.monotonic() + self.MAX_WALL_SECONDS
        consecutive_failures = 0
        contexts_read = 0
        for index, (account_id, market, firm, alias) in enumerate(selected_contexts):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                notes.append("history-read work budget stopped remaining account contexts")
                omitted_contexts.extend(requested_contexts[index:])
                break
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
                rows_seen_total += len(account_orders) + len(account_deals)
                if (
                    len(account_orders) > self.MAX_ROWS_PER_CONTEXT
                    or len(account_deals) > self.MAX_ROWS_PER_CONTEXT
                    or rows_seen_total > self.MAX_ROWS_TOTAL
                ):
                    raise HistoryBudgetLimit("historical row budget exceeded")
                raw_order_rows = [str(row.get("order_id", "")) for row in account_orders]
                raw_deal_rows = [str(row.get("deal_id", "")) for row in account_deals]
                if (
                    any(not value.strip() for value in (*raw_order_rows, *raw_deal_rows))
                    or len(raw_order_rows) != len(set(raw_order_rows))
                    or len(raw_deal_rows) != len(set(raw_deal_rows))
                ):
                    raise TradeReviewInputError(
                        "historical raw identities are missing or duplicated"
                    )
                raw_order_ids = {
                    str(row.get("order_id", ""))
                    for row in (*account_orders, *account_deals)
                    if isinstance(row, Mapping)
                }
                if "" in raw_order_ids:
                    raise TradeReviewInputError("historical order identity is incomplete")
                order_aliases = {
                    raw_id: "ord-"
                    + self.aliaser.scoped_digest(
                        f"order:{account_id}:{canonical_context_market(market)}", raw_id
                    )
                    for raw_id in raw_order_ids
                }
                deal_aliases = {
                    raw_id: "fill-"
                    + self.aliaser.scoped_digest(
                        f"deal:{account_id}:{canonical_context_market(market)}", raw_id
                    )
                    for raw_id in raw_deal_rows
                }
                normalized_orders = self._orders(
                    account_orders,
                    alias,
                    order_aliases,
                    canonical_context_market(market),
                    monetary_evidence,
                )
                normalized_deals = self._deals(
                    account_deals,
                    alias,
                    order_aliases,
                    deal_aliases,
                    canonical_context_market(market),
                    monetary_evidence,
                )
                validate_trade_context_rows(
                    orders=normalized_orders,
                    deals=normalized_deals,
                    start=start,
                    end=end,
                    account_ref=alias,
                    queried_market=canonical_context_market(market),
                )
                # A context becomes report evidence only after both source bodies
                # normalize successfully; failed contexts cannot leak half a read.
                orders.extend(normalized_orders)
                deals.extend(normalized_deals)
                contexts_read += 1
                successful_contexts.append(
                    {"account_ref": alias, "market": canonical_context_market(market)}
                )
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                reason = (
                    "BUDGET_LIMIT" if isinstance(exc, HistoryBudgetLimit) else type(exc).__name__
                )
                failed_contexts.append(
                    {
                        "account_ref": alias,
                        "market": canonical_context_market(market),
                        "reason": reason,
                    }
                )
                notes.append(f"history read failed for masked account {alias} ({reason})")
        if not selected:
            notes.append(f"no {environment} securities accounts were available")
        return build_trade_review(
            orders=orders,
            deals=deals,
            start=start,
            end=end,
            policy=policy,
            source_notes=notes,
            base_currency=portfolio_snapshot.base_currency if portfolio_snapshot else None,
            portfolio_nav_base=portfolio_snapshot.nav_base if portfolio_snapshot else None,
            source_snapshot_id=(portfolio_snapshot.snapshot_id if portfolio_snapshot else None),
            acquired_at=self.clock(),
            source_snapshot_as_of=(portfolio_snapshot.as_of if portfolio_snapshot else None),
            coverage_context={
                "accounts_requested": len({item["account_ref"] for item in requested_contexts}),
                "accounts_covered": len({item["account_ref"] for item in successful_contexts}),
                "contexts_requested": len(selected),
                "contexts_read": contexts_read,
                "contexts_failed": len(failed_contexts),
                "contexts_omitted_budget": len(omitted_contexts),
                "requested_contexts": requested_contexts,
                "successful_contexts": successful_contexts,
                "failed_contexts": failed_contexts,
                "omitted_contexts": omitted_contexts,
            },
            corporate_action_evidence_status="UNSUPPORTED_EVIDENCE",
            provenance_context={
                "audited_source_version": "futuapi-content-manifest-v1",
                "audited_source_digests": {
                    str(record.relative_path): record.sha256
                    for record in audited_files()
                    if record.relative_path.name
                    in {
                        "common.py",
                        "get_history_orders.py",
                        "get_history_order_fill_list.py",
                    }
                },
            },
        )

    @staticmethod
    def _orders(
        rows: Sequence[Any],
        account_ref: str,
        order_aliases: Mapping[str, str],
        queried_market: str,
        monetary_evidence: Mapping[str, tuple[str, Decimal]],
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
            if canonical_context_market(str(row["code"]).split(".", 1)[0]) != queried_market:
                raise TradeReviewInputError("historical order market conflicts with query context")
            currency, _ = monetary_evidence.get(
                str(row["code"]).strip().upper(), ("UNKNOWN", Decimal(1))
            )
            result.append(
                {
                    **dict(row),
                    "order_id": order_aliases[str(row["order_id"])],
                    "account_ref": account_ref,
                    "currency": currency,
                    "status": normalize_order_status(row["status"]),
                }
            )
        return result

    @staticmethod
    def _deals(
        rows: Sequence[Any],
        account_ref: str,
        order_aliases: Mapping[str, str],
        deal_aliases: Mapping[str, str],
        market: str,
        monetary_evidence: Mapping[str, tuple[str, Decimal]],
    ) -> list[dict[str, Any]]:
        required = {"deal_id", "order_id", "code", "qty", "price", "create_time"}
        result = []
        for row in rows:
            if not isinstance(row, Mapping) or not required <= set(row):
                raise TradeReviewInputError("historical deal contract changed")
            raw_deal_id = str(row["deal_id"]).strip()
            raw_order_id = str(row["order_id"]).strip()
            if not raw_deal_id or not raw_order_id or raw_order_id not in order_aliases:
                raise TradeReviewInputError("historical deal identity is incomplete or orphaned")
            if canonical_context_market(str(row["code"]).split(".", 1)[0]) != market:
                raise TradeReviewInputError("historical deal market conflicts with query context")
            side = row.get("trd_side", row.get("side"))
            if side is None:
                raise TradeReviewInputError("historical deal side is missing")
            event_type = str(row.get("event_type", "TRADE")).strip().upper()
            if event_type not in {
                "TRADE",
                "CORPORATE_ACTION",
                "TRANSFER",
                "COST_ADJUSTMENT",
            }:
                raise TradeReviewInputError("historical deal event type is unsupported")
            currency, fx_to_base = monetary_evidence.get(
                str(row["code"]).strip().upper(), ("UNKNOWN", None)
            )
            result.append(
                {
                    "deal_id": deal_aliases[raw_deal_id],
                    "order_id": order_aliases[raw_order_id],
                    "account_ref": account_ref,
                    "code": row["code"],
                    "side": side,
                    "qty": row["qty"],
                    "price": row["price"],
                    "create_time": row["create_time"],
                    "currency": currency,
                    "fx_to_base": fx_to_base,
                    "event_type": event_type,
                }
            )
        return result
