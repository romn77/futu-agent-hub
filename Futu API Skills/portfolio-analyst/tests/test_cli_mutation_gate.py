from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from portfolio_analyst import cli
from portfolio_analyst import private_home as private_home_module


def _assert_unsupported_without_home(monkeypatch, tmp_path, capsys, argv):
    home = tmp_path / "must-not-exist"
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(private_home_module, "os", SimpleNamespace(name="nt"))

    exit_code = cli.main([*argv, "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["error"]["code"] == "CONFIG_ERROR"
    assert "UNSUPPORTED_PLATFORM_SECURITY" in payload["error"]["message"]
    assert not home.exists()


def test_doctor_output_checks_platform_before_probe_or_home_creation(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli,
        "run_doctor",
        lambda **_kwargs: pytest.fail("doctor probe ran before the mutation gate"),
    )

    _assert_unsupported_without_home(
        monkeypatch, tmp_path, capsys, ["doctor", "--output", "reports/doctor.json"]
    )


def test_snapshot_save_checks_platform_before_source_access_or_home_creation(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        cli,
        "generate_snapshot",
        lambda *_args, **_kwargs: pytest.fail("snapshot source ran before the mutation gate"),
    )

    _assert_unsupported_without_home(monkeypatch, tmp_path, capsys, ["snapshot", "--save"])


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor", "--output", "reports/doctor.json"],
        ["snapshot", "--save"],
        ["risk", "--snapshot", "snapshots/source.json", "--output", "reports/risk.json"],
        ["analyze", "--snapshot", "snapshots/source.json", "--output", "reports/analysis"],
        [
            "simulate",
            "--snapshot",
            "snapshots/source.json",
            "--request",
            "requests/rebalance.json",
            "--output",
            "reports/simulation",
        ],
        [
            "trades",
            "--from",
            "2026-08-01",
            "--to",
            "2026-08-02",
            "--latest",
            "--output",
            "reports/trades",
        ],
        [
            "lifecycle",
            "init",
            "US.AAA",
            "--role",
            "CORE",
            "--opened-at",
            "2026-01-01",
            "--holding-horizon",
            "12 months",
            "--target-min",
            "0.1",
            "--target-max",
            "0.2",
            "--original-thesis",
            "User thesis",
            "--invalidation-condition",
            "User condition",
            "--review-trigger",
            "User trigger",
            "--last-reviewed-at",
            "2026-08-01",
        ],
        [
            "lifecycle",
            "validate",
            "--snapshot",
            "snapshots/source.json",
            "--output",
            "reports/lifecycle",
        ],
        [
            "journal",
            "record-decision",
            "--source-snapshot-id",
            "ps-20260828T000000Z-aaaaaaaaaaaaaaaa",
            "--decision",
            "ACCEPTED",
            "--summary",
            "User decision",
        ],
        [
            "journal",
            "record-analysis",
            "--source-snapshot-id",
            "ps-20260828T000000Z-aaaaaaaaaaaaaaaa",
            "--artifact-path",
            "reports/analysis.json",
            "--summary",
            "User analysis",
        ],
    ],
    ids=[
        "doctor-output",
        "snapshot-save",
        "risk-output",
        "analyze-output",
        "simulate-output",
        "trades-output",
        "lifecycle-init",
        "lifecycle-validate-output",
        "journal-decision",
        "journal-analysis",
    ],
)
def test_all_mutating_commands_fail_closed_before_home_creation(
    monkeypatch, tmp_path, capsys, argv
):
    _assert_unsupported_without_home(monkeypatch, tmp_path, capsys, argv)
