"""Prepare frozen synthetic reliability inputs; never invoke a model or live provider."""

import argparse
from datetime import datetime
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def build_fixture(raw):
    from portfolio_analyst.classification import Classification, ClassificationMapper
    from portfolio_analyst.fx import IdentityOnlyCurrencyConverter
    from portfolio_analyst.models import Account, AccountNAV, CashBalance, DataQuality, Position
    from portfolio_analyst.risk import analyze_risk
    from portfolio_analyst.schema import validate_technical_document
    from portfolio_analyst.snapshot import PortfolioBuilder
    from portfolio_analyst.snapshot_io import snapshot_from_document
    from portfolio_analyst.technical import analyze_technical

    def decimals(row, names):
        return {
            key: Decimal(value) if key in names and value is not None else value
            for key, value in row.items()
        }

    if raw["base_currency"] != "USD" or raw["trd_env"] != "REAL":
        raise ValueError("reliability fixtures require REAL/USD synthetic scope")
    if not raw["source_version"].upper().startswith("SYNTHETIC"):
        raise ValueError("synthetic source label required")
    if any(row["security_type"] == "OPTION" for row in raw["positions"]):
        raise ValueError("use the existing options fixture for option-contract cases")
    position_fields = {
        "quantity",
        "can_sell_quantity",
        "average_cost_local",
        "last_price_local",
        "market_value_local",
        "unrealized_pl_local",
        "realized_pl_local",
    }
    snapshot = PortfolioBuilder(
        base_currency="USD",
        converter=IdentityOnlyCurrencyConverter(),
        classifications=ClassificationMapper(
            {key: Classification(**value) for key, value in raw["classifications"].items()}
        ),
    ).build(
        accounts=tuple(Account(**row) for row in raw["accounts"]),
        account_navs=tuple(
            AccountNAV(**decimals(row, {"total_assets_local"})) for row in raw["account_navs"]
        ),
        cash_balances=tuple(
            CashBalance(**decimals(row, {"amount_local"})) for row in raw["cash_balances"]
        ),
        positions=tuple(Position(**decimals(row, position_fields)) for row in raw["positions"]),
        as_of=datetime.fromisoformat(raw["as_of"].replace("Z", "+00:00")),
        trd_env="REAL",
        source_version=raw["source_version"],
        data_quality=DataQuality(**raw["data_quality"]),
    )
    risk = analyze_risk(snapshot).to_dict()
    technical = analyze_technical(snapshot, provider=None, risk_document=risk).to_dict()
    snapshot_from_document(snapshot.to_dict())
    validate_technical_document(technical)
    return {
        "snapshot": snapshot.to_dict(),
        "risk": risk,
        "technical": technical,
        "options": None,
        "degradations": [],
    }


def prepare_case(registry, case_id, output, instructions):
    if registry.get("synthetic") is not True:
        raise ValueError("only an explicitly synthetic registry is accepted")
    cases = [case for case in registry["cases"] if case["id"] == case_id]
    if len(cases) != 1:
        raise ValueError("case must identify exactly one synthetic input")
    case = cases[0]
    fixture = build_fixture(case["portfolio_input"])
    spec = importlib.util.spec_from_file_location(
        "reliability_fixture_producer", ROOT / "evals/run_codex_evals.py"
    )
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    workspace = output / "workspace"
    producer.prepare_workspace(workspace, case_id, fixture=fixture, instructions=instructions)
    research = workspace / "research"
    research.mkdir(mode=0o700)
    shutil.copy2(ROOT / "evals/research-fixtures/public_reader.py", research / "public_reader.py")
    producer._write(
        research / "data.json",
        (json.dumps(case["public_data"], ensure_ascii=False, indent=2) + "\n").encode(),
    )
    prompt = registry["model_context"] + "\n\n" + case["prompt"] + "\n"
    producer._write(output / "prompt.txt", prompt.encode())
    manifest = {
        str(path.relative_to(workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
    }
    record = {
        "synthetic": True,
        "case_id": case_id,
        "files": manifest,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
    }
    producer._write(output / "inputs.json", producer._json_bytes(record))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--instructions", type=Path, default=ROOT)
    args = parser.parse_args()
    record = prepare_case(
        json.loads(args.registry.read_text()), args.case, args.output, args.instructions
    )
    print(
        json.dumps({"case_id": record["case_id"], "synthetic": True, "files": len(record["files"])})
    )


if __name__ == "__main__":
    main()
