import json
import os
import socket
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from portfolio_analyst import doctor, opend_probe_worker, settings
from portfolio_analyst.doctor import probe_environment, run_doctor
from portfolio_analyst.skill_trust import audited_files


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_SKILL = PROJECT_ROOT.parent / "futuapi"


def _trusted_skill(path):
    for record in audited_files():
        destination = path / record.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OFFICIAL_SKILL / record.relative_path, destination)
    return path


def test_skill_discovery_prefers_explicit_environment(monkeypatch, tmp_path):
    explicit = _trusted_skill(tmp_path / "futuapi")
    monkeypatch.setenv("FUTUAPI_SKILL_DIR", str(explicit))

    source = settings.discover_futuapi_skill_source(repo_root=tmp_path, global_roots=[])

    assert source.path == explicit.resolve()
    assert source.kind == "explicit"


def test_invalid_skill_override_falls_back_to_repository(monkeypatch, tmp_path):
    fallback = _trusted_skill(tmp_path / "Futu API Skills" / "futuapi")
    monkeypatch.setenv("FUTUAPI_SKILL_DIR", str(tmp_path / "missing"))

    assert (
        settings.discover_futuapi_skill(repo_root=tmp_path, global_roots=[]) == fallback.resolve()
    )


def test_worker_reports_broken_sdk_import(monkeypatch):
    def broken_import(_name):
        raise RuntimeError("native dependency failed")

    monkeypatch.setattr(opend_probe_worker.importlib, "import_module", broken_import)

    result = opend_probe_worker.collect_environment("127.0.0.1", 11111)

    assert result["sdk"]["available"] is False
    assert result["sdk"]["version"] is None
    assert "import failed" in result["sdk"]["detail"]
    assert result["opend"]["available"] is False


def test_worker_rejects_unrelated_tcp_listener_when_handshake_fails():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    host, port = listener.getsockname()

    class NotOpenDContext:
        def __init__(self, host, port):
            self.connection = socket.create_connection((host, port), timeout=0.5)

        def get_global_state(self):
            return -1, "not an OpenD protocol endpoint"

        def close(self):
            self.connection.close()

    fake_sdk = SimpleNamespace(OpenQuoteContext=NotOpenDContext, RET_OK=0)
    try:
        available, detail = opend_probe_worker.perform_opend_handshake(fake_sdk, host, port)
    finally:
        listener.close()

    assert available is False
    assert "handshake failed" in detail


def test_environment_probe_bounds_sdk_import_and_handshake_together():
    def hanging_worker(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["worker-importing-sdk"], timeout=2)

    result = probe_environment("127.0.0.1", 11111, runner=hanging_worker)

    assert result["sdk"]["available"] is False
    assert "deadline exceeded" in result["sdk"]["detail"]
    assert result["opend"]["available"] is False
    assert "unverified" in result["opend"]["detail"]


def test_environment_probe_parses_independent_sdk_and_opend_results():
    observed = {}
    expected = {
        "sdk": {"available": True, "version": "10.5.1", "detail": "SDK imported"},
        "opend": {"available": True, "detail": "quote handshake succeeded"},
    }

    def completed(args, **kwargs):
        observed["args"] = args
        observed.update(kwargs)
        output = f"SDK log\n{json.dumps(expected)}\n"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")

    result = probe_environment("127.0.0.1", 11111, runner=completed)

    assert result == expected
    assert observed["args"][0] == sys.executable
    assert observed["args"][1] == "-s"
    assert observed["args"][2] == str(doctor.OPEND_PROBE_WORKER)
    assert observed["timeout"] == doctor.DIAGNOSTIC_TIMEOUT_SECONDS
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "replace"


def test_environment_probe_uses_shared_controlled_environment(monkeypatch):
    safe_parent = {
        "HOME": "/runtime/home",
        "TEMP": "/runtime/temp",
        "FUTU_OPEND_HOST": "192.0.2.20",
        "FUTU_OPEND_PORT": "33333",
        "FUTU_TRD_ENV": "REAL",
        "FUTU_DEFAULT_MARKET": "US",
        "FUTU_SECURITY_FIRM": "FUTUINC",
        "FUTU_ACC_ID": "88776655",
    }
    unsafe_parent = {
        "FUTU_LOGIN_ACCOUNT": "private-login",
        "FUTU_LOGIN_PWD": "private-password",
        "API_TOKEN": "private-token",
        "CLIENT_SECRET": "private-secret",
        "PYTHONPATH": "/tmp/injected",
        "PYTHONHOME": "/tmp/runtime",
        "PYTHONUSERBASE": "/tmp/user-site",
        "LD_PRELOAD": "/tmp/injected.so",
    }
    for key, value in {**safe_parent, **unsafe_parent}.items():
        monkeypatch.setenv(key, value)
    observed = {}

    def completed(args, **kwargs):
        observed.update(kwargs)
        payload = {
            "sdk": {"available": False, "version": None, "detail": "missing"},
            "opend": {"available": False, "detail": "not attempted"},
        }
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    probe_environment("127.0.0.1", 11111, runner=completed)

    child_env = observed["env"]
    for key, value in safe_parent.items():
        assert child_env[key] == value
    assert child_env["PYTHONNOUSERSITE"] == "1"
    assert unsafe_parent.keys().isdisjoint(child_env)


def test_worker_launch_succeeds_from_unrelated_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    completed = subprocess.run(
        [sys.executable, str(doctor.OPEND_PROBE_WORKER)],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        timeout=2,
    )

    assert completed.returncode == 2
    assert "ModuleNotFoundError" not in completed.stderr
    assert json.loads(completed.stdout)["detail"] == "invalid diagnostic worker arguments"


def test_real_worker_ignores_parent_pythonpath_injection_from_unrelated_cwd(monkeypatch, tmp_path):
    injected = tmp_path / "injected"
    injected.mkdir()
    fake_futu = (
        "__version__ = 'injected-user-code'\n"
        "RET_OK = 0\n"
        "class OpenQuoteContext:\n"
        "    def __init__(self, **kwargs): pass\n"
        "    def get_global_state(self): return (RET_OK, {})\n"
        "    def close(self): pass\n"
    )
    (injected / "futu.py").write_text(fake_futu, encoding="utf-8")
    isolated_home = tmp_path / "isolated-home"
    site_env = dict(os.environ)
    for key in ("PYTHONUSERBASE", "APPDATA", "LOCALAPPDATA"):
        site_env.pop(key, None)
    site_env.update({"HOME": str(isolated_home), "USERPROFILE": str(isolated_home)})
    user_site_result = subprocess.run(
        [sys.executable, "-c", "import site; print(site.getusersitepackages())"],
        capture_output=True,
        check=True,
        encoding="utf-8",
        env=site_env,
    )
    user_site = Path(user_site_result.stdout.strip())
    assert user_site.is_relative_to(tmp_path)
    user_site.mkdir(parents=True)
    (user_site / "futu.py").write_text(fake_futu, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(injected))
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "ignored-user-base"))
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("USERPROFILE", str(isolated_home))
    monkeypatch.chdir(tmp_path)

    result = probe_environment("127.0.0.1", 11111, timeout=2)

    assert result["sdk"]["available"] is False
    assert result["sdk"]["version"] is None
    assert "injected-user-code" not in json.dumps(result)


def test_successful_handshake_with_close_failure_is_unavailable():
    class Context:
        def get_global_state(self):
            return 0, {"market_hk": "OPEN"}

        def close(self):
            raise RuntimeError("close failed")

    fake_sdk = SimpleNamespace(OpenQuoteContext=lambda **_kwargs: Context(), RET_OK=0)

    available, detail = opend_probe_worker.perform_opend_handshake(fake_sdk, "127.0.0.1", 11111)

    assert available is False
    assert "cleanup failed" in detail


def test_doctor_reports_independent_worker_results(tmp_path):
    skill = _trusted_skill(tmp_path / "futuapi")
    environment = {
        "sdk": {"available": False, "version": None, "detail": "SDK missing"},
        "opend": {"available": False, "detail": "not attempted; SDK unavailable"},
    }

    report = run_doctor(skill_dir=skill, environment_probe=lambda *_args: environment)

    assert report["ok"] is False
    assert report["checks"]["futuapi_skill"]["available"] is True
    assert report["checks"]["sdk"] == environment["sdk"]
    assert report["checks"]["opend"]["available"] is False
    assert report["checks"]["opend"]["detail"] == environment["opend"]["detail"]


def test_cli_doctor_json_supports_no_save(monkeypatch, tmp_path, capsys):
    data_home = tmp_path / "private-state"
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(data_home))

    from portfolio_analyst import cli

    expected = {
        "ok": False,
        "checks": {
            "futuapi_skill": {"available": True, "detail": "compatible"},
            "sdk": {"available": False, "detail": "missing"},
            "opend": {"available": False, "detail": "not checked"},
        },
    }
    monkeypatch.setattr(cli, "run_doctor", lambda **_kwargs: expected)

    exit_code = cli.main(["doctor", "--json", "--output", "doctor.json", "--no-save"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload == expected
    assert not (data_home / "doctor.json").exists()
