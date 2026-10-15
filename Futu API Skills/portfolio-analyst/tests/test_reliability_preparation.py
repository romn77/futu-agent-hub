"""Prospective cases use real deterministic libraries with synthetic-only boundaries."""

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def adapter():
    spec = importlib.util.spec_from_file_location(
        "reliability_preparation", ROOT / "evals/prepare_reliability.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def registry():
    return {
        "schema_version": "1.0",
        "synthetic": True,
        "evaluation_as_of": "2026-10-15",
        "model_context": "Use only the supplied synthetic interfaces.",
        "cases": [
            {
                "id": "T01",
                "prompt": "分析当前组合。",
                "public_data": {"futu/financials": {"US.AAPL": {"revenue": 10}}},
                "portfolio_input": {
                    "base_currency": "USD",
                    "as_of": "2026-10-15T12:00:00Z",
                    "trd_env": "REAL",
                    "source_version": "SYNTHETIC_EVAL_ONLY",
                    "accounts": [
                        {
                            "account_ref": "acct-synthetic",
                            "trd_env": "REAL",
                            "account_type": "CASH",
                            "account_role": "NORMAL",
                            "markets": ["US"],
                            "status": "ACTIVE",
                        }
                    ],
                    "account_navs": [
                        {
                            "account_ref": "acct-synthetic",
                            "currency": "USD",
                            "total_assets_local": "10000",
                        }
                    ],
                    "cash_balances": [
                        {"account_ref": "acct-synthetic", "currency": "USD", "amount_local": "2000"}
                    ],
                    "positions": [
                        {
                            "account_ref": "acct-synthetic",
                            "code": "US.AAPL",
                            "name": "Synthetic AAPL",
                            "security_type": "STOCK",
                            "currency": "USD",
                            "currency_provenance": "AUDITED_SOURCE",
                            "quantity": "80",
                            "can_sell_quantity": "80",
                            "average_cost_local": "110",
                            "last_price_local": "100",
                            "market_value_local": "8000",
                            "unrealized_pl_local": "-800",
                            "realized_pl_local": "0",
                        }
                    ],
                    "classifications": {
                        "US.AAPL": {
                            "sector": "SYNTHETIC",
                            "industry": "SYNTHETIC",
                            "region": "US",
                            "themes": ["FIXTURE"],
                            "asset_class": "EQUITY",
                        }
                    },
                    "data_quality": {
                        "status": "COMPLETE",
                        "coverage": {"accounts_returned": 1, "accounts_returned_read": 1},
                    },
                },
            }
        ],
    }


def test_prepared_case_is_bound_valid_and_contains_no_operator_answers(tmp_path):
    from portfolio_analyst.snapshot_io import snapshot_from_document
    from portfolio_analyst.schema import validate_technical_document

    data = registry()
    output = tmp_path / "run"
    result = adapter().prepare_case(data, "T01", output, ROOT)
    workspace = output / "workspace"
    envelope = json.loads((workspace / ".fixture/envelope.json").read_text())
    snapshot_from_document(envelope["snapshot"])
    validate_technical_document(envelope["technical"])
    assert envelope["snapshot"]["source_version"] == "SYNTHETIC_EVAL_ONLY"
    assert envelope["snapshot"]["positions"][0]["unrealized_pl_base"] == "-800"
    assert envelope["technical"]["source_snapshot_id"] == envelope["snapshot"]["snapshot_id"]
    assert not (workspace / "evals").exists()
    assert not (workspace / ".fixture/request.json").exists()
    assert (workspace / "research/data.json").read_text().strip() == json.dumps(
        data["cases"][0]["public_data"], ensure_ascii=False, indent=2
    )
    assert result["synthetic"] is True
    assert "T01" not in (output / "prompt.txt").read_text()
    assert all((workspace / name).is_file() for name in result["files"])


def test_preparation_never_overwrites_an_existing_attempt(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep").write_text("Evidence")
    with pytest.raises(FileExistsError):
        adapter().prepare_case(registry(), "T01", output, ROOT)
    assert (output / "keep").read_text() == "Evidence"


def test_non_synthetic_registry_is_rejected_before_writing(tmp_path):
    data = registry()
    data["synthetic"] = False
    with pytest.raises(ValueError, match="synthetic"):
        adapter().prepare_case(data, "T01", tmp_path / "run", ROOT)
    assert not (tmp_path / "run").exists()


def test_unknown_case_does_not_fall_back_to_a_different_portfolio(tmp_path):
    with pytest.raises(ValueError, match="case"):
        adapter().prepare_case(registry(), "missing", tmp_path / "run", ROOT)
    assert not (tmp_path / "run").exists()
