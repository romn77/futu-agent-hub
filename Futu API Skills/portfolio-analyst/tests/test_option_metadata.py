from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from portfolio_analyst.option_metadata import (
    FutuOptionMetadataProvider,
    MAX_METADATA_RESPONSE_BYTES,
    OptionMetadataContractError,
    OptionMetadataProviderError,
    _POSIX_PROCESS_GROUPS,
    _bounded_subprocess_run,
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

    def factory(*, host, port):
        assert (host, port) == ("10.0.0.8", 22222)
        context = _QuoteContext(calls, rows)
        contexts.append(context)
        return context

    result = collect_option_metadata(
        ["HK.00700260116P00300000", "US.AAPL260116C00150000"],
        context_factory=factory,
        option_security_type="OPTION",
        host="10.0.0.8",
        port=22222,
    )

    assert calls == [
        ("HK", "OPTION", ("HK.00700260116P00300000",)),
        ("US", "OPTION", ("US.AAPL260116C00150000",)),
    ]
    assert all(context.closed for context in contexts)
    assert result == {
        "data": [
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
        ]
    }


def test_worker_normalizes_dataframe_scalar_values_for_json_boundary():
    class Scalar:
        def __init__(self, value):
            self.value = value

        def item(self):
            return self.value

    rows = [
        {
            "code": "US.A",
            "lot_size": Scalar(100),
            "stock_owner": "US.AAPL",
            "option_type": "CALL",
            "strike_time": "2026-01-16",
            "strike_price": Scalar(150.0),
        }
    ]
    context = _QuoteContext([], rows)

    result = collect_option_metadata(
        ["US.A"],
        context_factory=lambda **_endpoint: context,
        option_security_type="OPTION",
        host="127.0.0.1",
        port=11111,
    )

    json.dumps(result, allow_nan=False)
    assert result["data"][0]["lot_size"] == 100


@pytest.mark.parametrize(
    "identities",
    [[], ["SG.BAD"], ["US.A"] * 31, ["US.A", "US.A"], ["US.A", 3]],
)
def test_worker_rejects_noncanonical_or_unbounded_requests_before_context(identities):
    with pytest.raises(ValueError, match="^OPTION_METADATA_REQUEST_INVALID$"):
        collect_option_metadata(
            identities,
            context_factory=lambda **_endpoint: pytest.fail("no context"),
            host="127.0.0.1",
            port=11111,
        )


def test_worker_requires_exact_canonical_json_endpoint_request():
    raw = b'{"host":"10.0.0.8","identities":["HK.B","US.A"],"port":22222}'
    assert _parse_request(raw) == ("10.0.0.8", 22222, ["HK.B", "US.A"])
    for raw in (
        b'{ "host":"10.0.0.8","identities":["US.A"],"port":22222}',
        b'{"host":"10.0.0.8","identities":["US.A","HK.B"],"port":22222}',
        b'{"host":"bad/host","identities":["US.A"],"port":22222}',
        b'{"host":"10.0.0.8","identities":["US.A"],"port":0}',
        b'["US.A"]',
    ):
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
            stdout=json.dumps(
                {
                    "data": [
                        {
                            "code": "US.AAPL260116C00150000",
                            "lot_size": 100,
                            "stock_owner": "US.AAPL",
                            "option_type": "CALL",
                            "strike_time": "2026-01-16",
                            "strike_price": 150,
                        }
                    ]
                }
            ).encode()
        )

    provider = FutuOptionMetadataProvider(host="10.0.0.8", port=22222, runner=runner)
    payload = provider.metadata(["US.AAPL260116C00150000"], timeout=5.0)

    assert observed["command"][:2] == [sys.executable, "-s"]
    assert observed["shell"] is False
    assert observed["timeout"] == 5.0
    assert observed["input"] == (
        b'{"host":"10.0.0.8","identities":["US.AAPL260116C00150000"],"port":22222}'
    )
    assert set(observed["env"]) == {"PATH", "PYTHONIOENCODING", "PYTHONNOUSERSITE"}
    assert payload["data"][0]["lot_size"] == 100


@pytest.mark.parametrize(
    ("runner", "reason"),
    [
        (
            lambda *_a, **_k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 5)),
            "OPTION_METADATA_TIMEOUT",
        ),
        (
            lambda *_a, **_k: _Completed(stdout=b"x" * (256 * 1024 + 1)),
            "OPTION_METADATA_RESPONSE_TOO_LARGE",
        ),
        (lambda *_a, **_k: _Completed(stdout=b"{"), "OPTION_METADATA_RESPONSE_INVALID"),
        (
            lambda *_a, **_k: _Completed(returncode=2, stderr=b"private failure"),
            "OPTION_METADATA_SOURCE_UNAVAILABLE",
        ),
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


@pytest.mark.parametrize(("host", "port"), [("bad/host", 11111), ("localhost", 0), ("", 11111)])
def test_parent_rejects_invalid_endpoint_before_starting_worker(host, port):
    provider = FutuOptionMetadataProvider(
        host=host,
        port=port,
        runner=lambda *_a, **_k: pytest.fail("no process"),
    )
    with pytest.raises(OptionMetadataProviderError) as caught:
        provider.metadata(["US.A"], timeout=5.0)
    assert caught.value.reason_code == "OPTION_METADATA_ENDPOINT_INVALID"


def test_parent_enforces_combined_stdout_stderr_budget_for_injected_runner():
    provider = FutuOptionMetadataProvider(
        runner=lambda *_a, **_k: _Completed(
            stdout=b"x" * (200 * 1024), stderr=b"private" * (15 * 1024)
        )
    )
    with pytest.raises(OptionMetadataProviderError) as caught:
        provider.metadata(["US.A"], timeout=5.0)
    assert caught.value.reason_code == "OPTION_METADATA_RESPONSE_TOO_LARGE"


@pytest.mark.skipif(not _POSIX_PROCESS_GROUPS, reason="POSIX process-group contract")
def test_posix_process_group_bounded_runner_kills_during_combined_output(tmp_path):
    marker = tmp_path / "should-not-exist"
    program = (
        "import os,sys\n"
        "chunk=b'x'*65536\n"
        "for _ in range(64):\n"
        " os.write(1,chunk); os.write(2,chunk)\n"
        f"open({str(marker)!r},'w').write('unbounded')\n"
    )

    completed = _bounded_subprocess_run(
        [sys.executable, "-c", program],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5.0,
        shell=False,
        env={"PATH": ""},
        check=False,
    )

    assert len(completed.stdout) + len(completed.stderr) == MAX_METADATA_RESPONSE_BYTES + 1
    assert not marker.exists()


@pytest.mark.skipif(not _POSIX_PROCESS_GROUPS, reason="POSIX process-group contract")
def test_posix_process_group_bounded_runner_enforces_timeout():
    with pytest.raises(subprocess.TimeoutExpired):
        _bounded_subprocess_run(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=0.05,
            shell=False,
            env={"PATH": ""},
            check=False,
        )


@pytest.mark.skipif(not _POSIX_PROCESS_GROUPS, reason="POSIX process-group contract")
def test_bounded_runner_timeout_retains_captured_execution_evidence():
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        _bounded_subprocess_run(
            [
                sys.executable,
                "-c",
                "import os,time; os.write(1,b'event'); os.write(2,b'diagnostic'); time.sleep(2)",
            ],
            timeout=0.2,
            shell=False,
        )
    assert caught.value.output == b"event"
    assert caught.value.stderr == b"diagnostic"


@pytest.mark.skipif(not _POSIX_PROCESS_GROUPS, reason="POSIX process-group contract")
def test_bounded_runner_accepts_explicit_larger_eval_budget():
    completed = _bounded_subprocess_run(
        [sys.executable, "-c", "import os; os.write(1,b'x'*300000)"],
        timeout=5,
        max_output_bytes=400000,
        shell=False,
    )
    assert completed.returncode == 0
    assert len(completed.stdout) == 300000


@pytest.mark.parametrize("budget", [0, -1, True, 8 * 1024 * 1024 + 1])
@pytest.mark.skipif(not _POSIX_PROCESS_GROUPS, reason="POSIX process-group contract")
def test_bounded_runner_rejects_invalid_budget_before_process_start(budget):
    with pytest.raises(ValueError, match="output budget"):
        _bounded_subprocess_run(["must-not-exist"], timeout=1, max_output_bytes=budget)


def _pid_is_live(pid: int) -> bool:
    # SIGKILL is asynchronous: allow bounded time for delivery and OS reaping.
    deadline = time.monotonic() + 0.5
    while _pid_present(pid):
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.005)
    return False


def _pid_present(pid: int) -> bool:
    stat_path = f"/proc/{pid}/stat"
    try:
        state = open(stat_path, encoding="utf-8").read().split()[2]
    except FileNotFoundError:
        state = None
    if state == "Z":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(not _POSIX_PROCESS_GROUPS, reason="POSIX process-group contract")
def test_posix_process_group_timeout_kills_descendant_with_inherited_pipes(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    parent_program = (
        "import pathlib,subprocess,sys\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(2)'])\n"
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
    )

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _bounded_subprocess_run(
            [sys.executable, "-c", parent_program],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=0.15,
            shell=False,
            env={"PATH": ""},
            check=False,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    child_pid = int(child_pid_file.read_text())
    assert not _pid_is_live(child_pid)


@pytest.mark.skipif(not _POSIX_PROCESS_GROUPS, reason="POSIX process-group contract")
def test_posix_process_group_overflow_kills_descendant_with_inherited_pipes(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    marker = tmp_path / "should-not-exist"
    child_program = (
        "import os,time\n"
        "chunk=b'x'*65536\n"
        "for _ in range(64):\n"
        " os.write(1,chunk); os.write(2,chunk)\n"
        f"open({str(marker)!r},'w').write('unbounded')\n"
        "time.sleep(2)\n"
    )
    parent_program = (
        "import pathlib,subprocess,sys\n"
        f"child=subprocess.Popen([sys.executable,'-c',{child_program!r}])\n"
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
    )

    started = time.monotonic()
    completed = _bounded_subprocess_run(
        [sys.executable, "-c", parent_program],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5.0,
        shell=False,
        env={"PATH": ""},
        check=False,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert len(completed.stdout) + len(completed.stderr) == MAX_METADATA_RESPONSE_BYTES + 1
    assert not marker.exists()
    child_pid = int(child_pid_file.read_text())
    assert not _pid_is_live(child_pid)


def test_bounded_runner_fails_closed_when_process_tree_is_not_supported(monkeypatch):
    import portfolio_analyst.option_metadata as module

    monkeypatch.setattr(module, "_POSIX_PROCESS_GROUPS", False, raising=False)
    provider = FutuOptionMetadataProvider()

    with pytest.raises(OptionMetadataProviderError) as caught:
        provider.metadata(["US.A"], timeout=0.1)

    assert caught.value.reason_code == "OPTION_METADATA_PROCESS_TREE_UNSUPPORTED"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {
            "data": [
                {
                    "code": "US.WRONG",
                    "lot_size": 100,
                    "stock_owner": "US.AAPL",
                    "option_type": "CALL",
                    "strike_time": "2026-01-16",
                    "strike_price": 150,
                }
            ]
        },
        {
            "data": [
                {
                    "code": "US.A",
                    "lot_size": 100,
                    "stock_owner": "US.AAPL",
                    "option_type": "CALL",
                    "strike_time": "2026-01-16",
                    "strike_price": 150,
                    "extra": 1,
                }
            ]
        },
        {
            "data": [
                {
                    "code": "US.A",
                    "lot_size": 0,
                    "stock_owner": "US.AAPL",
                    "option_type": "CALL",
                    "strike_time": "2026-01-16",
                    "strike_price": 150,
                }
            ]
        },
        {
            "data": [
                {
                    "code": "US.A",
                    "lot_size": 100,
                    "stock_owner": "HK.00700",
                    "option_type": "CALL",
                    "strike_time": "2026-01-16",
                    "strike_price": 150,
                }
            ]
        },
    ],
)
def test_parser_rejects_malformed_identity_mismatch_and_invalid_fields(payload):
    with pytest.raises(OptionMetadataContractError):
        parse_option_metadata_response(["US.A"], payload)
