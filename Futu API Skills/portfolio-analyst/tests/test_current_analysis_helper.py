import json
import os
import re
from pathlib import Path

import pytest

from portfolio_analyst.current_analysis import (
    CliResult,
    CurrentAnalysisError,
    OwnedTemporaryRun,
    TemporaryCleanupError,
    run_current_analysis,
)
from portfolio_analyst.settings import Settings


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX private-temp contract")


def _snapshot(*, held_options: bool = False):
    positions = []
    if held_options:
        positions.append({"security_type": "OPTION", "quantity": "1", "symbol": "US.TEST"})
    return {
        "snapshot_id": "ps-test",
        "as_of": "2026-09-02T00:00:00+00:00",
        "trd_env": "REAL",
        "base_currency": "USD",
        "positions": positions,
    }


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
        return CliResult(0, json.dumps({"status": "COMPLETE"}), "")

    envelope = run_current_analysis(settings=Settings(home), invoke=invoke)

    assert envelope[failing_component] is None
    assert envelope["snapshot"]["snapshot_id"] == "ps-test"
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
        return CliResult(0, json.dumps({"status": "COMPLETE"}), "")

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

    owned.cleanup()

    assert not path.exists()


def test_cleanup_implementation_never_uses_recursive_deletion():
    import portfolio_analyst.current_analysis as current_analysis

    source = Path(current_analysis.__file__).read_text(encoding="utf-8")
    assert "rmtree" not in source
    assert re.search(r"os\.unlink\(.+dir_fd=", source)
    assert re.search(r"os\.rmdir\(.+dir_fd=", source)
