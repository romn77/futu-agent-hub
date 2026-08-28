"""Private state-home validation shared by persistent repositories."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class PrivateHomeError(ValueError):
    pass


def require_secure_platform_mutation() -> None:
    """Fail before persistent state is touched on unsupported platforms."""

    if os.name != "posix":
        raise PrivateHomeError("UNSUPPORTED_PLATFORM_SECURITY: persistent mutation disabled")


def ensure_private_home(data_home: Path, *, create: bool = False) -> Path:
    """Return an absolute non-linked private directory, optionally creating it."""

    home = data_home.expanduser().absolute()
    if create:
        require_secure_platform_mutation()
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        from .persistence import open_or_create_private_directory

        current_fd = os.open("/", flags)
        try:
            for index, component in enumerate(home.parts[1:], start=1):
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    try:
                        next_fd = open_or_create_private_directory(current_fd, component, flags)
                    except (OSError, ValueError) as exc:
                        raise PrivateHomeError(
                            "PORTFOLIO_AGENT_HOME cannot be created safely"
                        ) from exc
                if index == len(home.parts) - 1:
                    metadata = os.fstat(next_fd)
                    if stat.S_IMODE(metadata.st_mode) != 0o700:
                        os.close(next_fd)
                        raise PrivateHomeError("PORTFOLIO_AGENT_HOME must have mode 0700")
                os.close(current_fd)
                current_fd = next_fd
        except OSError as exc:
            raise PrivateHomeError("PORTFOLIO_AGENT_HOME cannot be created safely") from exc
        finally:
            os.close(current_fd)
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
