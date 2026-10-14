import json
from pathlib import Path
import subprocess

import pytest

from portfolio_analyst.option_metadata import _POSIX_PROCESS_GROUPS


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

    safe = {"HOME": "/safe/home", "USERPROFILE": "C:/safe/home", "SYSTEMROOT": "C:/Windows"}
    forbidden = (
        "FUTU_TRADE_PWD",
        "FUTU_LOGIN_PWD",
        "FUTU_ACC_ID",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
    )
    for key, value in safe.items():
        monkeypatch.setenv(key, value)
    for key in forbidden:
        monkeypatch.setenv(key, "must-not-inherit")

    def runner(command, **kwargs):
        assert safe.items() <= kwargs["env"].items()
        assert not set(forbidden) & kwargs["env"].keys()
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


@pytest.mark.skipif(not _POSIX_PROCESS_GROUPS, reason="POSIX process-group contract")
@pytest.mark.parametrize("provider_kind", ["security_type", "option"])
@pytest.mark.parametrize("sdk_logs", [False, True])
def test_metadata_worker_preserves_sdk_runtime_and_json_output(
    provider_kind, sdk_logs, tmp_path, monkeypatch
):
    from portfolio_analyst import option_metadata_worker, security_type_source
    from portfolio_analyst.option_metadata import (
        FutuOptionMetadataProvider,
        _bounded_subprocess_run,
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    module = security_type_source if provider_kind == "security_type" else option_metadata_worker
    worker = tmp_path / Path(module.__file__).name
    worker.write_text(Path(module.__file__).read_text())
    diagnostics = (
        "import atexit, logging, sys\n"
        "logger = logging.getLogger('sdk')\n"
        "logger.addHandler(logging.StreamHandler(sys.stdout))\n"
        "logger.warning('SDK import diagnostic')\n"
        "atexit.register(lambda: logger.warning('SDK shutdown diagnostic'))\n"
        if sdk_logs
        else ""
    )
    (tmp_path / "futu.py").write_text(
        diagnostics + "import os\n"
        "os.environ['HOME']\n"
        "class Market:\n"
        "    US = 'US'\n"
        "    HK = 'HK'\n"
        "class SecurityType:\n"
        "    DRVT = 'DRVT'\n"
        "class Table:\n"
        "    def to_dict(self, orient):\n"
        "        return [{'code': 'US.AAPL', 'stock_type': 'STOCK', 'lot_size': 100,\n"
        "                 'stock_owner': 'US.AAPL', 'option_type': 'CALL',\n"
        "                 'strike_time': '2026-01-16', 'strike_price': 150}]\n"
        "class OpenQuoteContext:\n"
        "    def __init__(self, **kwargs): pass\n"
        "    def get_stock_basicinfo(self, *args, **kwargs):\n"
        "        if len(args) == 3: assert args[1] == 'DRVT'\n"
        "        return 0, Table()\n"
        "    def close(self): pass\n"
    )
    completed = []

    def runner(*args, **kwargs):
        result = _bounded_subprocess_run(*args, **kwargs)
        completed.append(result)
        return result

    if provider_kind == "security_type":
        monkeypatch.setattr(security_type_source, "__file__", str(worker))
        provider = security_type_source.FutuSecurityTypeProvider(runner=runner)
        expected = {"data": [{"code": "US.AAPL", "stock_type": "STOCK"}]}
    else:
        provider = FutuOptionMetadataProvider(worker_path=worker, runner=runner)
        expected = {
            "data": [
                {
                    "code": "US.AAPL",
                    "lot_size": 100,
                    "stock_owner": "US.AAPL",
                    "option_type": "CALL",
                    "strike_time": "2026-01-16",
                    "strike_price": "150",
                }
            ]
        }

    assert provider.metadata(["US.AAPL"], timeout=5) == expected
    assert json.loads(completed[0].stdout)["data"][0]["code"] == "US.AAPL"
    if sdk_logs:
        assert b"SDK import diagnostic" in completed[0].stderr
        assert b"SDK shutdown diagnostic" in completed[0].stderr
