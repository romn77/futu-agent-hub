"""Shared subprocess isolation policy for audited Futu execution and diagnostics."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal


# Paths, home/temp locations, locale, and core Windows process settings are the
# minimum inherited runtime context. APPDATA and all loader/Python paths stay excluded.
_SAFE_RUNTIME_KEYS = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)

# This is the complete non-login configuration read by the audited common.py.
# FUTU_ACC_ID is sensitive but required for deterministic multi-account selection;
# it is passed only to the child and is covered by adapter output sanitization.
_SAFE_QUERY_FUTU_KEYS = (
    "FUTU_OPEND_HOST",
    "FUTU_OPEND_PORT",
    "FUTU_TRD_ENV",
    "FUTU_DEFAULT_MARKET",
    "FUTU_SECURITY_FIRM",
)


def validate_account_id(value: str) -> str:
    """Validate the shared account-ID contract without echoing sensitive input."""

    if re.fullmatch(r"[1-9][0-9]{4,19}", value) is None:
        raise ValueError("account ID must be a valid positive decimal identifier")
    return value


def controlled_child_environment(
    source: Mapping[str, str] | None = None,
    *,
    purpose: Literal["adapter", "doctor"],
) -> dict[str, str]:
    """Return an allowlisted environment with credentials and import hooks omitted."""

    parent = source if source is not None else os.environ
    environment = {
        key: parent[key] for key in (*_SAFE_RUNTIME_KEYS, *_SAFE_QUERY_FUTU_KEYS) if key in parent
    }
    if purpose == "adapter" and "FUTU_ACC_ID" in parent:
        account_id = validate_account_id(parent["FUTU_ACC_ID"])
        environment["FUTU_ACC_ID"] = account_id
    environment.setdefault("FUTU_OPEND_HOST", "127.0.0.1")
    environment.setdefault("FUTU_OPEND_PORT", "11111")
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return environment


def isolated_python_command(script: Path, arguments: Sequence[str] = ()) -> list[str]:
    """Use the selected interpreter while disabling its untrusted user site."""

    return [sys.executable, "-s", str(script), *map(str, arguments)]
