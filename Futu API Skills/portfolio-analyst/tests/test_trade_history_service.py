from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json

import pytest

from portfolio_analyst.account_alias import AccountAliaser
from portfolio_analyst.schema import validate_trade_review_document
from portfolio_analyst.trade_history import FutuTradeHistoryService
from test_risk_analysis import snapshot


class FixtureAdapter:
    def __init__(self):
        self.calls = []
        self.protected = ()

    def protect_sensitive_values(self, values):
        self.protected = tuple(values)

    def read_private_json(self, name, arguments, *, timeout=30):
        self.calls.append((name, tuple(arguments), timeout))
        if name == "get_accounts":
            return {
                "accounts": [
                    {
                        "acc_id": 12345,
                        "trd_env": "REAL",
                        "ctx_type": "SEC",
                        "acc_status": "ACTIVE",
                        "trdmarket_auth": ["US"],
                        "security_firm": "FUTUSECURITIES",
                    },
                    {
                        "acc_id": 67890,
                        "trd_env": "REAL",
                        "ctx_type": "SEC",
                        "acc_status": "ACTIVE",
                        "trdmarket_auth": ["US"],
                        "security_firm": "FUTUSECURITIES",
                    },
                ]
            }
        account = arguments[arguments.index("--acc-id") + 1]
        if name == "get_history_orders":
            return {
                "orders": [
                    {
                        "order_id": f"o-{account}",
                        "code": "US.AAA",
                        "side": "BUY",
                        "status": "FILLED_ALL",
                        "qty": 1,
                        "price": 10,
                        "dealt_qty": 1,
                        "create_time": "2026-08-03 10:00:00",
                        "updated_time": "2026-08-03 10:00:01",
                    }
                ]
            }
        return {
            "market": "US",
            "deals": [
                {
                    "deal_id": f"d-{account}",
                    "order_id": f"o-{account}",
                    "code": "US.AAA",
                    "trd_side": "BUY",
                    "qty": 1,
                    "price": 10,
                    "create_time": "2026-08-03 10:00:01",
                }
            ],
        }


def test_trade_history_service_is_bounded_multi_account_and_masks_private_ids():
    adapter = FixtureAdapter()
    review = FutuTradeHistoryService(adapter, aliaser=AccountAliaser.ephemeral()).review(
        start=date(2026, 8, 1), end=date(2026, 8, 27), trd_env="REAL"
    )
    assert review.metrics.fill_count == 2
    assert len({lot.account_ref for lot in review.open_lots}) == 2
    assert adapter.protected == ("12345", "67890")
    assert all(
        "12345" not in str(item) and "67890" not in str(item) for item in review.to_dict().values()
    )
    assert [call[0] for call in adapter.calls] == [
        "get_accounts",
        "get_history_orders",
        "get_history_order_fill_list",
        "get_history_orders",
        "get_history_order_fill_list",
    ]


class EmptyFixtureAdapter(FixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        if name == "get_accounts":
            return super().read_private_json(name, arguments, timeout=timeout)
        self.calls.append((name, tuple(arguments), timeout))
        return {"orders": []} if name == "get_history_orders" else {"deals": []}


class EmptyHongKongAdapter(EmptyFixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        payload = super().read_private_json(name, arguments, timeout=timeout)
        if name == "get_accounts":
            payload["accounts"] = payload["accounts"][:1]
            payload["accounts"][0]["trdmarket_auth"] = ["HK"]
        return payload


def test_turnover_snapshot_date_is_compared_in_requested_exchange_timezone():
    source = replace(snapshot(), as_of=datetime(2026, 8, 27, 16, 30, tzinfo=timezone.utc))
    review = FutuTradeHistoryService(
        EmptyHongKongAdapter(),
        aliaser=AccountAliaser.ephemeral(),
        clock=lambda: datetime(2026, 8, 28, 1, tzinfo=timezone.utc),
    ).review(start=date(2026, 8, 1), end=date(2026, 8, 28), portfolio_snapshot=source)

    assert review.coverage["successful_contexts"][0]["market"] == "HK"
    assert review.data_quality_status == "PARTIAL"
    assert "CURRENT_SESSION_INCOMPLETE" in {item["code"] for item in review.quality_issues}


class NoEligibleAccountAdapter(FixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        if name == "get_accounts":
            self.calls.append((name, tuple(arguments), timeout))
            return {"accounts": []}
        raise AssertionError("history reads must not run without an eligible account")


class InvalidIdentityContextAdapter(FixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        payload = super().read_private_json(name, arguments, timeout=timeout)
        if name == "get_accounts":
            payload["accounts"] = payload["accounts"][:1]
        elif name == "get_history_orders":
            payload["orders"][0]["code"] = "US.BAD SYMBOL"
        elif name == "get_history_order_fill_list":
            payload["deals"][0]["code"] = "US.BAD SYMBOL"
        return payload


def test_invalid_canonical_identity_fails_only_its_context_atomically():
    review = FutuTradeHistoryService(
        InvalidIdentityContextAdapter(), aliaser=AccountAliaser.ephemeral()
    ).review(start=date(2026, 8, 1), end=date(2026, 8, 27))

    assert review.coverage["contexts_read"] == 0
    assert review.coverage["contexts_failed"] == 1
    assert review.normalized_orders == ()
    assert review.normalized_fills == ()
    assert review.data_quality_status == "UNAVAILABLE"


def test_zero_requested_contexts_are_unavailable_and_cannot_claim_zero_activity():
    review = FutuTradeHistoryService(
        NoEligibleAccountAdapter(),
        aliaser=AccountAliaser.ephemeral(),
        clock=lambda: datetime(2026, 8, 27, 8, 9, 10, tzinfo=timezone.utc),
    ).review(start=date(2026, 8, 1), end=date(2026, 8, 27), trd_env="REAL")

    assert review.coverage["contexts_requested"] == 0
    assert review.coverage["successful_contexts"] == []
    assert review.data_quality_status == "UNAVAILABLE"
    assert all(value is None for value in vars(review.metrics).values())
    assert review.behavior_findings == ()
    validate_trade_review_document(review.to_dict())

    false_zero = deepcopy(review.to_dict())
    false_zero["metrics"]["fill_count"] = 0
    _rehash_trade_review(false_zero)
    with pytest.raises(Exception):
        validate_trade_review_document(false_zero)


def test_service_clock_and_empty_successful_contexts_are_preserved_without_raw_ids():
    acquired = datetime(2026, 8, 27, 8, 9, 10, tzinfo=timezone.utc)
    adapter = EmptyFixtureAdapter()
    review = FutuTradeHistoryService(
        adapter, aliaser=AccountAliaser.ephemeral(), clock=lambda: acquired
    ).review(start=date(2026, 8, 1), end=date(2026, 8, 27), trd_env="REAL")

    assert review.as_of == acquired
    assert review.source_snapshot_as_of is None
    assert review.coverage["accounts_requested"] == 2
    assert review.coverage["accounts_covered"] == 2
    assert review.coverage["contexts_requested"] == 2
    assert review.coverage["contexts_read"] == 2
    assert review.coverage["contexts_failed"] == 0
    assert review.coverage["contexts_omitted_budget"] == 0
    assert len(review.coverage["successful_contexts"]) == 2
    assert all(
        "12345" not in str(value) and "67890" not in str(value)
        for value in review.coverage.values()
    )


class FirstContextFailureAdapter(EmptyFixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        if name == "get_history_orders":
            raise RuntimeError("fixture failure")
        return super().read_private_json(name, arguments, timeout=timeout)


def test_failed_and_budget_omitted_contexts_are_separate_masked_coverage():
    adapter = FirstContextFailureAdapter()
    service = FutuTradeHistoryService(
        adapter,
        aliaser=AccountAliaser.ephemeral(),
        clock=lambda: datetime(2026, 8, 27, 8, 9, 10, tzinfo=timezone.utc),
    )
    service.MAX_CONSECUTIVE_FAILURES = 1
    review = service.review(start=date(2026, 8, 1), end=date(2026, 8, 27))

    assert review.coverage["contexts_failed"] == 1
    assert review.coverage["contexts_omitted_budget"] == 1
    assert review.coverage["contexts_read"] == 0
    assert review.coverage["accounts_covered"] == 0
    assert review.data_quality_status == "UNAVAILABLE"
    assert {item["code"] for item in review.quality_issues} >= {
        "CONTEXT_FAILED",
        "CONTEXT_OMITTED_BUDGET",
    }
    assert all(value is None for value in vars(review.metrics).values())
    assert "Orders / fills / symbols: Unavailable / Unavailable / Unavailable" in (
        review.to_markdown()
    )
    assert "Orders: Unavailable" in review.to_human()
    validate_trade_review_document(review.to_dict())
    false_zero = deepcopy(review.to_dict())
    false_zero["metrics"]["trade_count"] = 0
    _rehash_trade_review(false_zero)
    with pytest.raises(Exception):
        validate_trade_review_document(false_zero)
    assert all(
        "12345" not in str(value) and "67890" not in str(value)
        for value in review.coverage.values()
    )


class UnknownStatusAdapter(FixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        payload = super().read_private_json(name, arguments, timeout=timeout)
        if name == "get_history_orders" and arguments[arguments.index("--acc-id") + 1] == "12345":
            payload["orders"][0]["status"] = "FUTURE_UNKNOWN_STATUS"
        return payload


def test_unknown_order_status_fails_that_context_closed_and_marks_report_partial():
    review = FutuTradeHistoryService(
        UnknownStatusAdapter(),
        aliaser=AccountAliaser.ephemeral(),
        clock=lambda: datetime(2026, 8, 27, 8, 9, 10, tzinfo=timezone.utc),
    ).review(start=date(2026, 8, 1), end=date(2026, 8, 27))

    assert review.coverage["contexts_failed"] == 1
    assert review.coverage["contexts_read"] == 1
    assert review.data_quality_status == "PARTIAL"


def test_service_keeps_acquisition_clock_separate_from_source_snapshot_as_of():
    source = snapshot()
    acquired = datetime(2026, 8, 28, 1, 2, 3, tzinfo=timezone.utc)
    review = FutuTradeHistoryService(
        FixtureAdapter(), aliaser=AccountAliaser.ephemeral(), clock=lambda: acquired
    ).review(
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        portfolio_snapshot=source,
    )

    assert review.as_of == acquired
    assert review.source_snapshot_as_of == source.as_of
    assert review.as_of != review.source_snapshot_as_of


class MalformedDealsAdapter(FixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        payload = super().read_private_json(name, arguments, timeout=timeout)
        if (
            name == "get_history_order_fill_list"
            and arguments[arguments.index("--acc-id") + 1] == "12345"
        ):
            payload["deals"][0].pop("qty")
        return payload


def test_failed_context_does_not_leak_partially_normalized_orders_into_report():
    review = FutuTradeHistoryService(
        MalformedDealsAdapter(),
        aliaser=AccountAliaser.ephemeral(),
        clock=lambda: datetime(2026, 8, 27, 8, 9, 10, tzinfo=timezone.utc),
    ).review(start=date(2026, 8, 1), end=date(2026, 8, 27))

    assert review.coverage["contexts_failed"] == 1
    assert review.coverage["contexts_read"] == 1
    assert len(review.normalized_orders) == 1
    assert len(review.normalized_fills) == 1
    assert (
        review.normalized_orders[0].account_ref
        == review.coverage["successful_contexts"][0]["account_ref"]
    )
    validate_trade_review_document(review.to_dict())


class RecognizedTransferAdapter(FixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        payload = super().read_private_json(name, arguments, timeout=timeout)
        if name == "get_history_orders":
            payload["orders"][0]["status"] = "CANCELLED_ALL"
            payload["orders"][0]["dealt_qty"] = 0
        if name == "get_history_order_fill_list":
            payload["deals"][0]["event_type"] = "TRANSFER"
        return payload


def test_recognized_nontrade_event_is_explicit_and_excluded_from_fifo():
    review = FutuTradeHistoryService(
        RecognizedTransferAdapter(),
        aliaser=AccountAliaser.from_key(b"stable-test-key-material-32bytes!!"),
        clock=lambda: datetime(2026, 8, 28, 8, 9, 10, tzinfo=timezone.utc),
    ).review(start=date(2026, 8, 1), end=date(2026, 8, 27))

    assert {fill.event_type for fill in review.normalized_fills} == {"TRANSFER"}
    assert review.metrics.fill_count == 0
    assert review.coverage["excluded_nontrade_events"] == 2
    assert review.coverage["corporate_action_evidence_status"] == "UNSUPPORTED_EVIDENCE"


class HkccFixtureAdapter(FixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        if name == "get_accounts":
            self.calls.append((name, tuple(arguments), timeout))
            return {
                "accounts": [
                    {
                        "acc_id": 12345,
                        "trd_env": "REAL",
                        "ctx_type": "SEC",
                        "acc_status": "ACTIVE",
                        "trdmarket_auth": ["HKCC"],
                        "security_firm": "FUTUSECURITIES",
                    }
                ]
            }
        payload = super().read_private_json(name, arguments, timeout=timeout)
        rows = payload["orders"] if name == "get_history_orders" else payload["deals"]
        rows[0]["code"] = "HKCC.00001"
        if name == "get_history_order_fill_list":
            payload["market"] = "HKCC"
        return payload


def _rehash_trade_review(payload):
    body = dict(payload)
    body.pop("review_id")
    payload["review_id"] = (
        "tr_"
        + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
    )


def test_hkcc_source_context_uses_hk_canonical_coverage_and_evidence_key():
    review = FutuTradeHistoryService(
        HkccFixtureAdapter(),
        aliaser=AccountAliaser.ephemeral(),
        clock=lambda: datetime(2026, 8, 27, 8, 9, 10, tzinfo=timezone.utc),
    ).review(start=date(2026, 8, 1), end=date(2026, 8, 27))

    assert review.coverage["requested_contexts"][0]["market"] == "HK"
    assert review.coverage["successful_contexts"][0]["market"] == "HK"
    assert review.normalized_orders[0].market == "HK"
    assert review.normalized_fills[0].market == "HK"
    validate_trade_review_document(review.to_dict())

    conflicting = deepcopy(review.to_dict())
    conflicting["coverage"]["requested_contexts"][0]["market"] = "HKCC"
    conflicting["coverage"]["successful_contexts"][0]["market"] = "HKCC"
    _rehash_trade_review(conflicting)
    with pytest.raises(Exception):
        validate_trade_review_document(conflicting)


class ReorderedRowsAdapter(FixtureAdapter):
    def __init__(
        self,
        *,
        reverse=False,
        reverse_accounts=False,
        duplicate_order=False,
        duplicate_deal=False,
        wrong_market=False,
    ):
        super().__init__()
        self.reverse = reverse
        self.reverse_accounts = reverse_accounts
        self.duplicate_order = duplicate_order
        self.duplicate_deal = duplicate_deal
        self.wrong_market = wrong_market

    def read_private_json(self, name, arguments, *, timeout=30):
        payload = super().read_private_json(name, arguments, timeout=timeout)
        if name == "get_accounts" and self.reverse_accounts:
            payload["accounts"].reverse()
        elif name == "get_history_orders":
            second = dict(payload["orders"][0])
            second["order_id"] += "-2"
            second["create_time"] = "2026-08-04 10:00:00"
            second["updated_time"] = "2026-08-04 10:00:01"
            payload["orders"].append(second)
            if self.duplicate_order:
                payload["orders"][1]["order_id"] = payload["orders"][0]["order_id"]
        elif name == "get_history_order_fill_list":
            second = dict(payload["deals"][0])
            second["deal_id"] += "-2"
            second["order_id"] += "-2"
            second["create_time"] = "2026-08-04 10:00:01"
            payload["deals"].append(second)
            if self.duplicate_deal:
                payload["deals"][1]["deal_id"] = payload["deals"][0]["deal_id"]
            if self.wrong_market:
                payload["deals"][0]["code"] = "HK.00001"
        rows = payload.get("orders", payload.get("deals"))
        if self.reverse and rows:
            rows.reverse()
        return payload


def test_raw_id_aliases_and_report_identity_are_stable_across_upstream_row_order():
    acquired = datetime(2026, 8, 28, 8, 9, 10, tzinfo=timezone.utc)
    key = b"stable-test-key-material-32bytes!!"
    first = FutuTradeHistoryService(
        ReorderedRowsAdapter(),
        aliaser=AccountAliaser.from_key(key),
        clock=lambda: acquired,
    ).review(start=date(2026, 8, 1), end=date(2026, 8, 27))
    second = FutuTradeHistoryService(
        ReorderedRowsAdapter(reverse=True, reverse_accounts=True),
        aliaser=AccountAliaser.from_key(key),
        clock=lambda: acquired,
    ).review(start=date(2026, 8, 1), end=date(2026, 8, 27))
    assert first.review_id == second.review_id
    assert first.to_dict() == second.to_dict()


@pytest.mark.parametrize(
    "kwargs",
    [{"duplicate_order": True}, {"duplicate_deal": True}, {"wrong_market": True}],
)
def test_duplicate_raw_identity_or_cross_market_body_fails_context_atomically(kwargs):
    review = FutuTradeHistoryService(
        ReorderedRowsAdapter(**kwargs),
        aliaser=AccountAliaser.from_key(b"stable-test-key-material-32bytes!!"),
        clock=lambda: datetime(2026, 8, 28, 8, 9, 10, tzinfo=timezone.utc),
    ).review(start=date(2026, 8, 1), end=date(2026, 8, 27))
    assert review.coverage["contexts_failed"] == 2
    assert review.data_quality_status == "UNAVAILABLE"
    assert review.normalized_orders == ()
    assert review.normalized_fills == ()


class FailedRowsStillConsumeBudgetAdapter(FixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        payload = super().read_private_json(name, arguments, timeout=timeout)
        account = arguments[arguments.index("--acc-id") + 1] if "--acc-id" in arguments else None
        if name == "get_history_order_fill_list" and account == "12345":
            payload["deals"][0].pop("qty")
        return payload


def test_total_row_budget_counts_failed_context_bodies_and_stops_later_evidence():
    service = FutuTradeHistoryService(
        FailedRowsStillConsumeBudgetAdapter(),
        aliaser=AccountAliaser.from_key(b"stable-test-key-material-32bytes!!"),
        clock=lambda: datetime(2026, 8, 28, 8, 9, 10, tzinfo=timezone.utc),
    )
    service.MAX_ROWS_TOTAL = 3

    review = service.review(start=date(2026, 8, 1), end=date(2026, 8, 27))

    assert review.coverage["contexts_failed"] == 2
    assert review.coverage["contexts_read"] == 0
    assert review.coverage["failed_contexts"][1]["reason"] == "BUDGET_LIMIT"
    assert review.data_quality_status == "UNAVAILABLE"
