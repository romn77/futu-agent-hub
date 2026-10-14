import io
import json
from pathlib import Path
import shutil
import subprocess
from decimal import Decimal

import pytest

from portfolio_analyst import futu_adapter
from portfolio_analyst.futu_adapter import FutuAdapter, FutuReadError, UnsafeFutuCommand
from portfolio_analyst.option_quote import (
    OptionQuoteError,
    OptionQuoteProviderError,
    parse_option_quote_response,
)
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


@pytest.mark.parametrize("value", ["1e1000", "1e-1000"])
def test_fixed_point_overflow_is_rejected_independently_for_greek_strike_and_iv(value):
    quote = parse_option_quote_response(
        "US.X",
        {
            "data": [
                {
                    "code": "US.X",
                    "delta": value,
                    "gamma": "0.25",
                    "strike_price": value,
                    "implied_volatility": value,
                }
            ]
        },
    )

    assert quote.greeks["delta"] is None
    assert str(quote.greeks["gamma"]) == "0.25"
    assert quote.strike is None
    assert quote.implied_volatility is None


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


@pytest.mark.parametrize(
    ("failure", "reason_code"),
    [
        (subprocess.TimeoutExpired("quote", 5), "QUOTE_SOURCE_TIMEOUT"),
        (FutuReadError("audited Futu read failed"), "QUOTE_SOURCE_READ_FAILED"),
        (
            FutuReadError("audited Futu read exceeded the bounded response budget"),
            "QUOTE_RESPONSE_TOO_LARGE",
        ),
        (
            UnsafeFutuCommand("option quote capability is unavailable"),
            "QUOTE_CAPABILITY_UNAVAILABLE",
        ),
    ],
)
def test_futu_option_provider_maps_adapter_failures_to_stable_codes(failure, reason_code):
    class Adapter:
        def read_private_json(self, *args, **kwargs):
            del args, kwargs
            raise failure

    with pytest.raises(OptionQuoteProviderError) as caught:
        FutuOptionQuoteProvider(Adapter()).quote("US.X", timeout=5.0)

    assert caught.value.reason_code == reason_code
    assert str(caught.value) == reason_code


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


def test_sdk_quote_without_code_is_bound_to_audited_single_request(monkeypatch, tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    seen = []

    def run(name, arguments, **kwargs):
        seen.append(adapter.validate_request(name, arguments))
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"data":[{"delta":0.1234567890123456789,"option_type":"CALL","expire_time":"2026-01-16","strike_price":150}]}',
            stderr="",
        )

    monkeypatch.setattr(adapter, "_run_unsanitized", run)
    identity = "US.AAPL260116C00150000"
    payload = FutuOptionQuoteProvider(adapter).quote(identity, timeout=5.0)
    quote = parse_option_quote_response(identity, payload)

    assert seen[0].arguments == (_canonical_request(identity), "--json")
    assert "code" not in payload["data"][0]  # Do not invent a vendor response field.
    assert payload["request_binding"] == {
        "command": "get_option_quote",
        "code": identity,
        "action": "BUY",
        "quantity": "1.0",
    }
    assert quote.identity == identity
    assert quote.greeks["delta"] == Decimal("0.1234567890123456789")


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"delta": "0.5"}]},
        {"data": [{"code": None, "delta": "0.5"}]},
    ],
)
def test_bare_quote_without_matching_code_remains_unbound(payload):
    with pytest.raises(OptionQuoteError):
        parse_option_quote_response("US.X", payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", "US.WRONG"),
        ("command", "get_market_snapshot"),
        ("action", "SELL"),
        ("quantity", "2.0"),
        ("extra", True),
    ],
)
def test_quote_request_binding_cannot_change_single_contract_request(field, value):
    binding = {"command": "get_option_quote", "code": "US.X", "action": "BUY", "quantity": "1.0"}
    binding[field] = value
    with pytest.raises(OptionQuoteError):
        parse_option_quote_response(
            "US.X", {"data": [{"delta": "0.5"}], "request_binding": binding}
        )


def test_bound_request_does_not_override_conflicting_response_code():
    binding = {"command": "get_option_quote", "code": "US.X", "action": "BUY", "quantity": "1.0"}
    with pytest.raises(OptionQuoteError):
        parse_option_quote_response(
            "US.X", {"data": [{"code": "US.WRONG", "delta": "0.5"}], "request_binding": binding}
        )


def test_sdk_quote_request_binding_survives_report_semantic_replay(monkeypatch, tmp_path):
    from copy import deepcopy
    from portfolio_analyst.option_risk import analyze_option_risk
    from portfolio_analyst.schema import ReportSemanticError, validate_option_risk_document
    from test_option_risk import Clock, _position, _snapshot
    from test_option_risk_schema import _rehash

    adapter = FutuAdapter(_skill(tmp_path))
    monkeypatch.setattr(
        adapter,
        "_run_unsanitized",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"data":[{"delta":0.5,"gamma":0.1,"vega":2,"theta":-0.25,"rho":0.75,"option_type":"CALL","expire_time":"2026-01-16","strike_price":150}]}',
            stderr="",
        ),
    )
    result = analyze_option_risk(
        _snapshot((_position("AAPL260116C00150000"),)),
        quote_provider=FutuOptionQuoteProvider(adapter),
        clock=Clock(),
    ).to_dict()
    item = result["quote_evidence"][0]
    assert item["status"] == "SUCCESS"
    assert "code" not in item["source_response"]["data"][0]
    assert item["source_response"]["request_binding"]["code"] == item["identity"]
    assert item["response"]["greeks"]["delta"] == "0.5"
    validate_option_risk_document(result)

    for mutate in ("remove_binding", "wrong_identity"):
        damaged = deepcopy(result)
        source = damaged["quote_evidence"][0]["source_response"]
        if mutate == "remove_binding":
            source.pop("request_binding")
        else:
            source["request_binding"]["code"] = "US.WRONG"
        from portfolio_analyst.option_risk import _bounded_response_bytes

        damaged["quote_evidence"][0]["response_byte_count"] = len(_bounded_response_bytes(source))
        _rehash(damaged)
        with pytest.raises(ReportSemanticError):
            validate_option_risk_document(damaged)


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": [{"delta": "0.5"}, {"delta": "0.5"}]},
        {"data": [{"delta": "0.5"}], "extra": True},
        {"data": [{"code": None, "delta": "0.5"}]},
        {"data": [{"code": "US.WRONG", "delta": "0.5"}]},
        {
            "data": [{"delta": "0.5"}],
            "request_binding": {
                "command": "get_option_quote",
                "code": "US.AAPL260116C00150000",
                "action": "BUY",
                "quantity": "1.0",
            },
        },
    ],
)
def test_sdk_provider_never_rebinds_invalid_or_prebound_source_response(payload):
    from portfolio_analyst.option_risk import analyze_option_risk
    from portfolio_analyst.schema import validate_option_risk_document
    from test_option_risk import Clock, _position, _snapshot

    class Adapter:
        def read_private_json(self, *args, **kwargs):
            return payload

    result = analyze_option_risk(
        _snapshot((_position("AAPL260116C00150000"),)),
        quote_provider=FutuOptionQuoteProvider(Adapter()),
        clock=Clock(),
    ).to_dict()
    item = result["quote_evidence"][0]
    assert item["status"] == "FAILURE"
    assert item["reason_code"] == "QUOTE_RESPONSE_CONTRACT_INVALID"
    assert item["response"] is None
    validate_option_risk_document(result)


@pytest.mark.parametrize("change", ["extra", "numeric_quantity", "wrong_code", "multirow"])
def test_malformed_binding_stays_invalid_after_evidence_normalization(change):
    from portfolio_analyst.option_risk import analyze_option_risk
    from portfolio_analyst.schema import validate_option_risk_document
    from test_option_risk import Clock, _position, _snapshot

    payload = {
        "data": [{"delta": "0.5"}],
        "request_binding": {
            "command": "get_option_quote",
            "code": "US.AAPL260116C00150000",
            "action": "BUY",
            "quantity": "1.0",
        },
    }
    if change == "extra":
        payload["request_binding"]["extra"] = "invalid"
    elif change == "numeric_quantity":
        payload["request_binding"]["quantity"] = 1.0
    elif change == "wrong_code":
        payload["request_binding"]["code"] = "US.WRONG"
    else:
        payload["data"].append({"delta": "0.5"})

    class Provider:
        def quote(self, identity, *, timeout):
            return payload

    result = analyze_option_risk(
        _snapshot((_position("AAPL260116C00150000"),)),
        quote_provider=Provider(),
        clock=Clock(),
    ).to_dict()
    assert result["quote_evidence"][0]["status"] == "FAILURE"
    assert result["quote_evidence"][0]["reason_code"] == "QUOTE_RESPONSE_CONTRACT_INVALID"
    validate_option_risk_document(result)
