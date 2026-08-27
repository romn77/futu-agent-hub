"""Narrow subprocess boundary around approved read-only official scripts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence


class UnsafeFutuCommand(ValueError):
    """Raised when a command falls outside the fixed read-only boundary."""


class FutuAdapter:
    """Execute only audited, read-only scripts from the official futuapi Skill."""

    READ_ONLY_SCRIPTS = {
        "get_accounts": Path("scripts/trade/get_accounts.py"),
        "get_portfolio": Path("scripts/trade/get_portfolio.py"),
        "get_all_portfolios": Path("scripts/trade/get_all_portfolios.py"),
    }
    DENY_TERMS = frozenset(
        {
            "place_order",
            "modify_order",
            "cancel_order",
            "unlock_trade",
            "place_combo_order",
            "buy",
            "sell",
        }
    )
    CREDENTIAL_FLAG_TERMS = frozenset({"password", "passwd", "pwd", "unlock"})

    def __init__(self, skill_dir: Path, *, python_executable: str | None = None) -> None:
        self.skill_dir = skill_dir.expanduser().resolve()
        self.python_executable = python_executable or sys.executable
        if not self.skill_dir.is_dir():
            raise FileNotFoundError(f"futuapi Skill directory not found: {self.skill_dir}")

    def command(self, name: str, arguments: Sequence[str]) -> list[str]:
        """Build an argument-array command for an approved script."""

        normalized = name.lower()
        if any(term in normalized for term in self.DENY_TERMS):
            raise UnsafeFutuCommand(f"write-capable Futu command is forbidden: {name}")
        relative_script = self.READ_ONLY_SCRIPTS.get(name)
        if relative_script is None:
            raise UnsafeFutuCommand(f"Futu command is not in the read-only allowlist: {name}")
        option_names = (
            str(argument).lower() for argument in arguments if str(argument).startswith("-")
        )
        if any(term in option for option in option_names for term in self.CREDENTIAL_FLAG_TERMS):
            raise UnsafeFutuCommand("credential and unlock arguments are forbidden")
        script = (self.skill_dir / relative_script).resolve()
        if not script.is_relative_to(self.skill_dir) or not script.is_file():
            raise FileNotFoundError(f"approved futuapi script not found: {relative_script}")
        return [self.python_executable, str(script), *map(str, arguments)]

    def run(
        self, name: str, arguments: Sequence[str], *, timeout: float = 30
    ) -> subprocess.CompletedProcess[str]:
        """Run an approved read-only command without a shell."""

        return subprocess.run(
            self.command(name, arguments),
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
