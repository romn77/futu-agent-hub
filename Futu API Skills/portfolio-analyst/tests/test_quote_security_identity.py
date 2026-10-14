"""Exchange-qualified source identities survive snapshot normalization and replay."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json

import pytest

from portfolio_analyst.classification import ClassificationMapper
from portfolio_analyst.fx import IdentityOnlyCurrencyConverter
from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.models import Account, AccountNAV, CashBalance, DataQuality, Position
from portfolio_analyst.performance import analyze_performance
from portfolio_analyst.price_history import FutuAdjustedPriceProvider
from portfolio_analyst.risk import analyze_risk
from portfolio_analyst.schema import validate_performance_document
from portfolio_analyst.snapshot import PortfolioBuilder
from portfolio_analyst.snapshot_io import InvalidSnapshot, snapshot_from_document


def chinese_snapshot(code="SH.600519"):
    return PortfolioBuilder(
        base_currency="CNH",
        converter=IdentityOnlyCurrencyConverter(),
        classifications=ClassificationMapper({}),
    ).build(
        accounts=(Account("acct-a", "REAL", "CASH", "NORMAL", ("CN",), "ACTIVE"),),
        account_navs=(AccountNAV("acct-a", "CNH", Decimal(100)),),
        cash_balances=(CashBalance("acct-a", "CNH", Decimal(90)),),
        positions=(
            Position(
                account_ref="acct-a",
                code=code,
                name="Synthetic",
                security_type="STOCK",
                currency="CNH",
                currency_provenance="AUDITED_SOURCE",
                quantity=Decimal(1),
                can_sell_quantity=Decimal(1),
                average_cost_local=Decimal(10),
                last_price_local=Decimal(10),
                market_value_local=Decimal(10),
                unrealized_pl_local=Decimal(0),
                realized_pl_local=Decimal(0),
            ),
        ),
        as_of=datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
        trd_env="REAL",
        source_version="fixture",
        data_quality=DataQuality("COMPLETE"),
    )


class Adapter:
    def __init__(self):
        self.codes = []

    def read_private_json(self, name, arguments, *, timeout):
        self.codes.append(arguments[0])
        return {
            "code": arguments[0],
            "ktype": "1d",
            "source": "history",
            "data": [
                {"time": "2026-08-26 00:00:00", "close": "10"},
                {"time": "2026-08-27 00:00:00", "close": "11"},
            ],
        }


@pytest.mark.parametrize("code", ["SH.600519", "SZ.000001"])
def test_snapshot_preserves_exchange_and_history_uses_original_code(code):
    snapshot = chinese_snapshot(code)
    assert snapshot.to_dict()["positions"][0]["source_code"] == code
    restored = snapshot_from_document(snapshot.to_dict())
    adapter = Adapter()
    provider = FutuAdjustedPriceProvider(adapter, snapshots=(restored,))
    analyze_risk(restored, price_provider=provider)
    assert adapter.codes == [code]


def test_legacy_snapshot_without_exchange_round_trips_and_skips_ambiguous_quote():
    from test_risk_analysis import snapshot as legacy_snapshot

    legacy = legacy_snapshot().to_dict()
    assert snapshot_from_document(legacy).to_dict() == legacy
    adapter = Adapter()
    provider = FutuAdjustedPriceProvider(adapter)
    assert provider.history("600519", market="CN", lookback=120, as_of=date(2026, 8, 27)) is None
    assert adapter.codes == []
    provider.history("AAPL", market="US", lookback=120, as_of=date(2026, 8, 27))
    assert adapter.codes == ["US.AAPL"]


def test_source_code_must_match_normalized_snapshot_identity():
    snapshot = chinese_snapshot()
    bad = replace(snapshot, positions=(replace(snapshot.positions[0], source_code="SH.600000"),))
    bad = replace(bad, snapshot_id=snapshot_identity(bad))
    with pytest.raises(InvalidSnapshot, match="source code"):
        snapshot_from_document(bad.to_dict())


def test_conflicting_exchange_evidence_never_guesses_a_quote_code():
    first = chinese_snapshot("SH.000001")
    second = chinese_snapshot("SZ.000001")
    adapter = Adapter()
    provider = FutuAdjustedPriceProvider(adapter, snapshots=(first, second))
    assert provider.history("000001", market="CN", lookback=120, as_of=date(2026, 8, 27)) is None
    assert adapter.codes == []


def test_canonical_cn_input_round_trips_without_inventing_exchange_evidence():
    snapshot = chinese_snapshot("CN.600519")
    restored = snapshot_from_document(snapshot.to_dict())
    assert restored.to_dict() == snapshot.to_dict()
    assert "source_code" not in restored.to_dict()["positions"][0]
    adapter = Adapter()
    provider = FutuAdjustedPriceProvider(adapter, snapshots=(restored,))
    assert provider.history("600519", market="CN", lookback=120, as_of=date(2026, 8, 27)) is None
    assert adapter.codes == []


def _mixed_market_snapshots(exchange_prefixes):
    from test_risk_analysis import reconciled_snapshot, snapshot as legacy_snapshot

    base = legacy_snapshot()
    snapshots = []
    for hour, prefix in zip((10, 22), exchange_prefixes):
        positions = tuple(
            replace(
                position,
                market="CN",
                symbol=symbol,
                source_code=f"{prefix}.{symbol}" if prefix else None,
            )
            for position, symbol in zip(base.positions, ("000001", "000002", "000003"))
        ) + (replace(base.positions[0], symbol="AAPL"),)
        candidate = reconciled_snapshot(base, positions)
        candidate = replace(candidate, as_of=datetime(2026, 8, 27, hour, tzinfo=timezone.utc))
        candidate = replace(candidate, snapshot_id=snapshot_identity(candidate))
        restored = snapshot_from_document(candidate.to_dict())
        assert restored.to_dict() == candidate.to_dict()
        snapshots.append(restored)
    return tuple(snapshots)


@pytest.mark.parametrize("exchange_prefixes", [(None, None), ("SH", "SZ")])
def test_unavailable_cn_identities_do_not_open_performance_source_circuit(exchange_prefixes):
    snapshots = _mixed_market_snapshots(exchange_prefixes)
    adapter = Adapter()
    report = analyze_performance(
        snapshots,
        start=date(2026, 8, 26),
        end=date(2026, 8, 27),
        price_provider=FutuAdjustedPriceProvider(adapter, snapshots=snapshots),
        analysis_now=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert adapter.codes == ["US.AAPL"]
    evidence = {item.identity: item for item in report.price_evidence}
    assert all(
        evidence[f"CN.{symbol}"].status == "UNAVAILABLE"
        for symbol in ("000001", "000002", "000003")
    )
    assert evidence["US.AAPL"].status == "AVAILABLE"
    assert evidence["US.AAPL"].rows == 2
    validate_performance_document(report.to_dict(), source_snapshots=snapshots)


@pytest.mark.parametrize("failure", ["empty", "error"])
def test_real_price_source_failures_still_open_performance_circuit(failure):
    class FailedAdapter(Adapter):
        def read_private_json(self, name, arguments, *, timeout):
            payload = super().read_private_json(name, arguments, timeout=timeout)
            if failure == "error":
                raise RuntimeError("source unavailable")
            return {**payload, "data": []}

    snapshots = _mixed_market_snapshots(("SH", "SH"))
    adapter = FailedAdapter()
    report = analyze_performance(
        snapshots,
        start=date(2026, 8, 26),
        end=date(2026, 8, 27),
        price_provider=FutuAdjustedPriceProvider(adapter, snapshots=snapshots),
        analysis_now=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert adapter.codes == ["SH.000001", "SH.000002", "SH.000003"]
    apple = next(item for item in report.price_evidence if item.identity == "US.AAPL")
    assert apple.status == "UNAVAILABLE"
    assert "circuit breaker" in apple.reason
    validate_performance_document(report.to_dict(), source_snapshots=snapshots)


@pytest.mark.parametrize("command", ["risk", "performance"])
def test_cli_passes_persisted_exchange_evidence_to_history_provider(
    command, tmp_path, monkeypatch, capsys
):
    from portfolio_analyst import cli
    from portfolio_analyst.settings import Settings
    from portfolio_analyst.snapshot_repository import SnapshotRepository

    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    source = chinese_snapshot()
    saved = SnapshotRepository(home).save(source)
    later = replace(source, as_of=datetime(2026, 8, 28, 10, tzinfo=timezone.utc))
    later = replace(later, snapshot_id=snapshot_identity(later))
    SnapshotRepository(home).save(later)
    adapter = Adapter()
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(home)))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: tmp_path / "fake-skill")
    monkeypatch.setattr(cli, "FutuAdapter", lambda _: adapter)
    arguments = (
        ["risk", "--snapshot", str(saved.relative_to(home))]
        if command == "risk"
        else ["performance", "--from", "2026-08-26", "--to", "2026-08-28"]
    )
    assert cli.main([*arguments, "--json", "--no-save"]) == 0
    json.loads(capsys.readouterr().out)
    assert adapter.codes == ["SH.600519"]
