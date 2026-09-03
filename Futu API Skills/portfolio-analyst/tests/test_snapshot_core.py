import json
import os
import stat
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import jsonschema
import pytest

from portfolio_analyst.classification import Classification, ClassificationMapper
from portfolio_analyst.fx import StaticCurrencyConverter
from portfolio_analyst.models import Account, CashBalance, DataQuality, Position
from portfolio_analyst.persistence import SnapshotAlreadyExists, SnapshotStore
from portfolio_analyst.security_metadata import SecurityMetadata, SecurityMetadataMapper
from portfolio_analyst.snapshot import PortfolioBuilder, UnsafeAggregationError, normalize_security


FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA = Path(__file__).parents[1] / "schemas" / "portfolio_snapshot.schema.json"
FX_EFFECTIVE_AT = datetime(2026, 8, 27, tzinfo=timezone.utc)


def _converter(rates):
    return StaticCurrencyConverter(rates, source="TEST_FIXTURE", effective_at=FX_EFFECTIVE_AT)


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


def test_security_normalization_preserves_symbol_and_market_without_guessing_currency():
    assert normalize_security("US.AAPL") == ("AAPL", "US")
    assert normalize_security("HK.00700") == ("00700", "HK")
    assert normalize_security("SH.600519") == ("600519", "CN")

    with pytest.raises(ValueError, match="market prefix"):
        normalize_security("AAPL")


def test_builder_converts_every_currency_before_aggregation_and_weights_close():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=_converter({("HKD", "USD"): Decimal("0.125")}),
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
                currency="USD",
                currency_provenance="AUDITED_SOURCE",
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
                currency="HKD",
                currency_provenance="USER_CONFIG",
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
    assert payload["positions"][0]["realized_pl_base"] is None
    assert payload["positions"][1]["realized_pl_base"] == "0"
    assert payload["positions"][1]["currency_provenance"] == "USER_CONFIG"


def test_builder_uses_signed_short_market_value_and_explicit_nav_semantics():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=_converter({}),
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
                currency="USD",
                currency_provenance="AUDITED_SOURCE",
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
    assert snapshot.data_quality.status == "PARTIAL"
    assert "classification" in snapshot.data_quality.missing_fields


def test_builder_stops_cross_currency_aggregation_when_fx_is_missing():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=_converter({}),
        classifications=ClassificationMapper({}),
    )

    with pytest.raises(UnsafeAggregationError, match="HKD.*USD"):
        builder.build(
            accounts=(_account(),),
            cash_balances=(CashBalance("acct-0001", "HKD", Decimal("100")),),
            positions=(),
            as_of=FX_EFFECTIVE_AT,
            trd_env="REAL",
            source_version="fixture-v1",
            data_quality=_quality(),
        )


def test_builder_refuses_position_currency_inference_from_market_prefix():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=_converter({}),
        classifications=ClassificationMapper({}),
    )

    with pytest.raises(UnsafeAggregationError, match="currency unavailable.*US.AAPL"):
        builder.build(
            accounts=(_account(),),
            cash_balances=(CashBalance("acct-0001", "USD", Decimal("100")),),
            positions=(
                Position(
                    account_ref="acct-0001",
                    code="US.AAPL",
                    name="No Currency Proof",
                    security_type="UNKNOWN",
                    currency=None,
                    currency_provenance="UNAVAILABLE",
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


def test_builder_refuses_currency_without_auditable_provenance():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=_converter({}),
        classifications=ClassificationMapper({}),
    )

    with pytest.raises(UnsafeAggregationError, match="currency provenance unavailable"):
        builder.build(
            accounts=(_account(),),
            cash_balances=(CashBalance("acct-0001", "USD", Decimal("100")),),
            positions=(
                Position(
                    account_ref="acct-0001",
                    code="US.AAPL",
                    name="Unproven Currency",
                    security_type="UNKNOWN",
                    currency="USD",
                    currency_provenance="UNAVAILABLE",
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


@pytest.mark.parametrize(
    "provenance", ["", " ", "unknown", "N/A", "none", "NULL", "unsupported source"]
)
def test_position_currency_provenance_sentinels_and_unsupported_values_block(provenance):
    position = Position(
        account_ref="acct-0001",
        code="US.AAPL",
        name="Unproven",
        security_type="STOCK",
        currency="USD",
        currency_provenance=provenance,
        quantity=Decimal("1"),
        can_sell_quantity=Decimal("1"),
        average_cost_local=Decimal("10"),
        last_price_local=Decimal("10"),
        market_value_local=Decimal("10"),
        unrealized_pl_local=None,
        realized_pl_local=None,
    )
    assert position.currency_provenance == "UNKNOWN"

    with pytest.raises(UnsafeAggregationError, match="currency provenance unavailable"):
        PortfolioBuilder(
            base_currency="USD",
            converter=_converter({}),
            classifications=ClassificationMapper({}),
        ).build(
            accounts=(_account(),),
            cash_balances=(CashBalance("acct-0001", "USD", Decimal("100")),),
            positions=(position,),
            as_of=FX_EFFECTIVE_AT,
            trd_env="REAL",
            source_version="fixture-v1",
            data_quality=_quality(),
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" audited_source ", "AUDITED_SOURCE"),
        ("Audited Source", "AUDITED_SOURCE"),
        (" user_config ", "USER_CONFIG"),
        ("User Config", "USER_CONFIG"),
    ],
)
def test_allowed_currency_provenance_is_canonicalized(raw, expected):
    position = Position(
        "acct-0001",
        "US.AAPL",
        "Example",
        "STOCK",
        "USD",
        raw,
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        None,
        None,
    )
    assert position.currency_provenance == expected


@pytest.mark.parametrize("provenance", ["", " ", "unknown", "N/A", "none", "NULL", "unsupported"])
def test_direct_security_metadata_currency_provenance_is_canonical_unknown(provenance):
    metadata = SecurityMetadata(currency="USD", security_type="STOCK", provenance=provenance)
    assert metadata.provenance == "UNKNOWN"
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=_converter({}),
        classifications=ClassificationMapper({}),
        security_metadata=SecurityMetadataMapper({"US.AAPL": metadata}),
    )
    position = Position(
        "acct-0001",
        "US.AAPL",
        "Example",
        "STOCK",
        "USD",
        "AUDITED_SOURCE",
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        None,
        None,
    )
    with pytest.raises(UnsafeAggregationError, match="currency provenance unavailable"):
        builder.build(
            accounts=(_account(),),
            cash_balances=(CashBalance("acct-0001", "USD", Decimal("100")),),
            positions=(position,),
            as_of=FX_EFFECTIVE_AT,
            trd_env="REAL",
            source_version="fixture-v1",
            data_quality=_quality(),
        )


def test_exact_security_metadata_uses_audited_source_code_before_market_normalization():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=_converter({("CNH", "USD"): Decimal("0.14")}),
        classifications=ClassificationMapper({}),
        security_metadata=SecurityMetadataMapper(
            {
                "SH.600519": SecurityMetadata(
                    currency="CNH", security_type="STOCK", provenance="USER_CONFIG"
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
                code="SH.600519",
                name="Exact Source Code",
                security_type="UNKNOWN",
                currency=None,
                currency_provenance="UNAVAILABLE",
                quantity=Decimal("1"),
                can_sell_quantity=Decimal("1"),
                average_cost_local=Decimal("100"),
                last_price_local=Decimal("100"),
                market_value_local=Decimal("100"),
                unrealized_pl_local=None,
                realized_pl_local=None,
            ),
        ),
        as_of=FX_EFFECTIVE_AT,
        trd_env="REAL",
        source_version="fixture-v1",
        data_quality=_quality(),
    )

    assert snapshot.positions[0].market == "CN"
    assert snapshot.positions[0].currency == "CNH"


def test_realized_pl_base_uses_verified_non_base_fx_and_preserves_null():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=_converter({("HKD", "USD"): Decimal("0.125")}),
        classifications=ClassificationMapper({}),
    )
    common = dict(
        account_ref="acct-0001",
        name="Example",
        security_type="STOCK",
        currency="HKD",
        currency_provenance="USER_CONFIG",
        quantity=Decimal("1"),
        can_sell_quantity=Decimal("1"),
        average_cost_local=Decimal("80"),
        last_price_local=Decimal("80"),
        market_value_local=Decimal("80"),
        unrealized_pl_local=Decimal("8"),
    )
    snapshot = builder.build(
        accounts=(_account(),),
        cash_balances=(CashBalance("acct-0001", "USD", Decimal("100")),),
        positions=(
            Position(code="HK.00001", realized_pl_local=Decimal("16"), **common),
            Position(code="HK.00002", realized_pl_local=None, **common),
        ),
        as_of=FX_EFFECTIVE_AT,
        trd_env="REAL",
        source_version="fixture-v1",
        data_quality=_quality(),
    )

    assert snapshot.positions[0].realized_pl_base == Decimal("2")
    assert snapshot.positions[1].realized_pl_base is None


def test_unknown_classification_is_literal_and_never_name_guessed():
    result = ClassificationMapper({}).classify("US.AAPL", "Obviously A Technology Company")

    assert result == Classification()
    assert result.sector == "UNKNOWN"
    assert result.themes == ("UNKNOWN",)


def test_builder_rejects_orphan_source_records_before_aggregation():
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=_converter({}),
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
        converter=_converter({}),
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
                currency="USD",
                currency_provenance="AUDITED_SOURCE",
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


@pytest.mark.parametrize(
    "security_type", ["", " ", "unknown", " UNKNOWN ", "n/a", " N/A ", "none", "NULL"]
)
def test_all_security_type_sentinels_canonicalize_and_degrade_quality(security_type):
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=_converter({}),
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
        security_metadata=SecurityMetadataMapper(
            {
                "US.AAPL": SecurityMetadata(
                    currency="USD", security_type=security_type, provenance="USER_CONFIG"
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
                security_type="STOCK",
                currency="USD",
                currency_provenance="AUDITED_SOURCE",
                quantity=Decimal("1"),
                can_sell_quantity=Decimal("1"),
                average_cost_local=Decimal("10"),
                last_price_local=Decimal("10"),
                market_value_local=Decimal("10"),
                unrealized_pl_local=None,
                realized_pl_local=None,
            ),
        ),
        as_of=FX_EFFECTIVE_AT,
        trd_env="REAL",
        source_version="fixture-v1",
        data_quality=_quality(),
    )

    assert snapshot.positions[0].security_type == "UNKNOWN"
    assert snapshot.data_quality.status == "PARTIAL"
    assert "security_type" in snapshot.data_quality.missing_fields


@pytest.mark.parametrize("security_type", [" ", "unknown", "n/a", "none", "null"])
def test_source_position_security_type_sentinels_normalize_at_model_boundary(security_type):
    position = Position(
        account_ref="acct-0001",
        code="US.AAPL",
        name="Example",
        security_type=security_type,
        currency="USD",
        currency_provenance="AUDITED_SOURCE",
        quantity=Decimal("1"),
        can_sell_quantity=Decimal("1"),
        average_cost_local=Decimal("10"),
        last_price_local=Decimal("10"),
        market_value_local=Decimal("10"),
        unrealized_pl_local=None,
        realized_pl_local=None,
    )

    assert position.security_type == "UNKNOWN"


def test_snapshot_store_is_atomic_confined_and_never_overwrites(tmp_path):
    store = SnapshotStore(tmp_path)
    content = '{"schema_version":"1.0"}\n'

    destination = store.save("snapshots/example.json", content)
    assert destination.read_text(encoding="utf-8") == content
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600

    with pytest.raises(SnapshotAlreadyExists):
        store.save("snapshots/example.json", "changed")
    with pytest.raises(ValueError, match="inside"):
        store.save("../escape.json", content)
