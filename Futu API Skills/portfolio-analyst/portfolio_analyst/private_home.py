"""Private state-home validation shared by persistent repositories."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class PrivateHomeError(ValueError):
    pass


def ensure_private_home(data_home: Path, *, create: bool = False) -> Path:
    """Return an absolute non-linked private directory, optionally creating it."""

    home = data_home.expanduser().absolute()
    if create:
        try:
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise PrivateHomeError("PORTFOLIO_AGENT_HOME cannot be created safely") from exc
    try:
        metadata = home.lstat()
    except OSError as exc:
        raise PrivateHomeError("PORTFOLIO_AGENT_HOME is unavailable") from exc
    is_junction = getattr(home, "is_junction", lambda: False)
    if not stat.S_ISDIR(metadata.st_mode) or home.is_symlink() or bool(is_junction()):
        raise PrivateHomeError("PORTFOLIO_AGENT_HOME must be a real directory")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PrivateHomeError("PORTFOLIO_AGENT_HOME must have mode 0700")
    return home


def validate_simple_component(value: str, *, label: str) -> str:
    candidate = Path(value)
    if candidate.parts != (value,) or value in {"", ".", ".."}:
        raise PrivateHomeError(f"{label} must be a simple confined name")
    return value
