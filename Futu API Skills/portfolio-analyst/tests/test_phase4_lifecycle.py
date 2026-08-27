from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from types import SimpleNamespace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.account_alias import AccountAliasError, AccountAliasStore
from portfolio_analyst.journal_repository import JournalError, JournalRepository
from portfolio_analyst.lifecycle import analyze_lifecycle
from portfolio_analyst.lifecycle import LifecycleInputError
from portfolio_analyst.lifecycle_repository import (
    LifecycleConfigError,
    LifecycleRecord,
    LifecycleRepository,
    TargetWeight,
)
from portfolio_analyst.snapshot_repository import SnapshotConflict, SnapshotRepository
from portfolio_analyst.snapshot_repository import SnapshotRepositoryError
import portfolio_analyst.snapshot_repository as snapshot_repository_module
import portfolio_analyst.journal_repository as journal_repository_module
import portfolio_analyst.lifecycle_repository as lifecycle_repository_module
from portfolio_analyst.persistence import IncompleteBundle, SnapshotStore
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


def test_snapshot_repository_rejects_wrong_layout_duplicates_temp_and_insecure_home(tmp_path):
    home = _home(tmp_path)
    repo = SnapshotRepository(home)
    item = make_snapshot()
    canonical = repo.save(item)
    canonical.parent.joinpath(f".{item.snapshot_id}.json.dead.tmp").write_text("stale")
    assert [value.snapshot_id for value in repo.list()] == [item.snapshot_id]

    misplaced = home / "snapshots" / "2026" / "13"
    misplaced.mkdir(mode=0o700)
    copied = misplaced / canonical.name
    copied.write_bytes(canonical.read_bytes())
    copied.chmod(0o600)
    with pytest.raises(SnapshotRepositoryError, match="canonical|month|layout"):
        repo.list()

    home.chmod(0o755)
    with pytest.raises(SnapshotRepositoryError, match="0700"):
        repo.read(item.snapshot_id)
    with pytest.raises(SnapshotRepositoryError, match="0700"):
        repo.save(item)


def test_snapshot_repository_enumeration_budget_fails_before_parsing(monkeypatch, tmp_path):
    home = _home(tmp_path)
    repo = SnapshotRepository(home)
    repo.save(make_snapshot())
    monkeypatch.setattr(snapshot_repository_module, "MAX_SNAPSHOT_ENTRIES", 0)
    with pytest.raises(SnapshotRepositoryError, match="budget"):
        repo.list()


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


def test_journal_rejects_duplicate_keys_and_preflights_total_append_size(monkeypatch, tmp_path):
    home = _home(tmp_path)
    repo = JournalRepository(home)
    record = repo.append_decision(
        timestamp=datetime(2026, 8, 27, tzinfo=timezone.utc),
        source_snapshot_id=make_snapshot().snapshot_id,
        summary="canonical",
        decision="ACCEPTED",
        metadata={},
    )
    journal = home / "journal" / "decisions.jsonl"
    raw = journal.read_bytes()
    payload = record.to_dict()
    duplicate = (
        b'{"summary":'
        + json.dumps(payload["summary"], ensure_ascii=False).encode()
        + b","
        + raw[1:]
    )
    journal.write_bytes(duplicate)
    with pytest.raises(JournalError, match="invalid|canonical|duplicate"):
        repo.list_decisions()

    journal.write_bytes(raw)
    before = journal.read_bytes()
    monkeypatch.setattr(journal_repository_module, "MAX_JOURNAL_BYTES", len(before) + 8)
    with pytest.raises(JournalError, match="size|limit"):
        repo.append_decision(
            timestamp=datetime(2026, 8, 28, tzinfo=timezone.utc),
            source_snapshot_id=make_snapshot().snapshot_id,
            summary="second record cannot fit",
            decision="ACCEPTED",
            metadata={},
        )
    assert journal.read_bytes() == before


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
    aaa = next(item for item in with_history.positions if item.symbol == "US.AAA")
    assert aaa.lifecycle_status == "ROLE_DRIFT"
    assert aaa.history_evidence_status == "AVAILABLE"
    assert len(aaa.history_evidence) == 3
    assert aaa.role_drift_policy == {
        "threshold": Decimal("0.10"),
        "minimum_snapshots": 3,
        "minimum_span_days": 30,
    }


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
    lifecycle_payload = json.loads(capsys.readouterr().out)
    assert lifecycle_payload["source_snapshot_id"] == snap.snapshot_id
    assert lifecycle_payload["coverage"]["history"]["issue"]["code"] == "HISTORY_INVALID"
    assert lifecycle_payload["coverage"]["history"]["status"] == "PARTIAL"
    assert all(
        any("HISTORY_INVALID" in note for note in item["data_quality_notes"])
        for item in lifecycle_payload["positions"]
    )
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


def test_lifecycle_repository_entry_budget_is_checked_before_yaml_reads(monkeypatch, tmp_path):
    home = _home(tmp_path)
    positions = home / "positions"
    positions.mkdir(mode=0o700)
    for name in ("US.AAA.yaml", "US.BBB.yaml"):
        path = positions / name
        path.write_text("invalid: yaml contract\n")
        path.chmod(0o600)
    monkeypatch.setattr(lifecycle_repository_module, "MAX_LIFECYCLE_RECORDS", 1)
    with pytest.raises(LifecycleConfigError, match="excessive"):
        LifecycleRepository(home).list()


def test_repository_consumer_budgets_fail_before_snapshot_or_yaml_parse(monkeypatch, tmp_path):
    home = _home(tmp_path)
    snapshot_repository = SnapshotRepository(home)
    snapshot_repository.save(make_snapshot())
    snapshot_reads: list[str] = []
    monkeypatch.setattr(
        snapshot_repository,
        "read",
        lambda value: snapshot_reads.append(value),
    )
    with pytest.raises(SnapshotRepositoryError, match="consumer limit"):
        snapshot_repository.list(limit=0)
    assert snapshot_reads == []

    _write_record(home)
    lifecycle_repository = LifecycleRepository(home)
    yaml_reads: list[str] = []
    monkeypatch.setattr(
        lifecycle_repository,
        "read",
        lambda value: yaml_reads.append(value),
    )
    with pytest.raises(LifecycleConfigError, match="excessive"):
        lifecycle_repository.list(limit=0)
    assert yaml_reads == []


def test_lifecycle_validator_preflights_shape_and_operation_budget_before_schema():
    with pytest.raises(ReportSemanticError, match="array or observation budget"):
        validate_lifecycle_document({"positions": [{}] * 20_001})

    with pytest.raises(ReportSemanticError, match="operation or wall-time budget"):
        validate_lifecycle_document({"positions": [{}]}, max_validation_operations=1)


def test_windows_repository_security_fallbacks_fail_closed(monkeypatch, tmp_path):
    home = _home(tmp_path)
    monkeypatch.setattr(snapshot_repository_module, "os", SimpleNamespace(name="nt"))
    with pytest.raises(SnapshotRepositoryError, match="UNSUPPORTED_PLATFORM_SECURITY"):
        SnapshotRepository(home).read(make_snapshot().snapshot_id)

    monkeypatch.setattr(lifecycle_repository_module, "os", SimpleNamespace(name="nt"))
    with pytest.raises(LifecycleConfigError, match="UNSUPPORTED_PLATFORM_SECURITY"):
        LifecycleRepository(home).read("US.AAA")

    with pytest.raises(JournalError, match="UNSUPPORTED_PLATFORM_SECURITY"):
        JournalRepository(home)._append_fallback("decisions.jsonl", {})
    with pytest.raises(ValueError, match="UNSUPPORTED_PLATFORM_SECURITY"):
        SnapshotStore(home)._save_fallback(("snapshot.json",), "{}")


def test_unsupported_platform_mutations_have_zero_filesystem_side_effect(monkeypatch, tmp_path):
    home = tmp_path / "never-created"
    snap = make_snapshot()

    monkeypatch.setattr(snapshot_repository_module, "os", SimpleNamespace(name="nt"))
    with pytest.raises(SnapshotRepositoryError, match="UNSUPPORTED_PLATFORM_SECURITY"):
        SnapshotRepository(home).save(snap)
    assert not home.exists()

    monkeypatch.setattr(lifecycle_repository_module, "os", SimpleNamespace(name="nt"))
    with pytest.raises(LifecycleConfigError, match="UNSUPPORTED_PLATFORM_SECURITY"):
        LifecycleRepository(home).create(
            {
                "schema_version": "1.0",
                "symbol": "US.AAA",
                "role": "CORE",
                "status": "ACTIVE",
                "opened_at": "2026-01-01",
                "holding_horizon": "12 months",
                "target_weight": {"min": "0.1", "max": "0.2"},
                "original_thesis": "User fact",
                "invalidation_conditions": [],
                "review_triggers": [],
                "notes": [],
                "last_reviewed_at": "2026-01-01",
            }
        )
    assert not home.exists()

    monkeypatch.setattr(journal_repository_module, "os", SimpleNamespace(name="nt"))
    with pytest.raises(JournalError, match="UNSUPPORTED_PLATFORM_SECURITY"):
        JournalRepository(home).append_decision(
            timestamp=snap.as_of,
            source_snapshot_id=snap.snapshot_id,
            summary="review",
            decision="ACCEPTED",
            metadata={},
        )
    assert not home.exists()

    import portfolio_analyst.account_alias as account_alias_module

    monkeypatch.setattr(account_alias_module, "os", SimpleNamespace(name="nt"))
    with pytest.raises(AccountAliasError, match="UNSUPPORTED_PLATFORM_SECURITY"):
        AccountAliasStore(home).load_or_create()
    assert not home.exists()


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
    for name, evidence in manifest["artifacts"].items():
        body = (output / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == evidence["sha256"]
        assert len(body) == evidence["size_bytes"]
    assert set(SnapshotStore(home).validate_bundle("reports/run")) == set(manifest["artifacts"])


def test_bundle_manifest_is_published_last_and_missing_or_bad_digest_is_incomplete(
    monkeypatch, tmp_path
):
    home = _home(tmp_path)
    store = SnapshotStore(home)
    real_link = os.link
    published: list[str] = []

    def observe_link(*args, **kwargs):
        published.append(str(args[1]))
        return real_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", observe_link)
    files = {"artifact.json": "{}\n"}
    snap = make_snapshot()
    from portfolio_analyst.persistence import bundle_manifest

    manifest = bundle_manifest(
        source_snapshot_id=snap.snapshot_id,
        as_of=snap.to_dict()["as_of"],
        report_id="test-report",
        files=files,
    )
    store.save_bundle(
        "reports/atomic",
        {**files, "manifest.json": manifest},
    )
    assert published[-1] == "manifest.json"
    assert set(store.validate_bundle("reports/atomic")) == {"artifact.json"}

    (home / "reports" / "atomic" / "artifact.json").write_text("tampered")
    with pytest.raises(IncompleteBundle, match="INCOMPLETE"):
        store.validate_bundle("reports/atomic")
    (home / "reports" / "atomic" / "manifest.json").unlink()
    with pytest.raises(IncompleteBundle, match="INCOMPLETE"):
        store.validate_bundle("reports/atomic")


def test_bundle_manifest_publish_failure_rolls_back_all_artifacts(monkeypatch, tmp_path):
    home = _home(tmp_path)
    store = SnapshotStore(home)
    real_link = os.link

    def fail_manifest(*args, **kwargs):
        if args[1] == "manifest.json":
            raise OSError("injected manifest publish failure")
        return real_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", fail_manifest)
    with pytest.raises(OSError, match="manifest publish"):
        store.save_bundle(
            "reports/interrupted",
            {"artifact.json": "{}\n", "manifest.json": "{}\n"},
        )
    output = home / "reports" / "interrupted"
    assert not any(path for path in output.iterdir() if not path.name.startswith("."))
    with pytest.raises(IncompleteBundle, match="INCOMPLETE"):
        store.validate_bundle("reports/interrupted")


def test_bundle_uses_artifact_barrier_before_manifest_barrier(monkeypatch, tmp_path):
    home = _home(tmp_path)
    store = SnapshotStore(home)
    snap = make_snapshot()
    files = {"a.json": "a\n", "b.md": "b\n"}
    from portfolio_analyst.persistence import bundle_manifest

    events: list[str] = []
    real_link = os.link
    real_fsync = os.fsync

    def observe_link(*args, **kwargs):
        events.append(f"link:{args[1]}")
        return real_link(*args, **kwargs)

    def observe_fsync(fd):
        if __import__("stat").S_ISDIR(os.fstat(fd).st_mode):
            events.append("fsync:dir")
        return real_fsync(fd)

    monkeypatch.setattr(os, "link", observe_link)
    monkeypatch.setattr(os, "fsync", observe_fsync)
    store.save_bundle(
        "reports/barriers",
        {
            **files,
            "manifest.json": bundle_manifest(
                source_snapshot_id=snap.snapshot_id,
                as_of=snap.to_dict()["as_of"],
                report_id="barrier-test",
                files=files,
            ),
        },
    )
    relevant = [event for event in events if event.startswith("link:") or event == "fsync:dir"]
    assert relevant[:5] == [
        "link:a.json",
        "link:b.md",
        "fsync:dir",
        "link:manifest.json",
        "fsync:dir",
    ]


def test_bundle_second_directory_fsync_failure_rolls_back_published_manifest(monkeypatch, tmp_path):
    home = _home(tmp_path)
    store = SnapshotStore(home)
    snap = make_snapshot()
    files = {"artifact.json": "{}\n"}
    from portfolio_analyst.persistence import bundle_manifest

    real_fsync = os.fsync
    directory_calls = 0

    def fail_second_directory_fsync(fd):
        nonlocal directory_calls
        if __import__("stat").S_ISDIR(os.fstat(fd).st_mode):
            directory_calls += 1
            if directory_calls == 2:
                raise OSError("injected manifest barrier failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_second_directory_fsync)
    with pytest.raises(OSError, match="manifest barrier"):
        store.save_bundle(
            "reports/fsync-failure",
            {
                **files,
                "manifest.json": bundle_manifest(
                    source_snapshot_id=snap.snapshot_id,
                    as_of=snap.to_dict()["as_of"],
                    report_id="fsync-test",
                    files=files,
                ),
            },
        )
    visible = [
        item
        for item in (home / "reports" / "fsync-failure").iterdir()
        if not item.name.startswith(".")
    ]
    assert visible == []


def test_bundle_manifest_rejects_impossible_source_calendar_and_symlink_swap(monkeypatch, tmp_path):
    home = _home(tmp_path)
    store = SnapshotStore(home)
    snap = make_snapshot()
    files = {"artifact.json": "{}\n"}
    from portfolio_analyst.persistence import bundle_manifest

    manifest = json.loads(
        bundle_manifest(
            source_snapshot_id=snap.snapshot_id,
            as_of=snap.to_dict()["as_of"],
            report_id="swap-test",
            files=files,
        )
    )
    manifest["source_snapshot_id"] = "ps-20269999T999999Z-real-usd-0123456789abcdef"
    store.save_bundle(
        "reports/invalid-calendar",
        {**files, "manifest.json": json.dumps(manifest)},
    )
    with pytest.raises(IncompleteBundle, match="manifest invalid"):
        store.validate_bundle("reports/invalid-calendar")

    valid = bundle_manifest(
        source_snapshot_id=snap.snapshot_id,
        as_of=snap.to_dict()["as_of"],
        report_id="swap-test",
        files=files,
    )
    store.save_bundle("reports/swap", {**files, "manifest.json": valid})
    directory = home / "reports" / "swap"
    outside = home / "outside"
    outside.write_text("{}\n")
    outside.chmod(0o600)
    real_open = os.open
    swapped = False

    def swap_before_artifact(path, *args, **kwargs):
        nonlocal swapped
        if path == "artifact.json" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            (directory / "artifact.json").unlink()
            (directory / "artifact.json").symlink_to(outside)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_artifact)
    with pytest.raises(IncompleteBundle, match="unsafe anchored read"):
        store.validate_bundle("reports/swap")


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


def _historical_snapshot(source, *, days: int):
    historical_as_of = source.as_of + timedelta(days=days)
    candidate = replace(
        source,
        as_of=historical_as_of,
        fx_evidence=tuple(
            replace(item, effective_at=historical_as_of) for item in source.fx_evidence
        ),
        snapshot_id="",
    )
    return replace(candidate, snapshot_id=snapshot_identity(candidate))


def test_history_excludes_future_preopen_and_duplicates_with_auditable_quality(tmp_path):
    home = _home(tmp_path)
    snap = make_snapshot()
    _write_record(
        home,
        role="SATELLITE",
        opened_at="2026-08-10",
        last_reviewed_at="2026-08-15",
        holding_horizon="24 months",
        target_weight={"min": 0.1, "max": 0.5},
    )
    pre_open = _historical_snapshot(snap, days=-30)
    eligible = _historical_snapshot(snap, days=-10)
    future = _historical_snapshot(snap, days=10)

    report = analyze_lifecycle(
        snap,
        LifecycleRepository(home),
        history=(future, eligible, eligible, pre_open),
    )
    aaa = next(item for item in report.positions if item.symbol == "US.AAA")

    assert aaa.lifecycle_status == "INTACT"
    assert aaa.history_evidence_status == "PARTIAL"
    assert [item.snapshot_id for item in aaa.history_evidence] == [eligible.snapshot_id]
    assert report.coverage["history"]["candidate_snapshots"] == 4
    assert report.coverage["history"]["accepted_snapshots"] == 2
    assert report.coverage["history"]["duplicate_snapshots"] == 1
    assert report.coverage["history"]["future_snapshots"] == 1
    assert any("pre-open" in note for note in aaa.data_quality_notes)
    assert any("future" in note for note in report.data_quality_notes)


def test_lifecycle_semantics_reject_forged_role_history_after_rehash(tmp_path):
    home = _home(tmp_path)
    snap = make_snapshot()
    _write_record(
        home,
        role="SATELLITE",
        opened_at="2026-01-01",
        holding_horizon="24 months",
        target_weight={"min": 0.1, "max": 0.5},
    )
    history = tuple(_historical_snapshot(snap, days=days) for days in (-40, -20, 0))
    payload = analyze_lifecycle(snap, LifecycleRepository(home), history=history).to_dict()
    aaa = next(item for item in payload["positions"] if item["symbol"] == "US.AAA")
    assert aaa["lifecycle_status"] == "ROLE_DRIFT"
    aaa["history_evidence"][0]["as_of"] = aaa["history_evidence"][1]["as_of"]
    body = dict(payload)
    body.pop("lifecycle_report_id")
    import hashlib

    payload["lifecycle_report_id"] = (
        "lr-"
        + hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
    )
    with pytest.raises(ReportSemanticError, match="role|history"):
        validate_lifecycle_document(payload)


def test_role_drift_history_records_absence_and_cannot_prove_persistence(tmp_path):
    home = _home(tmp_path)
    snap = make_snapshot()
    _write_record(
        home,
        role="SATELLITE",
        opened_at="2026-01-01",
        holding_horizon="24 months",
        target_weight={"min": 0.1, "max": 0.5},
    )
    history = []
    for index, days in enumerate((-40, -30, -20, 0)):
        item = _historical_snapshot(snap, days=days)
        if index == 1:
            changed_positions = tuple(
                replace(position, symbol="ZZZ") if position.symbol == "AAA" else position
                for position in item.positions
            )
            changed = replace(item, positions=changed_positions, snapshot_id="")
            item = replace(changed, snapshot_id=snapshot_identity(changed))
        history.append(item)

    report = analyze_lifecycle(snap, LifecycleRepository(home), history=tuple(history))
    aaa = next(item for item in report.positions if item.symbol == "US.AAA")
    assert aaa.lifecycle_status != "ROLE_DRIFT"
    assert aaa.history_evidence_status == "PARTIAL"
    assert [entry.present for entry in aaa.history_evidence] == [True, False, True, True]
    assert aaa.history_evidence[1].weight is None

    payload = report.to_dict()
    deleted = json.loads(json.dumps(payload))
    deleted_aaa = next(item for item in deleted["positions"] if item["symbol"] == "US.AAA")
    deleted_aaa["history_evidence"].pop(1)
    deleted["coverage"]["history"]["position_observations"] -= 1
    deleted_body = dict(deleted)
    deleted_body.pop("lifecycle_report_id")
    import hashlib

    deleted["lifecycle_report_id"] = (
        "lr-"
        + hashlib.sha256(
            json.dumps(
                deleted_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()[:20]
    )
    with pytest.raises(ReportSemanticError, match="incomplete"):
        validate_lifecycle_document(deleted)

    forged = next(item for item in payload["positions"] if item["symbol"] == "US.AAA")
    forged["history_evidence"][1]["present"] = True
    forged["history_evidence"][1]["weight"] = "0.4"
    body = dict(payload)
    body.pop("lifecycle_report_id")
    payload["lifecycle_report_id"] = (
        "lr-"
        + hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
    )
    with pytest.raises(ReportSemanticError, match="history|role"):
        validate_lifecycle_document(payload)


def test_lifecycle_rejects_current_identity_time_and_history_budget_tampering(tmp_path):
    home = _home(tmp_path)
    snap = make_snapshot()
    payload = analyze_lifecycle(snap, LifecycleRepository(home)).to_dict()
    payload["source_snapshot_id"] = "ps-20260828T100000Z-real-usd-0000000000000000"
    body = dict(payload)
    body.pop("lifecycle_report_id")
    import hashlib

    payload["lifecycle_report_id"] = (
        "lr-"
        + hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
    )
    with pytest.raises(ReportSemanticError, match="snapshot.*timestamp"):
        validate_lifecycle_document(payload)

    with pytest.raises(LifecycleInputError, match="budget"):
        analyze_lifecycle(
            snap,
            LifecycleRepository(home),
            history=tuple(_historical_snapshot(snap, days=-days) for days in (1, 2)),
            max_history_snapshots=1,
        )


def test_lifecycle_operation_and_injected_wall_clock_budgets_fail_closed(tmp_path):
    home = _home(tmp_path)
    snap = make_snapshot()
    with pytest.raises(LifecycleInputError, match="budget"):
        analyze_lifecycle(snap, LifecycleRepository(home), max_operations=1)

    ticks = iter((0.0, 11.0))
    with pytest.raises(LifecycleInputError, match="wall-time budget"):
        analyze_lifecycle(
            snap,
            LifecycleRepository(home),
            max_seconds=10.0,
            clock=lambda: next(ticks),
        )


def test_typed_lifecycle_record_cannot_bypass_canonical_validation():
    with pytest.raises(LifecycleConfigError):
        LifecycleRecord(
            schema_version="1.0",
            symbol="../US.BAD",
            role="FAKE",
            status="ACTIVE",
            opened_at=date(2026, 1, 1),
            holding_horizon="forever",
            target_weight=TargetWeight(Decimal("-1"), Decimal("2")),
            original_thesis="api_key=secret\x1b[31m",
            invalidation_conditions=(),
            review_triggers=(),
            notes=(),
            last_reviewed_at=date(2025, 1, 1),
        )


def test_typed_lifecycle_record_uses_same_sanitized_round_trip_as_document(tmp_path):
    home = _home(tmp_path)
    typed = LifecycleRecord(
        schema_version="1.0",
        symbol="us.safe",
        role="core",
        status="active",
        opened_at=date(2026, 1, 1),
        holding_horizon="12 MONTHS",
        target_weight=TargetWeight(Decimal("0.1"), Decimal("0.2")),
        original_thesis="api_key=secret\x1b[31m",
        invalidation_conditions=("condition\x00",),
        review_triggers=("trigger",),
        notes=(),
        last_reviewed_at=date(2026, 8, 1),
    )
    assert typed.symbol == "US.SAFE"
    assert typed.holding_horizon == "12 months"
    assert "secret" not in typed.original_thesis
    path = LifecycleRepository(home).create(typed)
    assert LifecycleRepository(home).read("US.SAFE") == typed
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_lifecycle_human_markdown_and_integrated_report_are_audit_complete(tmp_path):
    home = _home(tmp_path)
    _write_record(
        home,
        role="CORE",
        target_weight={"min": 0.1, "max": 0.3},
        original_thesis="private <thesis> [link](evil)",
        invalidation_conditions=["User invalidation <condition>"],
        review_triggers=["User trigger [review](evil)"],
        notes=["User note <audit>"],
    )
    snap = make_snapshot()
    lifecycle = analyze_lifecycle(snap, LifecycleRepository(home))
    lifecycle_payload = lifecycle.to_dict()
    human = lifecycle.to_human()
    markdown = lifecycle.to_markdown()
    integrated = build_portfolio_report(snap, analyze_risk(snap), lifecycle).to_markdown()

    for rendered in (human, markdown, integrated):
        assert "current_weight" in rendered or "Current weight" in rendered
        assert "target" in rendered.lower()
        assert "WEIGHT_DRIFT" in rendered
        assert "P1" in rendered
        assert "history" in rendered.lower()
        assert "source_snapshot_id" in rendered
    assert "As of:" in human
    assert "private <thesis> [link](evil)" in human
    assert "private &lt;thesis&gt; \\[link\\]\\(evil\\)" in markdown
    assert "private &lt;thesis&gt; \\[link\\]\\(evil\\)" in integrated
    assert "User invalidation <condition>" in human
    assert "User invalidation &lt;condition&gt;" in markdown
    assert "User invalidation &lt;condition&gt;" in integrated
    assert "User trigger" in human and "User note" in human
    assert "User trigger" in markdown and "User note" in markdown
    assert "User trigger" in integrated and "User note" in integrated
    assessment = lifecycle_payload["positions"][0]
    assert assessment["original_thesis"] == "private <thesis> [link](evil)"
    assert assessment["original_thesis_evidence"]["configured"] is True
    assert assessment["original_thesis_evidence"]["provenance"] == "USER_CONFIG"
    assert assessment["original_thesis_evidence"]["system_inferred"] is False
    for rendered in (human, markdown, integrated):
        assert "USER_CONFIG" in rendered
        assert "system inferred" in rendered.lower()


def test_lifecycle_report_scrubs_sensitive_user_config_in_every_output(tmp_path):
    home = _home(tmp_path)
    _write_record(
        home,
        original_thesis="api_key=thesis-secret account 12345678\x1b[31m",
        invalidation_conditions=["password=condition-secret"],
        review_triggers=["client_secret=trigger-secret"],
        notes=["token=note-secret"],
    )
    snap = make_snapshot()
    lifecycle = analyze_lifecycle(snap, LifecycleRepository(home))
    outputs = (
        json.dumps(lifecycle.to_dict()),
        lifecycle.to_human(),
        lifecycle.to_markdown(),
        build_portfolio_report(snap, analyze_risk(snap), lifecycle).to_markdown(),
    )
    for rendered in outputs:
        for secret in (
            "thesis-secret",
            "condition-secret",
            "trigger-secret",
            "note-secret",
            "12345678",
            "\x1b",
        ):
            assert secret not in rendered


def test_lifecycle_quality_is_canonical_from_history_and_position_notes(tmp_path):
    report = analyze_lifecycle(make_snapshot(), LifecycleRepository(_home(tmp_path)))
    payload = report.to_dict()
    assert payload["coverage"]["history"]["status"] == "UNAVAILABLE"
    assert payload["data_quality_status"] == payload["status"] == "PARTIAL"
    assert any(
        reason["code"] == "HISTORY_UNAVAILABLE" for reason in payload["data_quality_reasons"]
    )
    assert any(reason["code"] == "POSITION_DATA_NOTE" for reason in payload["data_quality_reasons"])

    payload["status"] = "COMPLETE"
    payload["data_quality_status"] = "COMPLETE"
    payload["data_quality_notes"] = []
    payload["data_quality_reasons"] = []
    body = dict(payload)
    body.pop("lifecycle_report_id")
    import hashlib

    payload["lifecycle_report_id"] = (
        "lr-"
        + hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
    )
    with pytest.raises(ReportSemanticError, match="quality"):
        validate_lifecycle_document(payload)


@pytest.mark.parametrize("tamper", ["text", "digest", "length"])
def test_lifecycle_thesis_text_digest_and_length_are_semantically_bound(tmp_path, tamper):
    home = _home(tmp_path)
    _write_record(home, original_thesis="Authoritative user thesis")
    payload = analyze_lifecycle(make_snapshot(), LifecycleRepository(home)).to_dict()
    assessment = next(item for item in payload["positions"] if item["symbol"] == "US.AAA")
    if tamper == "text":
        assessment["original_thesis"] = "Forged thesis"
    elif tamper == "digest":
        assessment["original_thesis_evidence"]["sha256"] = "0" * 64
    else:
        assessment["original_thesis_evidence"]["characters"] += 1
    body = dict(payload)
    body.pop("lifecycle_report_id")
    import hashlib

    payload["lifecycle_report_id"] = (
        "lr-"
        + hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
    )
    with pytest.raises(ReportSemanticError, match="thesis"):
        validate_lifecycle_document(payload)
