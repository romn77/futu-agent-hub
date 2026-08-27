import hashlib
import json
import os
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import jsonschema
import pytest

from portfolio_analyst.account_alias import AccountAliasError, AccountAliasStore, AccountAliaser
from portfolio_analyst.classification import Classification, ClassificationMapper
from portfolio_analyst.fx import FxEvidenceError, StaticCurrencyConverter
from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.models import Account, CashBalance, DataQuality, Position
from portfolio_analyst.persistence import SnapshotStore
from portfolio_analyst.quality import compose_quality
from portfolio_analyst.snapshot import PortfolioBuilder, UnsafeAggregationError


AS_OF = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)


def _builder(*, effective_at=AS_OF, source="TEST_SOURCE", classifications=None):
    return PortfolioBuilder(
        base_currency="USD",
        converter=StaticCurrencyConverter(
            {("HKD", "USD"): Decimal("0.125")},
            source=source,
            effective_at=effective_at,
        ),
        classifications=classifications
        or ClassificationMapper(
            {
                "HK.00001": Classification(
                    sector="FINANCE",
                    industry="BANK",
                    region="HK",
                    themes=("VALUE",),
                    asset_class="EQUITY",
                )
            }
        ),
    )


def _snapshot(builder=None, *, source_version="v1", quality=None):
    builder = builder or _builder()
    return builder.build(
        accounts=(Account("acct-private", "REAL", "MARGIN", "NORMAL", ("HK",), "ACTIVE"),),
        cash_balances=(CashBalance("acct-private", "USD", Decimal("100")),),
        positions=(
            Position(
                account_ref="acct-private",
                code="HK.00001",
                name="Fixture",
                security_type="STOCK",
                currency="HKD",
                currency_provenance="USER_CONFIG",
                quantity=Decimal("1"),
                can_sell_quantity=Decimal("1"),
                average_cost_local=Decimal("800"),
                last_price_local=Decimal("800"),
                market_value_local=Decimal("800"),
                unrealized_pl_local=Decimal("80"),
                realized_pl_local=Decimal("8"),
            ),
        ),
        as_of=AS_OF,
        trd_env="REAL",
        source_version=source_version,
        data_quality=quality or DataQuality("COMPLETE", coverage={"discovery_status": "COMPLETE"}),
    )


def test_fx_evidence_used_is_serialized_including_deterministic_base_identity():
    snapshot = _snapshot()
    evidence = snapshot.to_dict()["fx_evidence"]

    assert evidence == [
        {
            "source_currency": "HKD",
            "target_currency": "USD",
            "rate": "0.125",
            "source": "TEST_SOURCE",
            "effective_at": "2026-08-27T12:00:00Z",
        },
        {
            "source_currency": "USD",
            "target_currency": "USD",
            "rate": "1",
            "source": "BASE_CURRENCY_IDENTITY",
            "effective_at": "2026-08-27T12:00:00Z",
        },
    ]


@pytest.mark.parametrize(
    ("effective_at", "message"),
    [
        (AS_OF - timedelta(hours=97), "stale"),
        (AS_OF + timedelta(seconds=1), "future"),
    ],
)
def test_fx_future_and_stale_evidence_fail_before_valuation(effective_at, message):
    with pytest.raises(UnsafeAggregationError, match=message):
        _snapshot(_builder(effective_at=effective_at))


@pytest.mark.parametrize(
    "unused_effective_at", [AS_OF - timedelta(days=30), AS_OF + timedelta(days=30)]
)
def test_usd_only_snapshot_ignores_and_omits_unused_stale_or_future_hkd_rate(
    unused_effective_at,
):
    builder = PortfolioBuilder(
        base_currency="USD",
        converter=StaticCurrencyConverter(
            {("HKD", "USD"): Decimal("0.125")},
            source="UNUSED_RATE",
            effective_at=unused_effective_at,
        ),
        classifications=ClassificationMapper({}),
    )
    snapshot = builder.build(
        accounts=(Account("acct-private", "REAL", "MARGIN", "NORMAL", ("US",), "ACTIVE"),),
        cash_balances=(CashBalance("acct-private", "USD", Decimal("100")),),
        positions=(),
        as_of=AS_OF,
        trd_env="REAL",
        source_version="v1",
        data_quality=DataQuality("COMPLETE"),
    )

    assert [(item.source_currency, item.source) for item in snapshot.fx_evidence] == [
        ("USD", "BASE_CURRENCY_IDENTITY")
    ]


def test_explicit_96_hour_policy_intentionally_accepts_weekend_gap():
    friday = datetime(2026, 8, 28, 21, tzinfo=timezone.utc)
    monday = friday + timedelta(hours=72)
    converter = StaticCurrencyConverter(
        {("HKD", "USD"): Decimal("0.125")},
        source="TEST_SOURCE",
        effective_at=friday,
        max_age=timedelta(hours=96),
    )
    assert converter.quote("HKD", "USD", monday).rate == Decimal("0.125")


@pytest.mark.parametrize(
    "source", [None, "", "   ", "UNKNOWN", " unknown ", "N/A", " n/a ", "NONE", "null"]
)
def test_fx_source_provenance_is_mandatory(source):
    with pytest.raises(FxEvidenceError, match="source provenance"):
        _builder(source=source)


def test_snapshot_identity_covers_complete_body_and_is_reproducible():
    first = _snapshot()
    second = _snapshot()

    assert first.snapshot_id == second.snapshot_id
    assert snapshot_identity(replace(first, source_version="v2")) != first.snapshot_id
    changed_fx = replace(
        first,
        fx_evidence=(
            replace(first.fx_evidence[0], source="DIFFERENT_SOURCE"),
            *first.fx_evidence[1:],
        ),
    )
    assert snapshot_identity(changed_fx) != first.snapshot_id
    changed_quality = replace(
        first, data_quality=replace(first.data_quality, warnings=("material warning",))
    )
    assert snapshot_identity(changed_quality) != first.snapshot_id


def test_snapshot_identity_canonicalizes_decimal_time_and_mapping_order():
    snapshot = _snapshot()
    reordered_exposures = {
        key: dict(reversed(tuple(values.items())))
        for key, values in reversed(tuple(snapshot.exposures.items()))
    }
    equivalent = replace(
        snapshot,
        as_of=snapshot.as_of.astimezone(timezone(timedelta(hours=8))),
        nav_base=snapshot.nav_base.quantize(Decimal("0.0")),
        exposures=reordered_exposures,
    )

    assert snapshot_identity(equivalent) == snapshot.snapshot_id


@pytest.mark.parametrize("negative_zero", ["-0", "-0.0", "-0.00", "-0E+9"])
def test_decimal_serialization_and_identity_canonicalize_all_signed_zero_variants(
    negative_zero,
):
    snapshot = _snapshot()
    positive = replace(snapshot, cash_base=Decimal("0.000"))
    negative = replace(snapshot, cash_base=Decimal(negative_zero))

    assert positive.to_dict()["cash_base"] == "0"
    assert negative.to_dict()["cash_base"] == "0"
    assert snapshot_identity(positive) == snapshot_identity(negative)


def test_snapshot_nested_mappings_are_deeply_immutable_and_to_dict_is_copy_boundary():
    snapshot = _snapshot()

    with pytest.raises(TypeError):
        snapshot.exposures["sector"]["FINANCE"] = Decimal("0")
    with pytest.raises(TypeError):
        snapshot.data_quality.coverage["discovery_status"] = "UNVERIFIED"

    payload = snapshot.to_dict()
    payload["exposures"]["sector"]["FINANCE"] = "0"
    payload["data_quality"]["coverage"]["discovery_status"] = "MUTATED"
    assert snapshot.exposures["sector"]["FINANCE"] != Decimal("0")
    assert snapshot.data_quality.coverage["discovery_status"] == "COMPLETE"

    source_accounts = list(snapshot.accounts)
    frozen_copy = replace(snapshot, accounts=source_accounts)
    source_accounts.clear()
    assert frozen_copy.accounts == snapshot.accounts


@pytest.mark.parametrize(
    ("base", "kwargs", "expected"),
    [
        (DataQuality("COMPLETE"), {"reconstructed": True}, "RECONSTRUCTED"),
        (
            DataQuality("COMPLETE"),
            {"reconstructed": True, "missing_fields": ("classification",)},
            "PARTIAL",
        ),
        (DataQuality("COMPLETE"), {"stale": True}, "STALE"),
        (
            DataQuality("COMPLETE", source_errors=("source failed",)),
            {"stale": True},
            "PARTIAL",
        ),
        (DataQuality("UNAVAILABLE"), {"missing_fields": ("x",)}, "UNAVAILABLE"),
        (DataQuality("ERROR"), {"stale": True}, "ERROR"),
    ],
)
def test_quality_composition_matrix(base, kwargs, expected):
    composed = compose_quality(base, **kwargs)
    assert composed.status == expected
    if kwargs.get("stale"):
        assert any("STALE" in warning for warning in composed.warnings)


def test_classification_unknown_sentinels_are_case_canonicalized_and_detected():
    classification = Classification(
        sector=" unknown ", industry="n/A", region="None", themes=("null",), asset_class=""
    )
    assert classification == Classification()
    snapshot = _snapshot(
        _builder(classifications=ClassificationMapper({"HK.00001": classification}))
    )
    assert snapshot.data_quality.status == "PARTIAL"
    assert "classification" in snapshot.data_quality.missing_fields


def test_nonpositive_nav_fails_closed_without_inverted_weights():
    builder = _builder()
    with pytest.raises(UnsafeAggregationError, match="NAV must be positive"):
        builder.build(
            accounts=(Account("acct-private", "REAL", "MARGIN", "NORMAL", ("US",), "ACTIVE"),),
            cash_balances=(CashBalance("acct-private", "USD", Decimal("100")),),
            positions=(
                Position(
                    "acct-private",
                    "US.SHORT",
                    "Short",
                    "STOCK",
                    "USD",
                    "AUDITED_SOURCE",
                    Decimal("-2"),
                    Decimal("0"),
                    Decimal("100"),
                    Decimal("100"),
                    Decimal("-200"),
                    None,
                    None,
                ),
            ),
            as_of=AS_OF,
            trd_env="REAL",
            source_version="v1",
            data_quality=DataQuality("COMPLETE"),
        )


def test_private_hmac_aliases_are_stable_distinct_and_not_public_hashes():
    aliaser = AccountAliaser.from_key(b"a" * 32)
    first = aliaser.alias("90000001")

    assert first == aliaser.alias("90000001")
    assert first != aliaser.alias("90000002")
    assert "0001" not in first
    public_guess = hashlib.sha256(b"portfolio-account:90000001").hexdigest()
    assert public_guess not in first


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission and no-follow contract")
def test_alias_key_is_persisted_0600_and_symlink_is_refused(tmp_path):
    home = tmp_path / "private"
    home.mkdir()
    store = AccountAliasStore(home)
    first = store.load_or_create().alias("90000001")
    key_path = home / "config" / "account_alias.key"

    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert store.load_or_create().alias("90000001") == first

    key_path.unlink()
    target = tmp_path / "target"
    target.write_bytes(b"x" * 32)
    key_path.symlink_to(target)
    with pytest.raises(AccountAliasError, match="symlink"):
        store.load_or_create()


@pytest.mark.skipif(os.name != "posix", reason="POSIX dir-fd adversarial contract")
def test_snapshot_store_rejects_symlink_component_and_concurrent_component_swap(
    tmp_path, monkeypatch
):
    home = tmp_path / "private"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "linked").symlink_to(outside, target_is_directory=True)
    store = SnapshotStore(home)
    with pytest.raises(ValueError, match="symlink"):
        store.save("linked/leak.json", "secret")
    assert not (outside / "leak.json").exists()

    snapshots = home / "snapshots"
    snapshots.mkdir()
    moved = home / "moved"
    original_link = os.link
    swapped = False

    def swap_then_link(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            snapshots.rename(moved)
            snapshots.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", swap_then_link)
    with pytest.raises(ValueError, match="changed"):
        store.save("snapshots/race.json", "secret")
    assert not (outside / "race.json").exists()
    assert not (moved / "race.json").exists()


def test_snapshot_json_and_schema_remain_compatible_after_freezing():
    snapshot = _snapshot()
    schema_path = (
        __import__("pathlib").Path(__file__).parents[1]
        / "schemas"
        / "portfolio_snapshot.schema.json"
    )
    payload = snapshot.to_dict()
    json.dumps(payload)
    jsonschema.validate(payload, json.loads(schema_path.read_text(encoding="utf-8")))
