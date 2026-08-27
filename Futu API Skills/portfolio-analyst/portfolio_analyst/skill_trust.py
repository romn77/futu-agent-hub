"""Central trust validation for the official futuapi Skill."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


READ_ONLY_SCRIPTS = {
    "get_accounts": Path("scripts/trade/get_accounts.py"),
    "get_portfolio": Path("scripts/trade/get_portfolio.py"),
    "get_all_portfolios": Path("scripts/trade/get_all_portfolios.py"),
}


@dataclass(frozen=True)
class SkillValidation:
    root: Path | None
    detail: str

    @property
    def valid(self) -> bool:
        return self.root is not None


def _frontmatter_name(skill_file: Path) -> str | None:
    try:
        lines = skill_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if not lines or lines[0].strip() != "---":
        return None
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return None
    for line in lines[1:closing]:
        match = re.fullmatch(r"\s*name\s*:\s*([^#]+?)\s*", line)
        if match:
            return match.group(1).strip().strip("\"'")
    return None


def validate_futuapi_skill(candidate: Path) -> SkillValidation:
    """Validate identity, layout, and containment of a futuapi Skill candidate."""

    try:
        root = candidate.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return SkillValidation(None, "candidate directory does not exist")
    if not root.is_dir():
        return SkillValidation(None, "candidate is not a directory")

    skill_file = root / "SKILL.md"
    try:
        resolved_skill_file = skill_file.resolve(strict=True)
    except (OSError, RuntimeError):
        return SkillValidation(None, "SKILL.md is missing")
    if not resolved_skill_file.is_relative_to(root) or not resolved_skill_file.is_file():
        return SkillValidation(None, "SKILL.md escapes Skill directory")
    if _frontmatter_name(resolved_skill_file) != "futuapi":
        return SkillValidation(None, "SKILL.md frontmatter must declare name: futuapi")

    for relative_script in READ_ONLY_SCRIPTS.values():
        script = root / relative_script
        try:
            resolved_script = script.resolve(strict=True)
        except (OSError, RuntimeError):
            return SkillValidation(None, f"required script is missing: {relative_script}")
        if not resolved_script.is_relative_to(root):
            return SkillValidation(
                None, f"required script escapes Skill directory: {relative_script}"
            )
        if not resolved_script.is_file():
            return SkillValidation(None, f"required script is not a file: {relative_script}")

    return SkillValidation(root, "verified futuapi Skill identity and read-only script layout")
