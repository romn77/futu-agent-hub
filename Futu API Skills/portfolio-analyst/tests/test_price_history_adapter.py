from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from portfolio_analyst.futu_adapter import FutuAdapter, UnsafeFutuCommand
from portfolio_analyst.price_history import FutuAdjustedPriceProvider
from portfolio_analyst.technical import FutuTechnicalHistoryProvider
from portfolio_analyst.skill_trust import audited_files


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_SKILL = PROJECT_ROOT.parent / "futuapi"


def _skill(tmp_path: Path) -> Path:
    skill = tmp_path / "futuapi"
    for record in audited_files():
        destination = skill / record.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OFFICIAL_SKILL / record.relative_path, destination)
    return skill


def test_adjusted_daily_history_command_has_exact_read_only_option_contract(tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    request = adapter.validate_request(
        "get_kline",
        [
            "US.AAPL",
            "--ktype",
            "1d",
            "--start",
            "2026-01-01",
            "--end",
            "2026-08-27",
            "--rehab",
            "forward",
            "--session",
            "NONE",
            "--num",
            "1000",
            "--json",
        ],
    )
    assert request.command == "get_kline"
    assert request.arguments[0] == "US.AAPL"

    for invalid in (
        ["US.AAPL", "--rehab", "none", "--json"],
        ["../../evil", "--rehab", "forward", "--json"],
        ["US.AAPL", "--ktype", "1m", "--rehab", "forward", "--json"],
    ):
        with pytest.raises(UnsafeFutuCommand):
            adapter.validate_request("get_kline", invalid)


def test_provider_parses_only_forward_adjusted_monotonic_daily_closes(monkeypatch, tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    payload = {
        "code": "US.AAPL",
        "ktype": "1d",
        "source": "history",
        "data": [
            {"time": "2026-08-25 00:00:00", "close": 100.0},
            {"time": "2026-08-26 00:00:00", "close": 101.0},
        ],
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        ),
    )

    points = FutuAdjustedPriceProvider(adapter).history(
        "AAPL", market="US", lookback=120, as_of=date(2026, 8, 27)
    )
    assert [(point.date.isoformat(), str(point.adjusted_close)) for point in points] == [
        ("2026-08-25", "100.0"),
        ("2026-08-26", "101.0"),
    ]


def test_provider_forwards_bounded_per_call_timeout_to_audited_adapter():
    observed = {}

    class Adapter:
        def read_private_json(self, name, arguments, *, timeout):
            observed.update(name=name, arguments=arguments, timeout=timeout)
            return {"code": "US.AAPL", "ktype": "1d", "source": "history", "data": []}

    FutuAdjustedPriceProvider(Adapter()).history(
        "AAPL",
        market="US",
        lookback=120,
        as_of=date(2026, 8, 27),
        timeout_seconds=1.25,
    )

    assert observed["timeout"] == 1.25


def test_weekly_kline_has_a_distinct_bounded_read_only_contract(tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    request = adapter.validate_request(
        "get_kline",
        [
            "US.AAPL",
            "--ktype",
            "1w",
            "--start",
            "2024-08-27",
            "--end",
            "2026-08-27",
            "--rehab",
            "forward",
            "--session",
            "NONE",
            "--num",
            "110",
            "--json",
        ],
    )

    assert request.arguments[2] == "1w"
    with pytest.raises(UnsafeFutuCommand):
        adapter.validate_request(
            "get_kline",
            [
                "US.AAPL",
                "--ktype",
                "1w",
                "--start",
                "2023-08-01",
                "--end",
                "2026-08-27",
                "--rehab",
                "forward",
                "--session",
                "NONE",
                "--num",
                "110",
                "--json",
            ],
        )


def test_technical_provider_uses_exact_daily_and_weekly_windows_and_identity():
    calls = []

    class Adapter:
        def read_private_json(self, name, arguments, *, timeout):
            calls.append((name, arguments, timeout))
            interval = arguments[arguments.index("--ktype") + 1]
            return {
                "code": "US.AAPL",
                "ktype": interval,
                "source": "history",
                "data": [
                    {
                        "time": "2026-08-26 00:00:00",
                        "open": "100",
                        "high": "103",
                        "low": "99",
                        "close": "102",
                        "volume": "12345",
                    }
                ],
            }

    provider = FutuTechnicalHistoryProvider(Adapter())
    daily = provider.bars(
        "AAPL", market="US", interval="1d", as_of=date(2026, 8, 27), timeout_seconds=5
    )
    weekly = provider.bars(
        "AAPL", market="US", interval="1w", as_of=date(2026, 8, 27), timeout_seconds=5
    )

    assert daily[0].close == 102
    assert weekly[0].volume == 12345
    assert calls[0][1][calls[0][1].index("--start") + 1] == "2026-02-08"
    assert calls[0][1][calls[0][1].index("--num") + 1] == "160"
    assert calls[1][1][calls[1][1].index("--start") + 1] == "2024-08-22"
    assert calls[1][1][calls[1][1].index("--num") + 1] == "110"
    assert all(call[2] == 5 for call in calls)
