from datetime import datetime, timezone
import json

import pytest

from portfolio_analyst.account_alias import AccountAliaser
from portfolio_analyst.service import generate_snapshot, load_classifications
from portfolio_analyst.settings import Settings
from test_snapshot_service_cli import ACCOUNT_IDS, FixtureAdapter, _configured_home


@pytest.mark.parametrize("body", [b"{", b"[]", b"\xff"])
def test_unreadable_classification_does_not_block_authoritative_snapshot(tmp_path, body):
    home = _configured_home(tmp_path)
    (home / "config/classifications.json").write_bytes(body)
    snapshot = generate_snapshot(
        Settings(data_home=home),
        adapter=FixtureAdapter(),
        account_ids=ACCOUNT_IDS,
        aliaser=AccountAliaser.from_key(b"a" * 32),
        discovery_complete=True,
        as_of=datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
    )
    assert snapshot.nav_source == "OPEND_TOTAL_ASSETS"
    assert len(snapshot.accounts) == 2
    assert snapshot.positions
    assert snapshot.data_quality.status == "PARTIAL"
    assert "classification" in snapshot.data_quality.missing_fields
    assert any("classifications.json" in note for note in snapshot.data_quality.warnings)


@pytest.mark.parametrize("bad", [None, 17, [], {"themes": "bad"}, {"sector": ["bad"]}])
def test_bad_classification_is_local_and_preserves_valid_rows(tmp_path, bad):
    home = _configured_home(tmp_path)
    (home / "config/classifications.json").write_text(
        json.dumps({"US.BAD": bad, "US.GOOD": {"sector": "TECH", "themes": ["AI"]}})
    )
    mapper = load_classifications(home)
    assert mapper.classify("US.GOOD").sector == "TECH"
    assert mapper.classify("US.GOOD").themes == ("AI",)
    invalid = mapper.classify("US.BAD")
    assert invalid.sector == "UNKNOWN"
    assert invalid.themes == ("UNKNOWN",)
    assert mapper.warnings


def test_classification_permission_failure_is_local(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    from pathlib import Path

    original = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path.name == "classifications.json":
            raise PermissionError("private detail must not enter warnings")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    mapper = load_classifications(home)
    assert mapper.classify("US.AAPL").sector == "UNKNOWN"
    assert mapper.warnings
    assert "private detail" not in " ".join(mapper.warnings)
