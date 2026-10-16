#!/usr/bin/env python3
"""Run from the existing research/test interpreter; no installation required."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sec_research.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
