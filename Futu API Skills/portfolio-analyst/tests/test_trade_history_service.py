from __future__ import annotations

from copy import deepcopy
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
