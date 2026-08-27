from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest

from portfolio_analyst import cli
from portfolio_analyst.persistence import SnapshotStore
from portfolio_analyst.risk import PricePoint, RiskConfigError, _pearson, analyze_risk
from portfolio_analyst.settings import Settings

from test_risk_analysis import snapshot


def test_decimal_pearson_handles_values_that_overflow_float():
    scale = Decimal("1e10000")
    left = tuple(scale * value for value in map(Decimal, (1, 2, 3, 4)))

    assert _pearson(left, left) == Decimal("1")


def test_decimal_pearson_handles_values_that_underflow_float():
    scale = Decimal("1e-10000")
    left = tuple(scale * value for value in map(Decimal, (1, 2, 3, 4)))

    assert _pearson(left, left) == Decimal("1")


@pytest.mark.parametrize("nonfinite", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_decimal_pearson_rejects_nonfinite_inputs_without_decimal_signal(nonfinite):
    assert _pearson((Decimal("1"), nonfinite), (Decimal("1"), Decimal("2"))) is None


def test_decimal_pearson_rounding_is_deterministic_at_documented_precision():
    assert _pearson(
        (Decimal(0), Decimal(1), Decimal(2)),
        (Decimal(0), Decimal(1), Decimal(1)),
    ) == Decimal("0.86602540378443864676372317075293618347140262690523")


def test_configured_correlation_thresholds_are_serialized_as_decimal_evidence():
    report = analyze_risk(
        snapshot(),
        medium_correlation=Decimal("0.60"),
        high_correlation=Decimal("0.80"),
    )
    assert report.correlation.medium_threshold == Decimal("0.60")
    assert report.correlation.high_threshold == Decimal("0.80")
    payload = report.to_dict()["correlation"]
    assert payload["medium_threshold"] == "0.6"
    assert payload["high_threshold"] == "0.8"


def test_tiny_adjusted_daily_returns_survive_return_alignment_without_float_loss():
    with localcontext() as context:
        context.prec = 60
        level = Decimal("1")
        points = []
        for offset in range(70):
            level *= Decimal("1") + Decimal(1 + offset % 3) * Decimal("1e-40")
            points.append(PricePoint(date(2026, 1, 1) + timedelta(days=offset), level))

    class TinyReturns:
        def history(self, symbol, *, market, lookback, as_of):
            del market, lookback, as_of
            return tuple(points) if symbol in {"AAA", "BBB"} else None

    report = analyze_risk(snapshot(), price_provider=TinyReturns())
    assert report.correlation.pairs[0].coefficient == Decimal("1")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lookback": True},
        {"lookback": 1},
        {"lookback": 5001},
        {"minimum_observations": True},
        {"minimum_observations": 0},
        {"lookback": 60, "minimum_observations": 60},
        {"high_correlation": Decimal("NaN")},
        {"high_correlation": Decimal("Infinity")},
        {"medium_correlation": Decimal("-0.01")},
        {"high_correlation": Decimal("1.01")},
        {"medium_correlation": Decimal("0.8"), "high_correlation": Decimal("0.7")},
        {"medium_correlation": True},
    ],
)
def test_risk_configuration_rejects_invalid_values_before_computation(kwargs):
    with pytest.raises(RiskConfigError):
        analyze_risk(snapshot(), **kwargs)


def test_cli_invalid_lookback_is_structured_config_error_and_stdout_pure(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    home.mkdir()
    SnapshotStore(home).save("snapshots/source.json", json.dumps(snapshot().to_dict()))
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    assert (
        cli.main(
            [
                "risk",
                "--snapshot",
                "snapshots/source.json",
                "--lookback",
                "60",
                "--json",
                "--no-save",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["code"] == "CONFIG_ERROR"
