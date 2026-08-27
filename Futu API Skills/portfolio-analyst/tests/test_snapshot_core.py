import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import jsonschema
import pytest

from portfolio_analyst.classification import Classification, ClassificationMapper
from portfolio_analyst.fx import StaticCurrencyConverter
from portfolio_analyst.models import Account, CashBalance, DataQuality, Position
from portfolio_analyst.persistence import SnapshotAlreadyExists, SnapshotStore
from portfolio_analyst.snapshot import PortfolioBuilder, UnsafeAggregationError, normalize_security


FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA = Path(__file__).parents[1] / "schemas" / "portfolio_snapshot.schema.json"


def _account(ref: str = "acct-0001") -> Account:
    return Account(
        account_ref=ref,
        trd_env="REAL",
        account_type="MARGIN",
        account_role="NORMAL",
        markets=("US", "HK"),
        status="ACTIVE",
    )


def _quality() -> DataQuality:
    return DataQuality(
        status="COMPLETE",
        coverage={"accounts_expected": 1, "accounts_read": 1},
    )


def test_security_normalization_preserves_symbol_and_derives_market_currency():
    assert normalize_security("US.AAPL") == ("AAPL", "US", "USD")
    assert normalize_security("HK.00700") == ("00700", "HK", "HKD")
    assert normalize_security("SH.600519") == ("600519", "CN", "CNH")

    with pytest.raises(ValueError, match="market prefix"):
        normalize_security("AAPL")


def test_builder_converts_every_currency_before_aggregation_and_weights_close():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=StaticCurrencyConverter({("HKD", "USD"): Decimal("0.125")}),
        classifications=ClassificationMapper(
            {
                "US.AAPL": Classification(
                    sector="TECHNOLOGY",
                    industry="HARDWARE",
                    region="US",
                    themes=("LARGE_CAP",),
                    asset_class="EQUITY",
                ),
                "HK.00700": Classification(
                    sector="TECHNOLOGY",
                    industry="INTERNET",
                    region="CN",
                    themes=("LARGE_CAP",),
                    asset_class="EQUITY",
                ),
            }
        ),
    )
    snapshot = builder.build(
        accounts=(_account(),),
        cash_balances=(
            CashBalance("acct-0001", "USD", Decimal("100")),
            CashBalance("acct-0001", "HKD", Decimal("800")),
        ),
        positions=(
            Position(
                account_ref="acct-0001",
                code="US.AAPL",
                name="Example US Equity",
                security_type="STOCK",
                quantity=Decimal("10"),
                can_sell_quantity=Decimal("10"),
                average_cost_local=Decimal("40"),
                last_price_local=Decimal("50"),
                market_value_local=Decimal("500"),
                unrealized_pl_local=Decimal("100"),
                realized_pl_local=None,
            ),
            Position(
                account_ref="acct-0001",
                code="HK.00700",
                name="Example HK Equity",
                security_type="STOCK",
                quantity=Decimal("8"),
                can_sell_quantity=Decimal("8"),
                average_cost_local=Decimal("300"),
                last_price_local=Decimal("400"),
                market_value_local=Decimal("3200"),
                unrealized_pl_local=Decimal("800"),
                realized_pl_local=Decimal("0"),
            ),
        ),
        as_of=datetime(2026, 8, 27, 8, 30, tzinfo=timezone.utc),
        trd_env="REAL",
        source_version="fixture-v1",
        data_quality=_quality(),
    )

    assert snapshot.cash_base == Decimal("200")
    assert snapshot.gross_exposure_base == Decimal("900")
    assert snapshot.net_exposure_base == Decimal("900")
    assert snapshot.nav_base == Decimal("1100")
    weight_total = sum(position.weight for position in snapshot.positions) + snapshot.cash_weight
    assert abs(weight_total - Decimal("1")) <= PortfolioBuilder.WEIGHT_TOLERANCE
    assert snapshot.exposures["sector"]["TECHNOLOGY"] == sum(
        position.weight for position in snapshot.positions
    )

    payload = snapshot.to_dict()
    jsonschema.validate(payload, json.loads(SCHEMA.read_text(encoding="utf-8")))
    assert payload["nav_base"] == "1100"
    assert payload["positions"][1]["fx_to_base"] == "0.125"


def test_builder_uses_signed_short_market_value_and_explicit_nav_semantics():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=StaticCurrencyConverter({}),
        classifications=ClassificationMapper({}),
    )
    snapshot = builder.build(
        accounts=(_account(),),
        cash_balances=(CashBalance("acct-0001", "USD", Decimal("300")),),
        positions=(
            Position(
                account_ref="acct-0001",
                code="US.SHORT",
                name="Short Position",
                security_type="STOCK",
                quantity=Decimal("-2"),
                can_sell_quantity=Decimal("0"),
                average_cost_local=Decimal("100"),
                last_price_local=Decimal("100"),
                market_value_local=Decimal("-200"),
                unrealized_pl_local=None,
                realized_pl_local=None,
                data_quality_notes=("market value sign normalized from source short semantics",),
            ),
        ),
        as_of=datetime.now(timezone.utc),
        trd_env="REAL",
        source_version="fixture-v1",
        data_quality=_quality(),
    )

    assert snapshot.long_exposure_base == Decimal("0")
    assert snapshot.short_exposure_base == Decimal("200")
    assert snapshot.gross_exposure_base == Decimal("200")
    assert snapshot.net_exposure_base == Decimal("-200")
    assert snapshot.nav_base == Decimal("100")
    assert snapshot.positions[0].weight == Decimal("-2")
    assert snapshot.cash_weight == Decimal("3")
    assert snapshot.data_quality.status == "RECONSTRUCTED"


def test_builder_stops_cross_currency_aggregation_when_fx_is_missing():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=StaticCurrencyConverter({}),
        classifications=ClassificationMapper({}),
    )

    with pytest.raises(UnsafeAggregationError, match="HKD.*USD"):
        builder.build(
            accounts=(_account(),),
            cash_balances=(CashBalance("acct-0001", "HKD", Decimal("100")),),
            positions=(),
            as_of=datetime.now(timezone.utc),
            trd_env="REAL",
            source_version="fixture-v1",
            data_quality=_quality(),
        )


def test_unknown_classification_is_literal_and_never_name_guessed():
    result = ClassificationMapper({}).classify("US.AAPL", "Obviously A Technology Company")

    assert result == Classification()
    assert result.sector == "UNKNOWN"
    assert result.themes == ("UNKNOWN",)


def test_builder_rejects_orphan_source_records_before_aggregation():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=StaticCurrencyConverter({}),
        classifications=ClassificationMapper({}),
    )

    with pytest.raises(UnsafeAggregationError, match="unknown masked account"):
        builder.build(
            accounts=(_account(),),
            cash_balances=(CashBalance("acct-orphan", "USD", Decimal("1")),),
            positions=(),
            as_of=datetime.now(timezone.utc),
            trd_env="REAL",
            source_version="fixture-v1",
            data_quality=_quality(),
        )


def test_unknown_security_type_degrades_quality_explicitly():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=StaticCurrencyConverter({}),
        classifications=ClassificationMapper(
            {
                "US.AAPL": Classification(
                    sector="TECHNOLOGY",
                    industry="HARDWARE",
                    region="US",
                    themes=("LARGE_CAP",),
                    asset_class="EQUITY",
                )
            }
        ),
    )
    snapshot = builder.build(
        accounts=(_account(),),
        cash_balances=(CashBalance("acct-0001", "USD", Decimal("100")),),
        positions=(
            Position(
                account_ref="acct-0001",
                code="US.AAPL",
                name="Example",
                security_type="UNKNOWN",
                quantity=Decimal("1"),
                can_sell_quantity=Decimal("1"),
                average_cost_local=Decimal("10"),
                last_price_local=Decimal("10"),
                market_value_local=Decimal("10"),
                unrealized_pl_local=None,
                realized_pl_local=None,
            ),
        ),
        as_of=datetime.now(timezone.utc),
        trd_env="REAL",
        source_version="fixture-v1",
        data_quality=_quality(),
    )

    assert snapshot.data_quality.status == "PARTIAL"
    assert "security_type" in snapshot.data_quality.missing_fields


def test_snapshot_store_is_atomic_confined_and_never_overwrites(tmp_path):
    store = SnapshotStore(tmp_path)
    content = '{"schema_version":"1.0"}\n'

    destination = store.save("snapshots/example.json", content)
    assert destination.read_text(encoding="utf-8") == content

    with pytest.raises(SnapshotAlreadyExists):
        store.save("snapshots/example.json", "changed")
    with pytest.raises(ValueError, match="inside"):
        store.save("../escape.json", content)
