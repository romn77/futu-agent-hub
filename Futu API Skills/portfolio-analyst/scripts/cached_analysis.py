#!/usr/bin/env python3
"""Explicit cache-aware orchestration; the legacy helper remains available separately."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio_analyst.cache.workflow_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
