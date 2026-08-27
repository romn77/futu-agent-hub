import json
import subprocess
from pathlib import Path

import pytest

from portfolio_analyst.futu_adapter import FutuAdapter, FutuReadError
from portfolio_analyst.repository import FutuPortfolioRepository, SourceDataError


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureAdapter:
    def __init__(self):
        self.calls = []

    def read_private_json(self, name, arguments, *, timeout=30):
        self.calls.append((name, tuple(arguments), timeout))
        filename = "accounts.json" if name == "get_accounts" else "portfolios.json"
        return json.loads((FIXTURES / filename).read_text(encoding="utf-8"))


def test_repository_reads_and_merges_every_real_securities_account_with_masked_refs():
    adapter = FixtureAdapter()

    result = FutuPortfolioRepository(adapter).read(trd_env="REAL")

    assert adapter.calls == [
        ("get_accounts", ("--json",), 30),
        ("get_all_portfolios", ("--trd-env", "REAL", "--json"), 30),
    ]
    assert len(result.accounts) == 2
    assert len(result.positions) == 3
    assert {(cash.currency, cash.amount_local) for cash in result.cash_balances} == {
        ("USD", 100),
        ("HKD", 800),
        ("USD", 50),
    }
    assert result.data_quality.status == "COMPLETE"
    assert result.data_quality.coverage == {"accounts_expected": 2, "accounts_read": 2}
    serialized = json.dumps(
        {
            "accounts": [account.account_ref for account in result.accounts],
            "positions": [position.account_ref for position in result.positions],
        }
    )
    assert "90000001" not in serialized
    assert "90000002" not in serialized


def test_repository_marks_partial_coverage_without_inventing_missing_account_data():
    class MissingPortfolioAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            payload = super().read_private_json(name, arguments, timeout=timeout)
            if name == "get_all_portfolios":
                payload["accounts"] = payload["accounts"][:1]
            return payload

    result = FutuPortfolioRepository(MissingPortfolioAdapter()).read(trd_env="REAL")

    assert result.data_quality.status == "PARTIAL"
    assert result.data_quality.coverage == {"accounts_expected": 2, "accounts_read": 1}
    assert result.data_quality.source_errors


def test_repository_refuses_unallocated_nonzero_cash_without_a_currency():
    class AmbiguousCashAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            payload = super().read_private_json(name, arguments, timeout=timeout)
            if name == "get_all_portfolios":
                payload["accounts"][0]["funds"] = {"cash": 10}
            return payload

    with pytest.raises(SourceDataError, match="currency"):
        FutuPortfolioRepository(AmbiguousCashAdapter()).read(trd_env="REAL")


def test_repository_refuses_missing_funds_instead_of_treating_them_as_zero():
    class MissingFundsAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            payload = super().read_private_json(name, arguments, timeout=timeout)
            if name == "get_all_portfolios":
                payload["accounts"][0]["funds"] = {}
            return payload

    with pytest.raises(SourceDataError, match="funds"):
        FutuPortfolioRepository(MissingFundsAdapter()).read(trd_env="REAL")


def test_repository_normalizes_short_role_to_negative_signed_market_value():
    class ShortPositionAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            payload = super().read_private_json(name, arguments, timeout=timeout)
            if name == "get_all_portfolios":
                position = payload["accounts"][0]["positions"][0]
                position.update({"qty": 2, "market_val": 200, "position_type": "SHORT"})
            return payload

    result = FutuPortfolioRepository(ShortPositionAdapter()).read(trd_env="REAL")

    assert result.positions[0].quantity == 2
    assert result.positions[0].market_value_local == -200
    assert "sign normalized" in result.positions[0].data_quality_notes[0]


def test_private_json_adapter_parses_account_ids_for_internal_use_but_public_run_stays_redacted(
    monkeypatch, tmp_path
):
    # Bypass construction only to test the subprocess-result boundary independently of the manifest.
    adapter = object.__new__(FutuAdapter)
    raw = subprocess.CompletedProcess(
        args=["python"], returncode=0, stdout='{"accounts":[{"acc_id":90000001}]}', stderr=""
    )
    monkeypatch.setattr(adapter, "_run_unsanitized", lambda *_args, **_kwargs: raw)

    payload = adapter.read_private_json("get_accounts", ["--json"])

    assert payload["accounts"][0]["acc_id"] == 90000001


def test_private_json_adapter_never_includes_raw_output_in_errors(monkeypatch):
    adapter = object.__new__(FutuAdapter)
    raw = subprocess.CompletedProcess(
        args=["python", "--acc-id", "90000001"],
        returncode=1,
        stdout='{"error":"SDK unavailable; account_id=90000001 failed"}',
        stderr="audited warning; account_id=90000001 failed",
    )
    monkeypatch.setattr(adapter, "_run_unsanitized", lambda *_args, **_kwargs: raw)

    with pytest.raises(FutuReadError) as caught:
        adapter.read_private_json("get_portfolio", ["--acc-id", "90000001", "--json"])

    assert "90000001" not in str(caught.value)
    assert "SDK unavailable" in str(caught.value)
    assert "audited warning" in str(caught.value)
