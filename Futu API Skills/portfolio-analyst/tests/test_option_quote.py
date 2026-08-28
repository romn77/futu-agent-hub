import io
import json
from pathlib import Path
import shutil
import subprocess
from decimal import Decimal

import pytest

from portfolio_analyst import futu_adapter
from portfolio_analyst.futu_adapter import FutuAdapter, UnsafeFutuCommand
from portfolio_analyst.option_quote import OptionQuoteError, parse_option_quote_response
from portfolio_analyst.option_quote import FutuOptionQuoteProvider
from portfolio_analyst.skill_trust import (
    OPTION_QUOTE_SHA256,
    audited_files,
    validate_option_quote_capability,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_SKILL = PROJECT_ROOT.parent / "futuapi"


def _skill(tmp_path: Path, *, option_quote: bool = True) -> Path:
    skill = tmp_path / "futuapi"
    for record in audited_files():
        destination = skill / record.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OFFICIAL_SKILL / record.relative_path, destination)
    if option_quote:
        relative = Path("scripts/quote/get_option_quote.py")
        destination = skill / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OFFICIAL_SKILL / relative, destination)
    return skill


def _canonical_request(code: str = "US.AAPL260116C00150000") -> str:
    return json.dumps(
        [{"code": code, "action": "BUY", "quantity": 1.0}],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def test_option_quote_digest_is_optional_and_isolated_from_core_trust(tmp_path):
    missing = _skill(tmp_path / "missing", option_quote=False)
    tampered = _skill(tmp_path / "tampered")
    script = tampered / "scripts/quote/get_option_quote.py"
    script.write_text(script.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")

    assert FutuAdapter(missing)
    assert validate_option_quote_capability(missing).valid is False
    assert FutuAdapter(tampered).validate_request("get_accounts", ["--json"])
    assert validate_option_quote_capability(tampered).valid is False
    assert validate_option_quote_capability(_skill(tmp_path / "valid")).valid is True
    assert OPTION_QUOTE_SHA256 == "0ea054b6b8f6f37938885041b7257653627941408462fc9c3c53d0395d4c8fd8"


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        [_canonical_request()],
        [_canonical_request(), "--json", "extra"],
        [_canonical_request(), "--json", "--unknown"],
        [
            json.dumps([{"code": "US.AAPL260116C00150000", "action": "SELL", "quantity": 1.0}]),
            "--json",
        ],
        [
            json.dumps([{"code": "US.AAPL260116C00150000", "action": "BUY", "quantity": 2.0}]),
            "--json",
        ],
        [json.dumps([{"code": "SG.BAD", "action": "BUY", "quantity": 1.0}]), "--json"],
        [
            json.dumps(
                [
                    {"code": "US.AAPL260116C00150000", "action": "BUY", "quantity": 1.0},
                    {"code": "US.AAPL260116P00150000", "action": "BUY", "quantity": 1.0},
                ]
            ),
            "--json",
        ],
        [
            json.dumps(
                [{"code": "US.AAPL260116C00150000", "action": "BUY", "quantity": 1.0, "x": 1}]
            ),
            "--json",
        ],
    ],
)
def test_option_quote_rejects_every_noncanonical_or_unsafe_shape_before_subprocess(
    monkeypatch, tmp_path, arguments
):
    adapter = FutuAdapter(_skill(tmp_path))
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not run"))

    with pytest.raises(UnsafeFutuCommand):
        adapter.run("get_option_quote", arguments)


def test_option_quote_accepts_only_canonical_single_buy_unit_request(tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    request_json = _canonical_request("HK.TCH260522P330000")

    request = adapter.validate_request("get_option_quote", [request_json, "--json"])

    assert request.command == "get_option_quote"
    assert request.arguments == (request_json, "--json")
    assert "get_option_quote" in adapter.READ_ONLY_COMMANDS


def test_option_quote_capability_failure_is_structured_before_subprocess(monkeypatch, tmp_path):
    adapter = FutuAdapter(_skill(tmp_path, option_quote=False))
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not run"))

    with pytest.raises(UnsafeFutuCommand, match="capability is unavailable"):
        adapter.run("get_option_quote", [_canonical_request(), "--json"])


def test_option_quote_response_binds_identity_and_preserves_decimal_raw_fields():
    quote = parse_option_quote_response(
        "US.AAPL260116C00150000",
        {
            "data": [
                {
                    "code": "US.AAPL260116C00150000",
                    "delta": "0.1234567890123456789",
                    "gamma": "0.001",
                    "vega": "1.25",
                    "theta": "-0.5",
                    "rho": "0.75",
                    "option_type": "CALL",
                    "strike_price": "150.125",
                    "expire_time": "2026-01-16",
                    "implied_volatility": "22.75",
                    "price": 9.99,
                    "max_profit": 999999,
                }
            ]
        },
    )

    assert quote.identity == "US.AAPL260116C00150000"
    assert str(quote.greeks["delta"]) == "0.1234567890123456789"
    assert quote.option_type == "CALL"
    assert quote.expiry.isoformat() == "2026-01-16"
    assert str(quote.strike) == "150.125"
    assert str(quote.implied_volatility) == "22.75"
    assert "price" not in quote.to_dict()
    assert "max_profit" not in quote.to_dict()
    assert quote.to_dict()["unit"] == "RAW_CONTRACT_UNITS_NO_MULTIPLIER"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": [], "extra": 1},
        {"data": []},
        {"data": [{"code": "US.X"}, {"code": "US.X"}]},
        {"data": ["not-an-object"]},
        {"data": [{"code": "HK.WRONG", "delta": "1"}]},
    ],
)
def test_option_quote_response_rejects_non_singleton_or_unbound_rows(payload):
    with pytest.raises(OptionQuoteError):
        parse_option_quote_response("US.X", payload)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", float("nan")])
def test_each_nonfinite_greek_is_unavailable_without_poisoning_other_fields(value):
    quote = parse_option_quote_response(
        "US.X", {"data": [{"code": "US.X", "delta": value, "gamma": "0.25"}]}
    )

    assert quote.greeks["delta"] is None
    assert str(quote.greeks["gamma"]) == "0.25"


def test_missing_greeks_remain_missing_instead_of_becoming_zero():
    quote = parse_option_quote_response(
        "HK.TCH260522P330000",
        {"data": [{"code": "HK.TCH260522P330000", "delta": "-0.4"}]},
    )

    assert quote.greeks["delta"] is not None
    assert quote.greeks["gamma"] is None
    assert quote.option_type is None
    assert quote.expiry is None


def test_finite_but_unbounded_decimal_expansion_is_not_accepted():
    quote = parse_option_quote_response(
        "US.X", {"data": [{"code": "US.X", "delta": Decimal("1e100000")}]}
    )

    assert quote.greeks["delta"] is None


def test_futu_option_provider_emits_only_canonical_read_request():
    class Adapter:
        def __init__(self):
            self.calls = []

        def read_private_json(self, command, arguments, *, timeout):
            self.calls.append((command, arguments, timeout))
            return {"data": [{"code": "US.X", "delta": "0.5"}]}

    adapter = Adapter()
    provider = FutuOptionQuoteProvider(adapter)

    payload = provider.quote("US.X", timeout=5.0)

    assert payload["data"][0]["code"] == "US.X"
    assert adapter.calls == [
        (
            "get_option_quote",
            ('[{"code":"US.X","action":"BUY","quantity":1.0}]', "--json"),
            5.0,
        )
    ]


def test_adapter_parses_option_numbers_directly_as_decimal(monkeypatch, tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    monkeypatch.setattr(
        adapter,
        "_run_unsanitized",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout='{"data":[{"code":"US.X","delta":0.1234567890123456789,"gamma":1}]}',
            stderr="",
        ),
    )

    payload = adapter.read_private_json("get_option_quote", [_canonical_request("US.X"), "--json"])

    assert payload["data"][0]["delta"] == Decimal("0.1234567890123456789")
    assert payload["data"][0]["gamma"] == Decimal("1")


def test_option_quote_uses_its_smaller_256kib_response_budget(monkeypatch, tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    adapter.MAX_OPTION_RESPONSE_BYTES = 16
    monkeypatch.setattr(futu_adapter, "resource", None)

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            self.args = command
            self.stdout = io.BytesIO(b'{"data":[{"code":"US.X"}]}')
            self.stderr = io.BytesIO()
            self.returncode = 0

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)

    with pytest.raises(Exception, match="bounded response budget"):
        adapter.run("get_option_quote", [_canonical_request("US.X"), "--json"])
