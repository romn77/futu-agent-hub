#!/usr/bin/env python3
"""Run one lifecycle-free current portfolio acquisition."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_analyst.current_analysis import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
