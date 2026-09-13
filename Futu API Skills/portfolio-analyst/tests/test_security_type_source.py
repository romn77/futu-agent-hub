import json
import subprocess

import pytest


def test_static_worker_only_queries_exact_codes_and_always_closes():
    from portfolio_analyst.security_type_source import collect_security_types

    calls = []

    class Table:
        def to_dict(self, orient):
            assert orient == "records"
            return [{"code": "US.AAPL", "stock_type": "STOCK", "private": "discard"}]

    class Context:
        def get_stock_basicinfo(self, market, *, code_list):
            calls.append((market, code_list))
            return 0, Table()

        def close(self):
            calls.append("close")

    result = collect_security_types(
        ["US.AAPL"], host="127.0.0.1", port=11111, context_factory=lambda **_: Context()
    )
    assert result == {"data": [{"code": "US.AAPL", "stock_type": "STOCK"}]}
    assert calls == [("US", ["US.AAPL"]), "close"]


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"code": "US.OTHER", "stock_type": "STOCK"}]},
        {"data": [{"code": "US.AAPL", "stock_type": "STOCK"}] * 2},
        {"data": [{"code": "US.AAPL", "stock_type": None}]},
    ],
)
def test_static_provider_rejects_invalid_or_unrequested_rows(payload):
    from portfolio_analyst.security_type_source import FutuSecurityTypeProvider

    provider = FutuSecurityTypeProvider(
        runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(payload).encode(), b"")
    )
    with pytest.raises(ValueError, match="SECURITY_METADATA"):
        provider.metadata(["US.AAPL"], timeout=5)


def test_static_provider_timeout_is_sanitized():
    from portfolio_analyst.security_type_source import FutuSecurityTypeProvider

    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired("secret path", 5)

    with pytest.raises(ValueError, match="SECURITY_METADATA_TIMEOUT") as caught:
        FutuSecurityTypeProvider(runner=runner).metadata(["US.AAPL"], timeout=5)
    assert "secret" not in str(caught.value)


def test_static_provider_never_inherits_credentials_or_python_hooks(monkeypatch):
    from portfolio_analyst.security_type_source import FutuSecurityTypeProvider

    monkeypatch.setenv("FUTU_TRADE_PWD", "secret")
    monkeypatch.setenv("PYTHONPATH", "hostile")

    def runner(command, **kwargs):
        assert "FUTU_TRADE_PWD" not in kwargs["env"]
        assert "PYTHONPATH" not in kwargs["env"]
        assert command[1] == "-s" and kwargs["shell"] is False
        assert json.loads(kwargs["input"])["identities"] == ["US.AAPL"]
        return subprocess.CompletedProcess(command, 0, b'{"data":[]}', b"")

    assert FutuSecurityTypeProvider(runner=runner).metadata(["US.AAPL"], timeout=5) == {"data": []}


def test_static_worker_closes_after_source_failure():
    from portfolio_analyst.security_type_source import collect_security_types

    closed = []

    class Context:
        def get_stock_basicinfo(self, *args, **kwargs):
            raise RuntimeError("source error")

        def close(self):
            closed.append(True)

    with pytest.raises(RuntimeError):
        collect_security_types(
            ["US.AAPL"], host="127.0.0.1", port=11111, context_factory=lambda **_: Context()
        )
    assert closed == [True]


@pytest.mark.parametrize(
    "identities", [[], ["US.AAPL"] * 2, ["US.AAPL;cat"], [f"US.S{i}" for i in range(201)]]
)
def test_invalid_static_request_never_starts_process(identities):
    from portfolio_analyst.security_type_source import FutuSecurityTypeProvider

    def runner(*args, **kwargs):
        pytest.fail("invalid request must not start a process")

    with pytest.raises(ValueError, match="REQUEST_INVALID"):
        FutuSecurityTypeProvider(runner=runner).metadata(identities, timeout=5)
