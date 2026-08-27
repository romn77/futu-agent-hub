from __future__ import annotations

from datetime import date

from portfolio_analyst.account_alias import AccountAliaser
from portfolio_analyst.trade_history import FutuTradeHistoryService


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
