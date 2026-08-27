from pathlib import Path

import pytest

from portfolio_analyst.doctor import run_doctor
from portfolio_analyst.futu_adapter import FutuAdapter
from portfolio_analyst.settings import discover_futuapi_skill


def _candidate(path: Path, *, name: str = "futuapi") -> Path:
    (path / "scripts" / "trade").mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    for script in FutuAdapter.READ_ONLY_SCRIPTS.values():
        target = path / script
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("print('{}')\n", encoding="utf-8")
    return path


def test_arbitrary_skill_md_directory_is_not_trusted(monkeypatch, tmp_path):
    fake = _candidate(tmp_path / "fake", name="portfolio-analyst")
    monkeypatch.setenv("FUTUAPI_SKILL_DIR", str(fake))

    assert discover_futuapi_skill(repo_root=tmp_path, cwd=tmp_path) is None
    with pytest.raises(ValueError, match="verified futuapi Skill"):
        FutuAdapter(fake)


def test_adapter_rejects_missing_required_allowlisted_script(tmp_path):
    incomplete = _candidate(tmp_path / "incomplete")
    (incomplete / FutuAdapter.READ_ONLY_SCRIPTS["get_all_portfolios"]).unlink()

    with pytest.raises(ValueError, match="required script"):
        FutuAdapter(incomplete)


def test_adapter_rejects_allowlisted_script_symlink_escape(tmp_path):
    candidate = _candidate(tmp_path / "candidate")
    script = candidate / FutuAdapter.READ_ONLY_SCRIPTS["get_accounts"]
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
        sdk_probe=lambda: (None, None, "missing"),
        opend_probe=lambda *_args: pytest.fail("must not probe OpenD"),
    )

    assert report["checks"]["futuapi_skill"]["available"] is False
    assert "frontmatter" in report["checks"]["futuapi_skill"]["detail"]
