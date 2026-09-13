from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from portfolio_analyst.service import generate_snapshot
from portfolio_analyst.settings import Settings
from portfolio_analyst.snapshot import UnsafeAggregationError
from test_source_field_fidelity import _adapter_with_sdk_rows
from test_snapshot_service_cli import _configured_home, FixtureAdapter, ACCOUNT_IDS


@pytest.mark.parametrize("currency", ["USD", "HKD", "CNH", None])
def test_position_currency_survives_unchanged_pinned_reader(monkeypatch, currency):
    adapter, calls = _adapter_with_sdk_rows(
        monkeypatch,
        {"currency": "USD", "total_assets": 1000, "cash": 100},
        [{"code": "US.TEST", "qty": 2, "market_val": 100, "currency": currency}],
    )
    body = adapter.read_private_json("get_portfolio", ["--acc-id", "90000001", "--json"])
    assert body["positions"][0]["currency"] == currency
    assert [name for name, _ in calls] == ["accinfo_query", "position_list_query", "close"]


class CurrencyAdapter(FixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        body = super().read_private_json(name, arguments, timeout=timeout)
        if name == "get_portfolio":
            body["positions"] = [body["positions"][0]]
            body["positions"][0]["currency"] = "USD"
        return body


def _source(tmp_path, *, metadata_provider=None, configured=None):
    home = _configured_home(tmp_path)
    path = home / "config/security_metadata.json"
    if configured is None:
        path.unlink()
    else:
        path.write_text(json.dumps(configured))
    return generate_snapshot(
        Settings(home),
        adapter=CurrencyAdapter(),
        account_ids=(ACCOUNT_IDS[0],),
        as_of=datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
        security_type_provider=metadata_provider,
    )


class MetadataProvider:
    def metadata(self, identities, *, timeout):
        assert tuple(identities) == ("US.AAPL",)
        assert timeout <= 5
        return {"data": [{"code": "US.AAPL", "stock_type": "STOCK"}]}


def test_authoritative_currency_and_type_need_no_per_symbol_config(tmp_path):
    source = _source(tmp_path, metadata_provider=MetadataProvider())
    assert source.positions[0].currency == "USD"
    assert source.positions[0].currency_provenance == "AUDITED_SOURCE"
    assert source.positions[0].security_type == "STOCK"
    assert source.nav_base == Decimal("1000")


def test_missing_type_is_local_when_source_currency_is_known(tmp_path):
    source = _source(tmp_path)
    assert source.positions[0].currency == "USD"
    assert source.positions[0].security_type == "UNKNOWN"
    assert "security_type" in source.data_quality.missing_fields


def test_unknown_config_type_does_not_erase_opend_type(tmp_path):
    source = _source(
        tmp_path,
        metadata_provider=MetadataProvider(),
        configured={
            "provenance": "USER_CONFIG",
            "securities": {"US.AAPL": {"currency": "USD", "security_type": "UNKNOWN"}},
        },
    )
    assert source.positions[0].security_type == "STOCK"
    assert source.positions[0].currency_provenance == "AUDITED_SOURCE"


def test_config_cannot_silently_override_conflicting_source_currency(tmp_path):
    with pytest.raises(UnsafeAggregationError, match="currency.*conflict"):
        _source(
            tmp_path,
            configured={
                "provenance": "USER_CONFIG",
                "securities": {"US.AAPL": {"currency": "HKD", "security_type": "STOCK"}},
            },
        )
