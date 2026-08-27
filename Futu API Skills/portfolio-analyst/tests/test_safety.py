import ast
from pathlib import Path

from portfolio_analyst.futu_adapter import FutuAdapter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
DANGEROUS_CALLS = {
    "place_order",
    "modify_order",
    "cancel_order",
    "unlock_trade",
    "place_combo_order",
    "buy",
    "sell",
}


def test_production_code_contains_no_order_entry_calls_or_shell_true():
    violations = []
    production_files = list((PROJECT_ROOT / "portfolio_analyst").rglob("*.py"))
    production_files.extend((PROJECT_ROOT / "scripts").rglob("*.py"))
    for path in production_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", "")
                )
                if name.lower() in DANGEROUS_CALLS:
                    violations.append(f"{path.name}:{node.lineno}:{name}")
                for keyword in node.keywords:
                    if (
                        keyword.arg == "shell"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                    ):
                        violations.append(f"{path.name}:{node.lineno}:shell=True")

    assert violations == []


def test_adapter_allowlist_has_no_dangerous_terms():
    for command, relative_path in FutuAdapter.READ_ONLY_SCRIPTS.items():
        combined = f"{command} {relative_path}".lower()
        assert all(term not in combined for term in DANGEROUS_CALLS)


def test_gitignore_protects_private_portfolio_artifacts():
    ignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
    for required in (
        ".env*",
        "accounts/",
        "snapshots/",
        "trades/",
        "credentials/",
        "personal-rules/",
        "reports/",
    ):
        assert required in ignore
