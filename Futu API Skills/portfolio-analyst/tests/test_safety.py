import ast
import subprocess
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


def _scan_python_files(paths):
    violations = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if name.lower() in DANGEROUS_CALLS:
                violations.append(f"{path}:{node.lineno}:{name}")
            for keyword in node.keywords:
                if (
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    violations.append(f"{path}:{node.lineno}:shell=True")
    return violations


def test_production_code_contains_no_order_entry_calls_or_shell_true():
    production_files = list((PROJECT_ROOT / "portfolio_analyst").rglob("*.py"))
    production_files.extend((PROJECT_ROOT / "scripts").rglob("*.py"))

    assert _scan_python_files(production_files) == []


def test_every_resolved_allowlisted_official_script_is_read_only():
    skill_dir = REPOSITORY_ROOT / "Futu API Skills" / "futuapi"
    adapter = FutuAdapter(skill_dir)
    resolved_scripts = [Path(adapter.command(name, [])[1]) for name in adapter.READ_ONLY_SCRIPTS]

    assert len(resolved_scripts) == len(adapter.READ_ONLY_SCRIPTS)
    assert all(path.is_file() and path.is_relative_to(skill_dir) for path in resolved_scripts)
    assert _scan_python_files(resolved_scripts) == []


def test_adapter_allowlist_has_no_dangerous_terms():
    for command, relative_path in FutuAdapter.READ_ONLY_SCRIPTS.items():
        combined = f"{command} {relative_path}".lower()
        assert all(term not in combined for term in DANGEROUS_CALLS)


def test_gitignore_protects_private_portfolio_artifacts():
    private_paths = (
        "Futu API Skills/portfolio-analyst/account_ids.json",
        "Futu API Skills/portfolio-analyst/snapshot.json",
        "Futu API Skills/portfolio-analyst/trades.json",
        "Futu API Skills/portfolio-analyst/credentials.json",
        "Futu API Skills/portfolio-analyst/personal_rules.yaml",
        "Futu API Skills/portfolio-analyst/report.json",
    )
    for path in private_paths:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", path], cwd=REPOSITORY_ROOT, check=False
        )
        assert result.returncode == 0, path

    safe_fixture = "Futu API Skills/portfolio-analyst/tests/fixtures/snapshot.json"
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", safe_fixture],
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    assert result.returncode == 1
