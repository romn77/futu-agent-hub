import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from portfolio_analyst.current_analysis import (
    CliResult,
    CurrentAnalysisError,
    OwnedTemporaryRun,
    TemporaryCleanupError,
    main,
    run_current_analysis,
)
from portfolio_analyst.private_home import PrivateHomeError
from portfolio_analyst.settings import Settings, SettingsError


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX private-temp contract")


def _snapshot(*, held_options: bool = False):
    from test_option_risk import _position, _snapshot as make_snapshot

    positions = [_position("AAA", security_type="STOCK")]
    if held_options:
        positions.append(_position("AAPL260116C00150000"))
    return make_snapshot(tuple(positions)).to_dict()


def _component(command, *, held_options=False, risk_available=True):
    from portfolio_analyst.snapshot_io import snapshot_from_document
    from portfolio_analyst.risk import analyze_risk
    from portfolio_analyst.technical import analyze_technical
    from portfolio_analyst.option_risk import analyze_option_risk

    source = snapshot_from_document(_snapshot(held_options=held_options))
    risk = analyze_risk(source).to_dict() if risk_available else None
    if command == "risk":
        body = risk
    elif command == "technical":
        body = analyze_technical(source, provider=None, risk_document=risk).to_dict()
    elif command == "options":
        body = analyze_option_risk(source).to_dict()
    else:
        raise AssertionError(command)
    return CliResult(0, json.dumps(body), "")


def _private_home(tmp_path: Path) -> Path:
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    return home


@pytest.mark.parametrize(
    ("held_options", "failing_component", "error_code"),
    [
        (False, "risk", "UNSUPPORTED_PORTFOLIO"),
        (True, "options", "SOURCE_UNAVAILABLE"),
    ],
)
def test_component_failure_is_local_and_cleanup_still_runs(
    tmp_path, held_options, failing_component, error_code
):
    home = _private_home(tmp_path)

    def invoke(arguments):
        command = arguments[0]
        if command == "snapshot":
            return CliResult(0, json.dumps(_snapshot(held_options=held_options)), "")
        if command == failing_component:
            return CliResult(
                2,
                json.dumps({"ok": False, "error": {"code": error_code, "message": "local"}}),
                "",
            )
        return _component(
            command, held_options=held_options, risk_available=failing_component != "risk"
        )

    envelope = run_current_analysis(settings=Settings(home), invoke=invoke)

    assert envelope[failing_component] is None
    assert (
        envelope["snapshot"]["snapshot_id"] == _snapshot(held_options=held_options)["snapshot_id"]
    )
    assert envelope["degradations"] == [
        {
            "component": failing_component,
            "status": "UNAVAILABLE",
            "error": {"code": error_code, "message": "local"},
        }
    ]
    assert list(home.iterdir()) == []


@pytest.mark.parametrize(
    ("held_options", "invalid_component"),
    [(False, "risk"), (True, "options")],
)
def test_invalid_component_output_is_local_and_cleanup_still_runs(
    tmp_path, held_options, invalid_component
):
    home = _private_home(tmp_path)

    def invoke(arguments):
        command = arguments[0]
        if command == "snapshot":
            return CliResult(0, json.dumps(_snapshot(held_options=held_options)), "")
        if command == invalid_component:
            return CliResult(0, "not-json", "")
        return _component(
            command, held_options=held_options, risk_available=invalid_component != "risk"
        )

    envelope = run_current_analysis(settings=Settings(home), invoke=invoke)

    assert envelope[invalid_component] is None
    assert envelope["degradations"] == [
        {
            "component": invalid_component,
            "status": "UNAVAILABLE",
            "error": {
                "code": "INVALID_COMPONENT_OUTPUT",
                "message": f"{invalid_component} returned invalid JSON",
            },
        }
    ]
    assert list(home.iterdir()) == []


def test_snapshot_failure_is_fatal_but_cleanup_still_runs(tmp_path):
    home = _private_home(tmp_path)

    def invoke(_arguments):
        return CliResult(
            2,
            json.dumps({"ok": False, "error": {"code": "SOURCE_UNAVAILABLE"}}),
            "",
        )

    with pytest.raises(CurrentAnalysisError, match="snapshot failed"):
        run_current_analysis(settings=Settings(home), invoke=invoke)

    assert list(home.iterdir()) == []


def test_cleanup_rejects_same_path_directory_replacement_and_reports_absolute_path(tmp_path):
    home = _private_home(tmp_path)
    owned = OwnedTemporaryRun.create(home)
    owned.write_json("snapshot.json", _snapshot())
    original_path = owned.path
    moved_path = home / "original-owned-run"
    original_path.rename(moved_path)
    original_path.mkdir(mode=0o700)

    with pytest.raises(TemporaryCleanupError) as caught:
        owned.cleanup()

    assert str(original_path) in str(caught.value)
    assert original_path.is_dir()
    assert (moved_path / "snapshot.json").is_file()


def test_cleanup_rechecks_directory_identity_immediately_before_unlink(tmp_path, monkeypatch):
    home = _private_home(tmp_path)
    owned = OwnedTemporaryRun.create(home)
    owned.write_json("snapshot.json", _snapshot())
    original_path = owned.path
    moved_path = home / "moved-after-initial-validation"
    original_validation = owned._validate_before_cleanup

    def validate_then_replace():
        original_validation()
        original_path.rename(moved_path)
        original_path.mkdir(mode=0o700)

    monkeypatch.setattr(owned, "_validate_before_cleanup", validate_then_replace)

    with pytest.raises(TemporaryCleanupError) as caught:
        owned.cleanup()

    assert str(original_path) in str(caught.value)
    assert (moved_path / "snapshot.json").is_file()
    assert original_path.is_dir()


def test_setup_failure_retains_unverified_directory_and_reports_absolute_path(
    tmp_path, monkeypatch
):
    import portfolio_analyst.current_analysis as current_analysis

    home = _private_home(tmp_path)

    def fail_chmod(*_args, **_kwargs):
        raise OSError("chmod blocked")

    monkeypatch.setattr(current_analysis.os, "chmod", fail_chmod)

    with pytest.raises(TemporaryCleanupError) as caught:
        OwnedTemporaryRun.create(home)

    retained = list(home.iterdir())
    assert len(retained) == 1
    assert retained[0].is_dir()
    assert str(retained[0]) in str(caught.value)


def test_cleanup_rejects_file_identity_replacement_without_deleting_any_file(tmp_path):
    home = _private_home(tmp_path)
    owned = OwnedTemporaryRun.create(home)
    owned.write_json("snapshot.json", _snapshot())
    snapshot = owned.path / "snapshot.json"
    original = home / "original.json"
    snapshot.rename(original)
    snapshot.write_text("{}", encoding="utf-8")
    snapshot.chmod(0o600)

    with pytest.raises(TemporaryCleanupError) as caught:
        owned.cleanup()

    assert str(owned.path) in str(caught.value)
    assert snapshot.is_file()
    assert original.is_file()


def test_cleanup_rechecks_file_identity_immediately_before_unlink(tmp_path, monkeypatch):
    home = _private_home(tmp_path)
    owned = OwnedTemporaryRun.create(home)
    owned.write_json("snapshot.json", _snapshot())
    snapshot = owned.path / "snapshot.json"
    original = home / "moved-after-initial-validation.json"
    original_validation = owned._validate_before_cleanup

    def validate_then_replace():
        original_validation()
        snapshot.rename(original)
        snapshot.write_text("replacement", encoding="utf-8")
        snapshot.chmod(0o600)

    monkeypatch.setattr(owned, "_validate_before_cleanup", validate_then_replace)

    with pytest.raises(TemporaryCleanupError) as caught:
        owned.cleanup()

    assert str(owned.path) in str(caught.value)
    assert snapshot.read_text(encoding="utf-8") == "replacement"
    assert original.is_file()


def test_cleanup_reports_a_file_recreated_immediately_after_unlink(tmp_path, monkeypatch):
    import portfolio_analyst.current_analysis as current_analysis

    home = _private_home(tmp_path)
    owned = OwnedTemporaryRun.create(home)
    owned.write_json("snapshot.json", _snapshot())
    snapshot = owned.path / "snapshot.json"
    real_unlink = current_analysis.os.unlink

    def unlink_then_recreate(name, *, dir_fd):
        real_unlink(name, dir_fd=dir_fd)
        snapshot.write_text("concurrent replacement", encoding="utf-8")
        snapshot.chmod(0o600)

    monkeypatch.setattr(current_analysis.os, "unlink", unlink_then_recreate)

    with pytest.raises(TemporaryCleanupError, match="changed after unlink") as caught:
        owned.cleanup()

    assert str(owned.path) in str(caught.value)
    assert snapshot.read_text(encoding="utf-8") == "concurrent replacement"


def test_cleanup_rejects_unknown_content_without_deleting_owned_files(tmp_path):
    home = _private_home(tmp_path)
    owned = OwnedTemporaryRun.create(home)
    owned.write_json("snapshot.json", _snapshot())
    unexpected = owned.path / "unexpected.txt"
    unexpected.write_text("do not delete", encoding="utf-8")
    unexpected.chmod(0o600)

    with pytest.raises(TemporaryCleanupError) as caught:
        owned.cleanup()

    assert str(owned.path) in str(caught.value)
    assert (owned.path / "snapshot.json").is_file()
    assert unexpected.is_file()


def test_cleanup_unlinks_only_owned_whitelist_then_removes_directory(tmp_path):
    home = _private_home(tmp_path)
    owned = OwnedTemporaryRun.create(home)
    path = owned.path
    owned.write_json("snapshot.json", _snapshot())
    owned.write_json("risk.json", {"status": "COMPLETE"})
    owned.write_json("technical.json", {"status": "COMPLETE"})

    owned.cleanup()

    assert not path.exists()


def test_current_analysis_runs_technical_after_risk_and_binds_available_risk(tmp_path):
    home = _private_home(tmp_path)
    calls = []

    def invoke(arguments):
        calls.append(arguments)
        if arguments[0] == "snapshot":
            return CliResult(0, json.dumps(_snapshot()), "")
        return _component(arguments[0])

    envelope = run_current_analysis(settings=Settings(home), invoke=invoke)

    technical = next(arguments for arguments in calls if arguments[0] == "technical")
    assert "--risk-report" in technical
    assert technical.index("--risk-report") > technical.index("--snapshot")
    assert envelope["technical"] == json.loads(_component("technical").stdout)
    assert list(home.iterdir()) == []


def test_current_analysis_keeps_technical_failure_local_when_risk_is_unavailable(tmp_path):
    home = _private_home(tmp_path)
    technical_arguments = []

    def invoke(arguments):
        if arguments[0] == "snapshot":
            return CliResult(0, json.dumps(_snapshot()), "")
        if arguments[0] == "risk":
            return CliResult(2, json.dumps({"error": {"code": "UNSUPPORTED_PORTFOLIO"}}), "")
        if arguments[0] == "technical":
            technical_arguments.extend(arguments)
            return CliResult(2, json.dumps({"error": {"code": "SOURCE_UNAVAILABLE"}}), "")
        raise AssertionError(arguments)

    envelope = run_current_analysis(settings=Settings(home), invoke=invoke)

    assert "--risk-report" not in technical_arguments
    assert envelope["technical"] is None
    assert [item["component"] for item in envelope["degradations"]] == ["risk", "technical"]
    assert list(home.iterdir()) == []


def test_current_analysis_returns_valid_technical_degradation_report_and_cleans_up(tmp_path):
    home = _private_home(tmp_path)

    def invoke(arguments):
        if arguments[0] == "snapshot":
            return CliResult(0, json.dumps(_snapshot()), "")
        if arguments[0] == "risk":
            return _component("risk")
        if arguments[0] == "technical":
            return _component("technical")
        raise AssertionError(arguments)

    envelope = run_current_analysis(settings=Settings(home), invoke=invoke)

    assert envelope["technical"]["data_quality_status"] == "UNAVAILABLE"
    assert envelope["degradations"] == []
    assert list(home.iterdir()) == []


def test_cleanup_implementation_never_uses_recursive_deletion():
    import portfolio_analyst.current_analysis as current_analysis

    source = Path(current_analysis.__file__).read_text(encoding="utf-8")
    assert "rmtree" not in source
    assert 'getattr(os, "O_DIRECTORY", 0)' in source
    assert re.search(r"os\.unlink\(.+dir_fd=", source)
    assert re.search(r"os\.rmdir\(.+dir_fd=", source)


@pytest.mark.parametrize(
    ("exception", "code", "message"),
    [
        (
            SettingsError("sensitive /source/settings.py"),
            "CONFIG_ERROR",
            "portfolio configuration is invalid",
        ),
        (
            PrivateHomeError("sensitive /private/path"),
            "PRIVATE_HOME_ERROR",
            "private portfolio data home is unavailable or unsafe",
        ),
        (OSError("sensitive /source/io.py"), "IO_ERROR", "current analysis I/O failed"),
    ],
)
def test_main_maps_expected_configuration_and_io_errors_without_traceback_or_paths(
    monkeypatch, capsys, exception, code, message
):
    import portfolio_analyst.current_analysis as current_analysis

    def fail(**_kwargs):
        raise exception

    monkeypatch.setattr(current_analysis, "run_current_analysis", fail)

    assert main([]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": False,
        "error": {"code": code, "message": message},
    }
    assert "sensitive" not in captured.out
    assert "Traceback" not in captured.out


@pytest.mark.parametrize("exception_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_main_does_not_swallow_programming_errors_or_control_flow(monkeypatch, exception_type):
    import portfolio_analyst.current_analysis as current_analysis

    def fail(**_kwargs):
        raise exception_type("stop")

    monkeypatch.setattr(current_analysis, "run_current_analysis", fail)

    with pytest.raises(exception_type):
        main([])


@pytest.mark.parametrize("data_home", ["/tmp", str(Path("/tmp").resolve())])
def test_script_rejects_tmp_as_data_home_with_stable_json_and_no_traceback(data_home):
    script = Path(__file__).resolve().parents[1] / "scripts" / "current_analysis.py"
    environment = os.environ.copy()
    environment["PORTFOLIO_AGENT_HOME"] = data_home
    completed = subprocess.run(
        [sys.executable, str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    # Settings rejects symlink components before private-directory permission checks.
    linked = Path(data_home).absolute() != Path(data_home).resolve()
    assert json.loads(completed.stdout) == {
        "ok": False,
        "error": {
            "code": "CONFIG_ERROR" if linked else "PRIVATE_HOME_ERROR",
            "message": "portfolio configuration is invalid"
            if linked
            else "private portfolio data home is unavailable or unsafe",
        },
    }
    assert "Traceback" not in completed.stdout
