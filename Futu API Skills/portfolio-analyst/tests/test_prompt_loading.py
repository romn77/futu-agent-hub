"""Structural loading checks; financial/report semantics belong to behavior evaluations."""

import importlib.util
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCUMENTS = {
    "SKILL.md",
    "references/intent-routing.md",
    "references/workflow.md",
    "references/portfolio-analysis-policy.md",
    "references/research-policy.md",
    "references/evidence-policy.md",
    "references/output-contract.md",
    "references/failure-and-degradation.md",
    "references/tool-contract/core/snapshot.md",
    "references/tool-contract/core/risk.md",
    "references/tool-contract/core/research.md",
    "references/tool-contract/core/technical.md",
}
EXTRACTED_DOCUMENTS = {
    "references/valuation-policy.md",
    "references/implementation/owned-state.md",
    "references/implementation/snapshot.md",
    "references/implementation/technical.md",
}


def _prose(markdown):
    """Exclude illustrative fenced code from link/loading interpretation, not size counts."""
    lines, fence = [], None
    for line in markdown.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*", line):
                fence = None
        elif marker:
            fence = marker.group(1)
        else:
            lines.append(line)
    return "\n".join(lines)


def _links(markdown, document):
    pattern = r"\[[^\]\n]+\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+\"[^\"]*\")?\s*\)"
    for match in re.finditer(pattern, _prose(markdown)):
        target = urlsplit(match.group(1) or match.group(2))
        if target.scheme or target.netloc or not target.path:
            continue
        yield (document.parent / unquote(target.path)).resolve()


def _section(markdown, heading):
    match = re.search(rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", _prose(markdown))
    assert match, f"missing loading section: {heading}"
    return match.group(1)


def _loading_blocks(markdown):
    """Keep section context for the existing conditional/maintenance loading prose."""
    heading = ""
    for block in re.split(r"\n\s*\n", _prose(markdown)):
        if re.match(r"^#{1,6} ", block):
            heading = block.splitlines()[0]
        yield heading, block


def _unconditional_reads(markdown, document):
    for heading, block in _loading_blocks(markdown):
        if not re.search(r"\b(?:read(?!-only)|load|required)\b", block, re.IGNORECASE):
            continue
        # Only explicit reading directives add cost; ordinary navigation does not.
        # These are structural markers, not an attempt to prove natural-language semantics.
        if re.search(r"\b(?:conditional|maintenance)\b", heading, re.IGNORECASE):
            continue
        if re.search(r"\b(?:if|when|only)\b", block, re.IGNORECASE):
            continue
        if re.search(
            r"\bbefore\s+(?:you\s+)?(?:calculat|comput|valu|deriv|build.*valu)",
            block,
            re.IGNORECASE,
        ):
            continue
        yield from _links(block, document)


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    spec = importlib.util.spec_from_file_location(
        "prompt_loading_eval_producer", ROOT / "evals/run_codex_evals.py"
    )
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    workspace = tmp_path_factory.mktemp("prompt-loading") / "isolated"
    producer.prepare_workspace(workspace, "comprehensive-many-holdings")
    return workspace


def test_isolated_workspace_preserves_all_default_and_extracted_documents(workspace):
    required = DEFAULT_DOCUMENTS | EXTRACTED_DOCUMENTS
    missing = sorted(relative for relative in required if not (workspace / relative).is_file())
    assert not missing, f"required prompt documents missing from isolated copy: {missing}"
    for relative in sorted(required):
        copied = (workspace / relative).read_bytes()
        assert copied.strip(), f"empty prompt document: {relative}"
        assert copied == (ROOT / relative).read_bytes(), f"incomplete copied document: {relative}"
    assert not (workspace / "evals").exists()


def test_default_route_declares_all_twelve_documents(workspace):
    skill = workspace / "SKILL.md"
    default = _section(skill.read_text(encoding="utf-8"), "Default comprehensive route")
    declared = {"SKILL.md"} | {
        path.relative_to(workspace).as_posix() for path in _links(default, skill)
    }
    assert declared == DEFAULT_DOCUMENTS


def test_extracted_references_are_reachable_from_the_skill(workspace):
    reached, pending = set(), [workspace / "SKILL.md"]
    while pending:
        document = pending.pop()
        assert document.is_relative_to(workspace), f"link leaves isolated workspace: {document}"
        if document in reached or not document.is_file():
            continue
        reached.add(document)
        pending.extend(
            path
            for path in _links(document.read_text(encoding="utf-8"), document)
            if path.suffix == ".md"
        )
    expected = {workspace / relative for relative in DEFAULT_DOCUMENTS | EXTRACTED_DOCUMENTS}
    assert expected <= reached, f"undiscoverable prompt documents: {sorted(expected - reached)}"


def test_local_markdown_links_resolve_inside_the_isolated_workspace(workspace):
    broken = []
    documents = [workspace / "SKILL.md", *sorted((workspace / "references").rglob("*.md"))]
    for document in documents:
        for target in _links(document.read_text(encoding="utf-8"), document):
            if not target.is_relative_to(workspace) or not target.is_file():
                broken.append(f"{document.relative_to(workspace)} -> {target}")
    assert not broken, "broken or escaping local links:\n" + "\n".join(broken)


def test_default_reading_budget_includes_complete_documents_and_required_additions(workspace):
    default = {workspace / relative for relative in DEFAULT_DOCUMENTS}
    pending = list(default)
    while pending:
        document = pending.pop()
        for target in _unconditional_reads(document.read_text(encoding="utf-8"), document):
            assert target.is_relative_to(workspace) and target.is_file(), target
            if target not in default:
                default.add(target)
                pending.append(target)
    contents = [path.read_text(encoding="utf-8") for path in default]
    words = sum(len(content.split()) for content in contents)
    characters = sum(len(content) for content in contents)
    paths = sorted(path.relative_to(workspace).as_posix() for path in default)
    assert words <= 6900 and characters <= 54512, (
        f"default prompt has {words} words / {characters} characters across {len(paths)} files; "
        f"limits are 6900 / 54512; counted: {paths}"
    )


def test_valuation_and_maintenance_are_not_unconditional_reads(workspace):
    extracted = {workspace / relative for relative in EXTRACTED_DOCUMENTS}
    for relative in DEFAULT_DOCUMENTS:
        document = workspace / relative
        required = set(_unconditional_reads(document.read_text(encoding="utf-8"), document))
        assert required.isdisjoint(extracted), f"unconditional extracted reference in {relative}"
    skill = workspace / "SKILL.md"
    conditional = _section(skill.read_text(encoding="utf-8"), "Conditional routes")
    assert workspace / "references/valuation-policy.md" in set(_links(conditional, skill))


def test_illustrative_fenced_links_do_not_become_loading_requirements(tmp_path):
    markdown = """Read [actual](actual.md).

```markdown
Read [illustration](missing-example.md).
```

~~~~markdown
[another illustration](missing-too.md)
~~~
~~~~
"""
    document = tmp_path / "SKILL.md"
    assert set(_links(markdown, document)) == {tmp_path / "actual.md"}
    assert set(_unconditional_reads(markdown, document)) == {tmp_path / "actual.md"}


@pytest.mark.parametrize(
    "instruction,required",
    [
        ("Read [policy](policy.md) before every comprehensive analysis.", True),
        ("Required: [policy](policy.md).", True),
        ("Related details: [policy](policy.md).", False),
        ("Before calculating a target price, read [policy](policy.md).", False),
        ("Read [policy](policy.md) when calculating a valuation.", False),
        ("## Conditional routes\n\nRead [policy](policy.md).", False),
        ("## Maintenance\n\nRead [policy](policy.md) to debug cleanup.", False),
    ],
)
def test_default_cost_distinguishes_required_reads_from_conditional_navigation(
    tmp_path, instruction, required
):
    targets = set(_unconditional_reads(instruction, tmp_path / "SKILL.md"))
    assert targets == ({tmp_path / "policy.md"} if required else set())
