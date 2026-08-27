from pathlib import Path

import pytest

from portfolio_analyst.futu_adapter import FutuAdapter, UnsafeFutuCommand


def _skill(tmp_path: Path) -> Path:
    skill = tmp_path / "futuapi"
    (skill / "scripts" / "trade").mkdir(parents=True)
    for name in ("get_accounts.py", "get_portfolio.py", "place_order.py"):
        (skill / "scripts" / "trade" / name).write_text("print('{}')\n", encoding="utf-8")
    return skill


def test_adapter_exposes_only_hard_coded_read_only_scripts(tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))

    assert adapter.command("get_accounts", ["--json"]) == [
        adapter.python_executable,
        str(adapter.skill_dir / "scripts" / "trade" / "get_accounts.py"),
        "--json",
    ]

    with pytest.raises(UnsafeFutuCommand):
        adapter.command("place_order", [])

    with pytest.raises(UnsafeFutuCommand):
        adapter.command("../../tmp/custom", [])


def test_read_only_real_query_never_accepts_a_password(tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))

    command = adapter.command("get_portfolio", ["--trd-env", "REAL", "--json"])

    assert "REAL" in command
    assert all("password" not in item.lower() and "pwd" not in item.lower() for item in command)

    with pytest.raises(UnsafeFutuCommand, match="credential"):
        adapter.command("get_portfolio", ["--trd-env", "REAL", "--password", "secret"])
