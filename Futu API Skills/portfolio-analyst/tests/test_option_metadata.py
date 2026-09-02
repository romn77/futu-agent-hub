from __future__ import annotations

import json
import subprocess
import sys

import pytest

from portfolio_analyst.option_metadata import (
    FutuOptionMetadataProvider,
    OptionMetadataContractError,
    OptionMetadataProviderError,
    parse_option_metadata_response,
)
from portfolio_analyst.option_metadata_worker import _parse_request, collect_option_metadata


class _Table:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return self._rows


class _QuoteContext:
    def __init__(self, calls, rows):
        self.calls = calls
        self.rows = rows
        self.closed = False

    def get_stock_basicinfo(self, market, security_type, code_list):
        self.calls.append((market, security_type, tuple(code_list)))
        return 0, _Table([row for row in self.rows if row["code"] in code_list])

    def close(self):
        self.closed = True


def test_worker_batches_only_us_and_hk_and_returns_allowlisted_fields():
    calls = []
    contexts = []
    rows = [
        {
            "code": "US.AAPL260116C00150000",
            "lot_size": 100,
            "stock_owner": "US.AAPL",
            "option_type": "CALL",
            "strike_time": "2026-01-16",
            "strike_price": 150.0,
            "secret": "must not cross boundary",
        },
        {
            "code": "HK.00700260116P00300000",
            "lot_size": 100,
            "stock_owner": "HK.00700",
            "option_type": "PUT",
            "strike_time": "2026-01-16",
            "strike_price": 300,
            "secret": "must not cross boundary",
        },
    ]

    def factory():
        context = _QuoteContext(calls, rows)
        contexts.append(context)
        return context

    result = collect_option_metadata(
        ["HK.00700260116P00300000", "US.AAPL260116C00150000"],
        context_factory=factory,
        option_security_type="OPTION",
    )

    assert calls == [
        ("HK", "OPTION", ("HK.00700260116P00300000",)),
        ("US", "OPTION", ("US.AAPL260116C00150000",)),
    ]
    assert all(context.closed for context in contexts)
    assert result == {"data": [
        {
            "code": "HK.00700260116P00300000",
            "lot_size": 100,
            "stock_owner": "HK.00700",
            "option_type": "PUT",
            "strike_time": "2026-01-16",
            "strike_price": 300,
        },
        {
            "code": "US.AAPL260116C00150000",
            "lot_size": 100,
            "stock_owner": "US.AAPL",
            "option_type": "CALL",
            "strike_time": "2026-01-16",
            "strike_price": 150.0,
        },
    ]}


def test_worker_normalizes_dataframe_scalar_values_for_json_boundary():
    class Scalar:
        def __init__(self, value):
            self.value = value

        def item(self):
            return self.value

    rows = [{
        "code": "US.A", "lot_size": Scalar(100), "stock_owner": "US.AAPL",
        "option_type": "CALL", "strike_time": "2026-01-16", "strike_price": Scalar(150.0),
    }]
    context = _QuoteContext([], rows)

    result = collect_option_metadata(
        ["US.A"], context_factory=lambda: context, option_security_type="OPTION"
    )

    json.dumps(result, allow_nan=False)
    assert result["data"][0]["lot_size"] == 100


@pytest.mark.parametrize(
    "identities",
    [[], ["SG.BAD"], ["US.A"] * 31, ["US.A", "US.A"], ["US.A", 3]],
)
def test_worker_rejects_noncanonical_or_unbounded_requests_before_context(identities):
    with pytest.raises(ValueError, match="^OPTION_METADATA_REQUEST_INVALID$"):
        collect_option_metadata(identities, context_factory=lambda: pytest.fail("no context"))


def test_worker_requires_exact_canonical_json_bytes():
    assert _parse_request(b'["HK.B","US.A"]') == ["HK.B", "US.A"]
    for raw in (b'[ "US.A" ]', b'["US.A","HK.B"]', b'{"codes":["US.A"]}'):
        with pytest.raises(ValueError, match="^OPTION_METADATA_REQUEST_INVALID$"):
            _parse_request(raw)


class _Completed:
    def __init__(self, *, stdout=b"", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_parent_uses_isolated_python_canonical_input_and_controlled_environment():
    observed = {}

    def runner(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return _Completed(
            stdout=json.dumps({"data": [{
                "code": "US.AAPL260116C00150000", "lot_size": 100,
                "stock_owner": "US.AAPL", "option_type": "CALL",
                "strike_time": "2026-01-16", "strike_price": 150,
            }]}).encode()
        )

    provider = FutuOptionMetadataProvider(runner=runner)
    payload = provider.metadata(["US.AAPL260116C00150000"], timeout=5.0)

    assert observed["command"][:2] == [sys.executable, "-s"]
    assert observed["shell"] is False
    assert observed["timeout"] == 5.0
    assert observed["input"] == b'["US.AAPL260116C00150000"]'
    assert set(observed["env"]) == {"PATH", "PYTHONIOENCODING", "PYTHONNOUSERSITE"}
    assert payload["data"][0]["lot_size"] == 100


@pytest.mark.parametrize(
    ("runner", "reason"),
    [
        (lambda *_a, **_k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 5)), "OPTION_METADATA_TIMEOUT"),
        (lambda *_a, **_k: _Completed(stdout=b"x" * (256 * 1024 + 1)), "OPTION_METADATA_RESPONSE_TOO_LARGE"),
        (lambda *_a, **_k: _Completed(stdout=b"{"), "OPTION_METADATA_RESPONSE_INVALID"),
        (lambda *_a, **_k: _Completed(returncode=2, stderr=b"private failure"), "OPTION_METADATA_SOURCE_UNAVAILABLE"),
    ],
)
def test_parent_classifies_failures_without_exposing_raw_errors(runner, reason):
    provider = FutuOptionMetadataProvider(runner=runner)
    with pytest.raises(OptionMetadataProviderError) as caught:
        provider.metadata(["US.A"], timeout=5.0)
    assert caught.value.reason_code == reason
    assert str(caught.value) == reason


def test_parent_classifies_invalid_request_before_starting_worker():
    provider = FutuOptionMetadataProvider(runner=lambda *_a, **_k: pytest.fail("no process"))
    with pytest.raises(OptionMetadataProviderError) as caught:
        provider.metadata(["SG.BAD"], timeout=5.0)
    assert caught.value.reason_code == "OPTION_METADATA_REQUEST_INVALID"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": [{"code": "US.WRONG", "lot_size": 100, "stock_owner": "US.AAPL", "option_type": "CALL", "strike_time": "2026-01-16", "strike_price": 150}]},
        {"data": [{"code": "US.A", "lot_size": 100, "stock_owner": "US.AAPL", "option_type": "CALL", "strike_time": "2026-01-16", "strike_price": 150, "extra": 1}]},
        {"data": [{"code": "US.A", "lot_size": 0, "stock_owner": "US.AAPL", "option_type": "CALL", "strike_time": "2026-01-16", "strike_price": 150}]},
        {"data": [{"code": "US.A", "lot_size": 100, "stock_owner": "HK.00700", "option_type": "CALL", "strike_time": "2026-01-16", "strike_price": 150}]},
    ],
)
def test_parser_rejects_malformed_identity_mismatch_and_invalid_fields(payload):
    with pytest.raises(OptionMetadataContractError):
        parse_option_metadata_response(["US.A"], payload)
