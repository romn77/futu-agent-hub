"""Locate the same policy resources in source checkouts and installed wheels."""

from pathlib import Path
import sys


def policy_root():
    for root in (Path(__file__).resolve().parents[2],
                 Path(sys.prefix) / "share/moomoo-portfolio-analyst"):
        if (root / "SKILL.md").is_file() and (root / "references").is_dir():
            return root
    raise FileNotFoundError("portfolio analysis policies are not installed")
