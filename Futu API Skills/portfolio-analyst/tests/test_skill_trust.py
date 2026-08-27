from pathlib import Path
import shutil

import pytest

from portfolio_analyst.doctor import run_doctor
from portfolio_analyst.futu_adapter import FutuAdapter
from portfolio_analyst import settings
from portfolio_analyst.skill_trust import audited_files


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_SKILL = PROJECT_ROOT.parent / "futuapi"


def _candidate(path: Path, *, name: str = "futuapi") -> Path:
    for record in audited_files():
        target = path / record.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("print('{}')\n", encoding="utf-8")
    (path / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return path


def _audited_candidate(path: Path) -> Path:
    for record in audited_files():
        target = path / record.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OFFICIAL_SKILL / record.relative_path, target)
    return path


def test_arbitrary_skill_md_directory_is_not_trusted(monkeypatch, tmp_path):
    fake = _candidate(tmp_path / "fake", name="portfolio-analyst")
    monkeypatch.setenv("FUTUAPI_SKILL_DIR", str(fake))

    assert settings.discover_futuapi_skill(repo_root=tmp_path, global_roots=[]) is None
    with pytest.raises(ValueError, match="audited futuapi Skill"):
        FutuAdapter(fake)


def test_matching_layout_in_arbitrary_cwd_is_not_implicitly_discovered(monkeypatch, tmp_path):
    _candidate(tmp_path / "futuapi")
    trusted_repo = tmp_path / "empty-repository"
    trusted_repo.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FUTUAPI_SKILL_DIR", raising=False)

    assert settings.discover_futuapi_skill(repo_root=trusted_repo, global_roots=[]) is None


def test_explicit_compatible_source_is_classified_without_official_claim(monkeypatch, tmp_path):
    explicit = _audited_candidate(tmp_path / "compatible")
    monkeypatch.setenv("FUTUAPI_SKILL_DIR", str(explicit))

    source = settings.discover_futuapi_skill_source(repo_root=tmp_path, global_roots=[])

    assert source.path == explicit.resolve()
    assert source.kind == "explicit"
    assert "user-configured" in source.detail
    assert "official" not in source.detail.lower()
    assert "verified" not in source.detail.lower()


def test_repository_and_global_roots_are_ordered_trusted_locations(monkeypatch, tmp_path):
    repository_skill = _audited_candidate(tmp_path / "repo" / "Futu API Skills" / "futuapi")
    global_skill = _audited_candidate(tmp_path / "global-skills" / "futuapi")
    monkeypatch.delenv("FUTUAPI_SKILL_DIR", raising=False)

    repository_source = settings.discover_futuapi_skill_source(
        repo_root=tmp_path / "repo", global_roots=[tmp_path / "global-skills"]
    )
    global_source = settings.discover_futuapi_skill_source(
        repo_root=tmp_path / "empty", global_roots=[tmp_path / "global-skills"]
    )

    assert repository_source.path == repository_skill.resolve()
    assert repository_source.kind == "repository"
    assert global_source.path == global_skill.resolve()
    assert global_source.kind == "global"


def test_adapter_rejects_missing_required_allowlisted_script(tmp_path):
    incomplete = _audited_candidate(tmp_path / "incomplete")
    (incomplete / "scripts/trade/get_portfolio.py").unlink()

    with pytest.raises(ValueError, match="required audited file"):
        FutuAdapter(incomplete)


def test_adapter_rejects_allowlisted_script_symlink_escape(tmp_path):
    candidate = _audited_candidate(tmp_path / "candidate")
    script = candidate / "scripts/trade/get_accounts.py"
    script.unlink()
    outside = tmp_path / "outside.py"
    outside.write_text("print('{}')\n", encoding="utf-8")
    script.symlink_to(outside)

    with pytest.raises(ValueError, match="escapes Skill directory"):
        FutuAdapter(candidate)


def test_doctor_direct_skill_argument_cannot_bypass_validation(tmp_path):
    fake = _candidate(tmp_path / "fake", name="not-futuapi")

    report = run_doctor(
        skill_dir=fake,
        environment_probe=lambda *_args: {
            "sdk": {"available": False, "version": None, "detail": "missing"},
            "opend": {"available": False, "detail": "not checked"},
        },
    )

    assert report["checks"]["futuapi_skill"]["available"] is False
    assert "digest mismatch" in report["checks"]["futuapi_skill"]["detail"]
