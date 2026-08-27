import json
import subprocess
from pathlib import Path

import pytest

from portfolio_analyst.account_alias import AccountAliaser
from portfolio_analyst.futu_adapter import FutuAdapter, FutuReadError
from portfolio_analyst.repository import FutuPortfolioRepository


FIXTURES = Path(__file__).parent / "fixtures"
ACCOUNT_IDS = ("90000001", "90000002")


def _repository(adapter, *, discovery_complete=True, currencies=None):
    return FutuPortfolioRepository(
        adapter,
        aliaser=AccountAliaser.from_key(b"a" * 32),
        account_currencies=(
            {account_id: "USD" for account_id in ACCOUNT_IDS} if currencies is None else currencies
        ),
        discovery_complete=discovery_complete,
    )


class FixtureAdapter:
    def __init__(self):
        self.calls = []
        self.protected = ()

    def protect_sensitive_values(self, values):
        self.protected = tuple(values)

    def read_private_json(self, name, arguments, *, timeout=30):
        self.calls.append((name, tuple(arguments), timeout))
        if name == "get_accounts":
            return json.loads((FIXTURES / "accounts.json").read_text(encoding="utf-8"))
        account_id = arguments[arguments.index("--acc-id") + 1]
        number = ACCOUNT_IDS.index(account_id) + 1
        return json.loads(
            (FIXTURES / f"portfolio_account_{number}.json").read_text(encoding="utf-8")
        )


def test_repository_queries_each_real_securities_account_with_exact_firm_and_market():
    adapter = FixtureAdapter()

    result = _repository(adapter).read(trd_env="REAL")

    assert adapter.protected == ACCOUNT_IDS
    assert adapter.calls == [
        ("get_accounts", ("--json",), 30),
        (
            "get_portfolio",
            (
                "--acc-id",
                "90000001",
                "--market",
                "US",
                "--trd-env",
                "REAL",
                "--currency",
                "USD",
                "--security-firm",
                "FUTUINC",
                "--ctx-type",
                "SEC",
                "--json",
            ),
            30,
        ),
        (
            "get_portfolio",
            (
                "--acc-id",
                "90000002",
                "--market",
                "US",
                "--trd-env",
                "REAL",
                "--currency",
                "USD",
                "--security-firm",
                "FUTUSECURITIES",
                "--ctx-type",
                "SEC",
                "--json",
            ),
            30,
        ),
    ]
    assert len(result.accounts) == 2
    assert len(result.positions) == 3
    assert {(cash.currency, cash.amount_local) for cash in result.cash_balances} == {
        ("USD", 100),
        ("USD", 50),
    }
    assert result.positions[0].currency is None
    assert result.positions[0].currency_provenance == "UNKNOWN"
    assert result.positions[0].security_type == "UNKNOWN"
    assert result.positions[0].realized_pl_local == 25
    assert result.data_quality.status == "COMPLETE"
    assert result.data_quality.coverage == {
        "accounts_returned": 2,
        "accounts_returned_read": 2,
        "discovery_status": "COMPLETE",
        "alias_provenance": "STABLE",
    }
    serialized = json.dumps(result, default=str)
    assert all(account_id not in serialized for account_id in ACCOUNT_IDS)


def test_per_account_failure_is_partial_and_never_echoes_any_discovered_id():
    class PartialAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            if name == "get_portfolio" and "90000002" in arguments:
                raise FutuReadError(
                    'failed account_id=90000002; peer {"acc_id":90000001}; raw 90000001'
                )
            return super().read_private_json(name, arguments, timeout=timeout)

    result = _repository(PartialAdapter()).read(trd_env="REAL")

    assert result.data_quality.status == "PARTIAL"
    assert result.data_quality.coverage["accounts_returned_read"] == 1
    exposed = json.dumps(result.data_quality.__dict__, default=str)
    assert all(account_id not in exposed for account_id in ACCOUNT_IDS)
    assert "masked account" in exposed


def test_all_per_account_failures_are_explicitly_unavailable():
    class UnavailableAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            if name == "get_portfolio":
                raise FutuReadError("portfolio unavailable")
            return super().read_private_json(name, arguments, timeout=timeout)

    result = _repository(UnavailableAdapter()).read(trd_env="REAL")

    assert result.data_quality.status == "UNAVAILABLE"
    assert result.data_quality.coverage["accounts_returned_read"] == 0
    assert not result.cash_balances
    assert not result.positions


@pytest.mark.parametrize(
    "stdout",
    [
        "failed 90000002 while reading 90000001",
        '{"error":"failed 90000002", "acc_id":90000001}',
    ],
)
def test_adapter_registered_discovered_ids_are_redacted_from_all_error_shapes(monkeypatch, stdout):
    adapter = object.__new__(FutuAdapter)
    adapter._protected_sensitive_values = set()
    adapter.protect_sensitive_values(ACCOUNT_IDS)
    raw = subprocess.CompletedProcess(
        args=["python", "--acc-id", "90000001"],
        returncode=1,
        stdout=stdout,
        stderr="exception 90000002",
    )
    monkeypatch.setattr(adapter, "_run_unsanitized", lambda *_args, **_kwargs: raw)

    with pytest.raises(FutuReadError) as caught:
        adapter.read_private_json("get_portfolio", ["--acc-id", "90000001", "--json"])

    exposed = str(caught.value)
    assert all(account_id not in exposed for account_id in ACCOUNT_IDS)


def test_registered_peer_id_is_redacted_from_timeout_exception(monkeypatch):
    adapter = object.__new__(FutuAdapter)
    adapter._protected_sensitive_values = set()
    adapter.protect_sensitive_values(ACCOUNT_IDS)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["python", "90000001"],
            timeout=1,
            output="waiting 90000002",
            stderr='{"acc_id":90000001}',
        )

    monkeypatch.setattr(adapter, "_run_unsanitized", timeout)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        adapter.read_private_json("get_portfolio", ["--acc-id", "90000001", "--json"])

    exposed = f"{caught.value.cmd} {caught.value.output} {caught.value.stderr}"
    assert all(account_id not in exposed for account_id in ACCOUNT_IDS)


@pytest.mark.parametrize(
    "stdout",
    [
        "get_accounts failed for account 90000001",
        '{"error":"get_accounts failed for account 90000001"}',
    ],
)
def test_initial_account_list_failure_redacts_ids_before_discovery(monkeypatch, stdout):
    adapter = object.__new__(FutuAdapter)
    adapter._protected_sensitive_values = set()
    raw = subprocess.CompletedProcess(
        args=["python", "get_accounts.py"], returncode=1, stdout=stdout, stderr=""
    )
    monkeypatch.setattr(adapter, "_run_unsanitized", lambda *_args, **_kwargs: raw)

    with pytest.raises(FutuReadError) as caught:
        _repository(adapter).read(trd_env="REAL")

    assert "90000001" not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


def test_live_repository_never_claims_complete_discovery():
    result = _repository(FixtureAdapter(), discovery_complete=False).read(trd_env="REAL")

    assert result.data_quality.status == "PARTIAL"
    assert result.data_quality.coverage["discovery_status"] == "UNVERIFIED"
    assert result.data_quality.coverage["accounts_returned_read"] == 2
    assert any("discovery" in warning for warning in result.data_quality.warnings)


@pytest.mark.parametrize("valuation_currency", ["USD", "CAD"])
def test_futuca_account_uses_explicit_per_account_valuation_currency(valuation_currency):
    account_id = "90000003"

    class FutuCaAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            self.calls.append((name, tuple(arguments), timeout))
            if name == "get_accounts":
                return {
                    "accounts": [
                        {
                            "acc_id": 90000003,
                            "acc_type": "MARGIN",
                            "acc_role": "NORMAL",
                            "trd_env": "REAL",
                            "card_num": "SANITIZED",
                            "security_firm": "FUTUCA",
                            "trdmarket_auth": ["US"],
                            "acc_status": "ACTIVE",
                            "sim_acc_type": "NONE",
                            "competition_acc_name": "N/A",
                            "ctx_type": "SEC",
                        }
                    ]
                }
            return {
                "funds": {"currency": valuation_currency, "cash": 100},
                "positions": [],
            }

    adapter = FutuCaAdapter()
    result = _repository(
        adapter, currencies={account_id: valuation_currency}, discovery_complete=True
    ).read(trd_env="REAL")

    portfolio_call = adapter.calls[1][1]
    assert portfolio_call[portfolio_call.index("--currency") + 1] == valuation_currency
    assert portfolio_call[portfolio_call.index("--security-firm") + 1] == "FUTUCA"
    assert result.cash_balances[0].currency == valuation_currency
    assert account_id not in json.dumps(result, default=str)


def test_missing_required_account_currency_is_explicitly_unavailable():
    result = _repository(FixtureAdapter(), currencies={}, discovery_complete=True).read(
        trd_env="REAL"
    )

    assert result.data_quality.status == "UNAVAILABLE"
    assert result.data_quality.coverage["accounts_returned_read"] == 0
    assert not result.cash_balances
    assert not result.positions
