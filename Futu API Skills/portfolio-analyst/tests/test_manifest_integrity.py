import shutil
import subprocess
from pathlib import Path

import pytest

from portfolio_analyst import skill_trust
from portfolio_analyst.futu_adapter import FutuAdapter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_SKILL = PROJECT_ROOT.parent / "futuapi"


def _audited_copy(tmp_path: Path) -> Path:
    target = tmp_path / "futuapi"
    for record in skill_trust.audited_files():
        relative = record.relative_path
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OFFICIAL_SKILL / relative, destination)
    return target


def test_canonical_command_view_is_immutable_and_cannot_add_place_order():
    names = skill_trust.audited_command_names()

    assert isinstance(names, frozenset)
    assert names == frozenset(
        {
            "get_accounts",
            "get_portfolio",
            "get_kline",
            "get_history_orders",
            "get_history_order_fill_list",
        }
    )
    assert "place_order" not in names
    with pytest.raises(AttributeError):
        names.add("place_order")
    assert not hasattr(FutuAdapter, "READ_ONLY_SCRIPTS")


def test_changed_allowlisted_content_fails_identity_validation(tmp_path):
    candidate = _audited_copy(tmp_path)
    script = candidate / "scripts/trade/get_accounts.py"
    script.write_text(script.read_text(encoding="utf-8") + "\nplace_order()\n", encoding="utf-8")

    validation = skill_trust.validate_futuapi_skill(candidate)

    assert validation.valid is False
    assert "digest mismatch" in validation.detail


def test_identity_is_stable_across_crlf_line_endings(tmp_path):
    candidate = _audited_copy(tmp_path)
    for record in skill_trust.audited_files():
        source = candidate / record.relative_path
        canonical = skill_trust.canonicalize_bytes(source.read_bytes())
        source.write_bytes(canonical.replace(b"\n", b"\r\n"))

    validation = skill_trust.validate_futuapi_skill(candidate)

    assert validation.valid is True


def test_execution_uses_verified_snapshot_when_source_changes_after_open(monkeypatch, tmp_path):
    candidate = _audited_copy(tmp_path)
    adapter = FutuAdapter(candidate)
    source_script = candidate / "scripts/trade/get_accounts.py"
    original = source_script.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    observed = {}

    def fake_run(args, **_kwargs):
        source_script.write_text("place_order()\n", encoding="utf-8")
        observed["executed"] = Path(args[2]).read_bytes()
        observed["snapshot_root"] = Path(args[2]).parents[2]
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = adapter.run("get_accounts", ["--json"])

    assert observed["executed"] == original
    assert not observed["snapshot_root"].exists()
    assert not Path(result.args[2]).exists()


@pytest.mark.parametrize("failure", ["return", "raise", "timeout"])
def test_private_snapshot_is_cleaned_for_all_subprocess_outcomes(monkeypatch, tmp_path, failure):
    candidate = _audited_copy(tmp_path)
    adapter = FutuAdapter(candidate)
    observed = {}

    def fake_run(args, **_kwargs):
        observed["snapshot_root"] = Path(args[2]).parents[2]
        if failure == "raise":
            raise RuntimeError("subprocess failed")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 1, output="{}", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    if failure == "raise":
        with pytest.raises(RuntimeError):
            adapter.run("get_accounts", ["--json"])
    elif failure == "timeout":
        with pytest.raises(subprocess.TimeoutExpired):
            adapter.run("get_accounts", ["--json"])
    else:
        assert adapter.run("get_accounts", ["--json"]).returncode == 1

    assert not observed["snapshot_root"].exists()
