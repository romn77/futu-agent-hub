"""Configuration and private data path handling."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .skill_trust import validate_futuapi_skill


DEFAULT_DATA_HOME = Path("~/.moomoo-portfolio-agent")


class SettingsError(ValueError):
    """Invalid runtime configuration before command execution."""


@dataclass(frozen=True)
class FutuSkillSource:
    path: Path | None
    kind: str
    detail: str


def _repository_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    return None


@dataclass(frozen=True)
class Settings:
    """Runtime settings derived from non-secret environment variables."""

    data_home: Path
    opend_host: str = "127.0.0.1"
    opend_port: int = 11111

    @classmethod
    def from_env(cls) -> "Settings":
        raw_home = os.environ.get("PORTFOLIO_AGENT_HOME", str(DEFAULT_DATA_HOME))
        try:
            requested_home = Path(raw_home).expanduser().absolute()
            data_home = requested_home.resolve()
        except (OSError, RuntimeError) as exc:
            raise SettingsError("PORTFOLIO_AGENT_HOME is invalid") from exc
        if requested_home != data_home:
            raise SettingsError("PORTFOLIO_AGENT_HOME must not contain symlink components")
        try:
            repository = _repository_root()
        except (OSError, RuntimeError) as exc:
            raise SettingsError("repository path configuration is invalid") from exc
        if repository is not None and data_home.is_relative_to(repository):
            raise SettingsError("PORTFOLIO_AGENT_HOME must be outside the Git repository")
        host = os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
        try:
            port = int(os.environ.get("FUTU_OPEND_PORT", "11111"))
        except ValueError as exc:
            raise SettingsError("FUTU_OPEND_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise SettingsError("FUTU_OPEND_PORT must be between 1 and 65535")
        return cls(data_home=data_home, opend_host=host, opend_port=port)


def resolve_output_path(data_home: Path, requested: str | Path) -> Path:
    """Resolve a relative output path and keep it below the private data home."""

    home = data_home.expanduser().resolve()
    candidate_path = Path(requested)
    if candidate_path.is_absolute():
        raise ValueError("output paths must be relative to PORTFOLIO_AGENT_HOME")
    candidate = (home / candidate_path).resolve()
    if not candidate.is_relative_to(home) or candidate == home:
        raise ValueError("output path must remain inside PORTFOLIO_AGENT_HOME")
    return candidate


def discover_futuapi_skill_source(
    *, repo_root: Path | None = None, global_roots: list[Path] | None = None
) -> FutuSkillSource:
    """Find a compatible Skill in explicit, repository, then known global locations."""

    explicit = os.environ.get("FUTUAPI_SKILL_DIR")
    here = Path(__file__).resolve()
    inferred_repo = here.parents[3]
    root = (repo_root or inferred_repo).resolve()
    roots = global_roots
    if roots is None:
        roots = [
            Path.home() / ".claude" / "skills",
            Path.home() / ".codex" / "skills",
            Path.home() / ".agents" / "skills",
        ]
    candidates: list[tuple[Path, str, str]] = []
    if explicit:
        candidates.append(
            (
                Path(explicit),
                "explicit",
                "audited-compatible futuapi Skill from user-configured FUTUAPI_SKILL_DIR",
            )
        )
    candidates.extend(
        [
            (
                root / "Futu API Skills" / "futuapi",
                "repository",
                "audited-compatible futuapi Skill from repository layout",
            ),
            (
                root / "futuapi",
                "repository",
                "audited-compatible futuapi Skill from repository layout",
            ),
        ]
    )
    candidates.extend(
        (
            global_root / "futuapi",
            "global",
            "audited-compatible futuapi Skill from known global root",
        )
        for global_root in roots
    )
    for candidate, kind, detail in candidates:
        validation = validate_futuapi_skill(candidate)
        if validation.valid:
            return FutuSkillSource(validation.root, kind, detail)
    return FutuSkillSource(None, "missing", "audited-compatible futuapi Skill not found")


def discover_futuapi_skill(
    *, repo_root: Path | None = None, global_roots: list[Path] | None = None
) -> Path | None:
    """Return only the path from ordered compatible-Skill discovery."""

    return discover_futuapi_skill_source(repo_root=repo_root, global_roots=global_roots).path
