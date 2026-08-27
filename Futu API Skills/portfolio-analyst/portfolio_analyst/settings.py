"""Configuration and private data path handling."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .skill_trust import validate_futuapi_skill


DEFAULT_DATA_HOME = Path("~/.moomoo-portfolio-agent")


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
        data_home = Path(raw_home).expanduser().resolve()
        repository = _repository_root()
        if repository is not None and data_home.is_relative_to(repository):
            raise ValueError("PORTFOLIO_AGENT_HOME must be outside the Git repository")
        host = os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
        try:
            port = int(os.environ.get("FUTU_OPEND_PORT", "11111"))
        except ValueError as exc:
            raise ValueError("FUTU_OPEND_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("FUTU_OPEND_PORT must be between 1 and 65535")
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


def discover_futuapi_skill(
    *, repo_root: Path | None = None, cwd: Path | None = None
) -> Path | None:
    """Find the official futuapi Skill without accepting user-selected scripts."""

    explicit = os.environ.get("FUTUAPI_SKILL_DIR")
    here = Path(__file__).resolve()
    inferred_repo = here.parents[3]
    root = (repo_root or inferred_repo).resolve()
    current = (cwd or Path.cwd()).resolve()
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            root / "Futu API Skills" / "futuapi",
            root / "futuapi",
            current / "Futu API Skills" / "futuapi",
            current / "futuapi",
            current / "skills" / "futuapi",
            Path.home() / ".claude" / "skills" / "futuapi",
            Path.home() / ".codex" / "skills" / "futuapi",
            Path.home() / ".agents" / "skills" / "futuapi",
        ]
    )
    for candidate in candidates:
        validation = validate_futuapi_skill(candidate)
        if validation.valid:
            return validation.root
    return None
