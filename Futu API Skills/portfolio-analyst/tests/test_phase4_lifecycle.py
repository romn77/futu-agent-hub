from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.journal_repository import JournalError, JournalRepository
from portfolio_analyst.lifecycle import LifecycleConfigError, analyze_lifecycle
from portfolio_analyst.lifecycle_repository import LifecycleRepository
from portfolio_analyst.snapshot_repository import SnapshotConflict, SnapshotRepository
from portfolio_analyst.persistence import SnapshotStore
from portfolio_analyst.reporting import build_portfolio_report
from portfolio_analyst.risk import analyze_risk
from portfolio_analyst.schema import (
    ReportSemanticError,
    validate_lifecycle_document,
    validate_portfolio_report_document,
)
from portfolio_analyst.settings import Settings, SettingsError
from portfolio_analyst import cli
from test_risk_analysis import snapshot as make_snapshot


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    return home


def _write_record(home: Path, symbol: str = "US.AAA", **updates: object) -> None:
    positions = home / "positions"
    positions.mkdir(mode=0o700, exist_ok=True)
    values = {
        "schema_version": "1.0",
        "symbol": symbol,
        "role": "CORE",
        "status": "ACTIVE",
        "opened_at": "2026-01-01",
        "holding_horizon": "12-24 months",
        "target_weight": {"min": 0.2, "max": 0.35},
        "original_thesis": "User supplied thesis",
        "invalidation_conditions": ["User condition"],
        "review_triggers": ["Next results"],
        "notes": [],
        "last_reviewed_at": "2026-08-01",
    }
    values.update(updates)
    import yaml

    path = positions / f"{symbol}.yaml"
    path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)


def test_snapshot_repository_is_content_addressed_nested_and_range_sorted(tmp_path):
    home = _home(tmp_path)
    repo = SnapshotRepository(home)
    first = make_snapshot()
    second_value = replace(first, as_of=first.as_of + timedelta(days=1), snapshot_id="")
    second = replace(second_value, snapshot_id=snapshot_identity(second_value))

    path = repo.save(first)
    repo.save(second)

    assert path.relative_to(home).parts[:3] == ("snapshots", "2026", "08")
    assert repo.save(first) == path  # exact duplicate is idempotent and never overwritten
    assert [item.snapshot_id for item in repo.list()] == [first.snapshot_id, second.snapshot_id]
    assert repo.read(first.snapshot_id) == first


def test_snapshot_repository_rejects_conflicting_existing_content(tmp_path):
    home = _home(tmp_path)
    repo = SnapshotRepository(home)
    item = make_snapshot()
    path = repo.save(item)
    path.chmod(0o600)
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SnapshotConflict):
        repo.save(item)


def test_journal_is_concurrent_append_only_hash_chained_and_bounded(tmp_path):
    home = _home(tmp_path)
    repo = JournalRepository(home)

    def append(index: int):
        return repo.append_decision(
            timestamp=datetime(2026, 8, 27, 12, index, tzinfo=timezone.utc),
            source_snapshot_id=make_snapshot().snapshot_id,
            summary=f"decision {index}",
            decision="ACCEPTED",
            metadata={"index": index},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(append, range(8)))

    loaded = repo.list_decisions()
    assert len(loaded) == len(records) == 8
    assert len({item.event_id for item in loaded}) == 8
    journal = home / "journal" / "decisions.jsonl"
    assert oct(journal.stat().st_mode & 0o777) == "0o600"
    with journal.open("ab") as handle:
        handle.write(b'{"truncated":')
    with pytest.raises(JournalError, match="truncated|invalid"):
        repo.list_decisions()


def test_journal_sanitizes_credentials_and_rejects_oversized_records(tmp_path):
    repo = JournalRepository(_home(tmp_path))
    record = repo.append_analysis(
        timestamp=datetime(2026, 8, 27, tzinfo=timezone.utc),
        source_snapshot_id=make_snapshot().snapshot_id,
        artifact_path="reports/safe/report.json",
        summary="api_key=secret\x1b[31m\nsecond line",
        metadata={},
    )
    assert "secret" not in json.dumps(record.to_dict())
    assert "\x1b" not in record.summary and "\n" not in record.summary
    with pytest.raises(JournalError, match="size|long"):
        repo.append_analysis(
            timestamp=datetime.now(timezone.utc),
            source_snapshot_id=make_snapshot().snapshot_id,
            artifact_path="reports/safe/report.json",
            summary="x" * 70_000,
            metadata={},
        )


def test_lifecycle_unconfigured_exact_boundary_and_weight_drift(tmp_path):
    home = _home(tmp_path)
    snap = make_snapshot()
    repository = LifecycleRepository(home)
    empty = analyze_lifecycle(snap, repository)
    assert {item.lifecycle_status for item in empty.positions} == {"UNCONFIGURED"}
    assert all(item.thesis_status == "UNREVIEWED" for item in empty.positions)

    _write_record(home, target_weight={"min": 0.4, "max": 0.4})
    report = analyze_lifecycle(snap, repository)
    aaa = next(item for item in report.positions if item.symbol == "US.AAA")
    assert aaa.lifecycle_status == "INTACT"

    _write_record(home, target_weight={"min": 0.1, "max": 0.3})
    drift = analyze_lifecycle(snap, repository)
    aaa = next(item for item in drift.positions if item.symbol == "US.AAA")
    assert aaa.lifecycle_status == "WEIGHT_DRIFT"
    assert aaa.review_priority == "P1"


def test_lifecycle_horizon_exit_and_role_drift_need_history(tmp_path):
    home = _home(tmp_path)
    snap = make_snapshot()
    _write_record(
        home,
        role="TRADING",
        opened_at="2025-01-01",
        holding_horizon="30 days",
        target_weight={"min": 0.4, "max": 0.4},
    )
    report = analyze_lifecycle(snap, LifecycleRepository(home))
    aaa = next(item for item in report.positions if item.symbol == "US.AAA")
    assert aaa.lifecycle_status == "HORIZON_DRIFT"

    _write_record(home, "US.GONE", status="EXITED")
    exited = analyze_lifecycle(snap, LifecycleRepository(home))
    assert (
        next(item for item in exited.positions if item.symbol == "US.GONE").lifecycle_status
        == "EXITED"
    )

    _write_record(
        home,
        role="SATELLITE",
        opened_at="2026-01-01",
        holding_horizon="24 months",
        target_weight={"min": 0.1, "max": 0.5},
    )
    no_history = analyze_lifecycle(snap, LifecycleRepository(home))
    assert (
        next(item for item in no_history.positions if item.symbol == "US.AAA").lifecycle_status
        == "INTACT"
    )
    history = []
    for days in (40, 20, 0):
        historical_as_of = snap.as_of - timedelta(days=days)
        candidate = replace(
            snap,
            as_of=historical_as_of,
            fx_evidence=tuple(
                replace(item, effective_at=historical_as_of) for item in snap.fx_evidence
            ),
            snapshot_id="",
        )
        history.append(replace(candidate, snapshot_id=snapshot_identity(candidate)))
    with_history = analyze_lifecycle(snap, LifecycleRepository(home), history=tuple(history))
    assert (
        next(item for item in with_history.positions if item.symbol == "US.AAA").lifecycle_status
        == "ROLE_DRIFT"
    )


def test_lifecycle_yaml_is_strict_private_and_never_invents_thesis(tmp_path):
    home = _home(tmp_path)
    _write_record(home, unexpected="no")
    with pytest.raises(LifecycleConfigError):
        LifecycleRepository(home).list()

    _write_record(home)
    path = home / "positions" / "US.AAA.yaml"
    path.chmod(0o644)
    with pytest.raises(LifecycleConfigError, match="0600"):
        LifecycleRepository(home).list()

    if hasattr(os, "link"):
        path.chmod(0o600)
        os.link(path, home / "positions" / "US.COPY.yaml")
        with pytest.raises(LifecycleConfigError, match="hard link"):
            LifecycleRepository(home).list()


def test_lifecycle_schema_reconstructs_identity_status_weight_and_coverage(tmp_path):
    home = _home(tmp_path)
    _write_record(home, target_weight={"min": 0.1, "max": 0.3})
    report = analyze_lifecycle(make_snapshot(), LifecycleRepository(home))
    payload = report.to_dict()
    validate_lifecycle_document(payload)

    payload["positions"][0]["lifecycle_status"] = "INTACT"
    body = dict(payload)
    body.pop("lifecycle_report_id")
    import hashlib

    payload["lifecycle_report_id"] = (
        "lr-"
        + hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
    )
    with pytest.raises(ReportSemanticError, match="precedence"):
        validate_lifecycle_document(payload)


def test_portfolio_report_embeds_separate_lifecycle_findings(tmp_path):
    home = _home(tmp_path)
    _write_record(home, target_weight={"min": 0.1, "max": 0.3})
    snap = make_snapshot()
    lifecycle = analyze_lifecycle(snap, LifecycleRepository(home))
    report = build_portfolio_report(snap, analyze_risk(snap), lifecycle)
    payload = report.to_dict()

    validate_portfolio_report_document(payload)
    assert (
        payload["risk_report"]["source_snapshot_id"] == payload["lifecycle"]["source_snapshot_id"]
    )
    assert payload["largest_risks"] == payload["risk_report"]["top_risks"]
    assert "Lifecycle findings" in report.to_markdown()
    assert "US.BBB: UNCONFIGURED" in report.to_markdown()


def test_analyze_lifecycle_failure_isolated_from_risk(monkeypatch, tmp_path, capsys):
    home = _home(tmp_path)
    snap = make_snapshot()
    SnapshotStore(home).save("snapshots/source.json", json.dumps(snap.to_dict()) + "\n")
    positions = home / "positions"
    positions.mkdir(mode=0o700)
    bad = positions / "US.AAA.yaml"
    bad.write_text("not: a lifecycle record\n", encoding="utf-8")
    bad.chmod(0o600)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: None)

    assert cli.main(["analyze", "--snapshot", "snapshots/source.json", "--json", "--no-save"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["risk_report"]["source_snapshot_id"] == snap.snapshot_id
    assert payload["lifecycle"]["status"] == "UNAVAILABLE"


def test_lifecycle_and_journal_cli_are_explicit_and_no_surprise_writes(
    monkeypatch, tmp_path, capsys
):
    home = _home(tmp_path)
    snap = make_snapshot()
    SnapshotStore(home).save("snapshots/source.json", json.dumps(snap.to_dict()) + "\n")
    _write_record(home)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    assert (
        cli.main(
            ["lifecycle", "validate", "--snapshot", "snapshots/source.json", "--json", "--no-save"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["source_snapshot_id"] == snap.snapshot_id
    assert not (home / "journal").exists()

    assert (
        cli.main(
            [
                "journal",
                "record-decision",
                "--source-snapshot-id",
                snap.snapshot_id,
                "--decision",
                "ACCEPTED",
                "--summary",
                "explicit",
                "--json",
            ]
        )
        == 0
    )
    event = json.loads(capsys.readouterr().out)
    assert event["event_type"] == "USER_DECISION"
    assert (home / "journal" / "decisions.jsonl").is_file()


def test_settings_rejects_symlinked_home(monkeypatch, tmp_path):
    real = _home(tmp_path)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(linked))
    with pytest.raises(SettingsError, match="symlink"):
        Settings.from_env()


def test_repository_list_operations_create_no_state_and_lifecycle_create_never_overwrites(tmp_path):
    home = tmp_path / "not-created"
    assert JournalRepository(home).list_decisions() == ()
    assert LifecycleRepository(home).list() == ()
    assert not home.exists()

    home = _home(tmp_path)
    payload = {
        "schema_version": "1.0",
        "symbol": "US.NEW",
        "role": "CORE",
        "status": "ACTIVE",
        "opened_at": "2026-01-01",
        "holding_horizon": "12 months",
        "target_weight": {"min": "0.1", "max": "0.2"},
        "original_thesis": "User fact",
        "invalidation_conditions": ["Condition"],
        "review_triggers": ["Trigger"],
        "notes": [],
        "last_reviewed_at": "2026-08-01",
    }
    repository = LifecycleRepository(home)
    created = repository.create(payload)
    before = created.read_bytes()
    with pytest.raises(LifecycleConfigError, match="overwrite"):
        repository.create(payload)
    assert created.read_bytes() == before


def test_analyze_saved_bundle_is_complete_manifested_and_transactional(
    monkeypatch, tmp_path, capsys
):
    home = _home(tmp_path)
    snap = make_snapshot()
    SnapshotStore(home).save("snapshots/source.json", json.dumps(snap.to_dict()) + "\n")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: None)

    assert (
        cli.main(
            ["analyze", "--snapshot", "snapshots/source.json", "--output", "reports/run", "--json"]
        )
        == 0
    )
    capsys.readouterr()
    output = home / "reports" / "run"
    expected = {
        "manifest.json",
        "portfolio_snapshot.json",
        "risk_report.json",
        "lifecycle_report.json",
        "lifecycle_report.md",
        "portfolio_report.json",
        "portfolio_report.md",
    }
    assert {path.name for path in output.iterdir()} == expected
    manifest = json.loads((output / "manifest.json").read_text())
    import hashlib

    assert manifest["source_snapshot_id"] == snap.snapshot_id
    for name, digest in manifest["artifacts"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest


def test_doctor_explicit_save_uses_private_immutable_store(monkeypatch, tmp_path, capsys):
    home = tmp_path / "doctor-home"
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(
        cli,
        "run_doctor",
        lambda **kwargs: {"ok": True, "checks": {}, "probe": kwargs["host"]},
    )

    assert cli.main(["doctor", "--json", "--output", "reports/doctor.json"]) == 0
    capsys.readouterr()
    assert oct(home.stat().st_mode & 0o777) == "0o700"
    assert oct((home / "reports/doctor.json").stat().st_mode & 0o777) == "0o600"
    assert cli.main(["doctor", "--json", "--output", "reports/doctor.json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "SNAPSHOT_EXISTS"
