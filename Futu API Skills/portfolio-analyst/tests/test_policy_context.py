"""Host delivery must bind real source text and count the first subsequent file read."""

import copy
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def context():
    spec = importlib.util.spec_from_file_location(
        "policy_context", ROOT / "evals/policy_context.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(tmp_path):
    module = context()
    for name in (*module.DEFAULT_DOCUMENTS, *module.OPTIONAL_DOCUMENTS):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n完整规则 for {name}.\n")
    return tmp_path


def test_full_source_submission_and_request_are_deterministic(source):
    module = context()
    bundle = module.build_bundle(source, additional=("references/valuation-policy.md",))
    assert bundle == module.build_bundle(source, additional=("references/valuation-policy.md",))
    prompt = "Analyze the synthetic portfolio."
    payload = module.render_submission(prompt, bundle)
    proof = module.verify_submission(source, prompt, bundle, payload)
    assert proof["status"] == "VERIFIED_CLI_INPUT"
    assert set(proof["document_sha256"]) == set(module.DEFAULT_DOCUMENTS) | {
        "references/valuation-policy.md"
    }
    for doc in bundle["documents"]:
        assert doc["body"] == (source / doc["path"]).read_text()
        assert doc["body"] in payload


@pytest.mark.parametrize(
    "change", ["body", "digest", "missing", "duplicate", "source", "payload", "request"]
)
def test_incomplete_or_changed_submission_never_counts_as_delivery(source, change):
    module = context()
    bundle = module.build_bundle(source)
    prompt = "Analyze this fixture."
    payload = module.render_submission(prompt, bundle)
    altered = copy.deepcopy(bundle)
    if change == "body":
        altered["documents"][0]["body"] += "Forged permission."
    elif change == "digest":
        altered["documents"][0]["sha256"] = "0" * 64
    elif change == "missing":
        altered["documents"].pop()
    elif change == "duplicate":
        altered["documents"].append(altered["documents"][0])
    elif change == "source":
        (source / "SKILL.md").write_text("Changed version")
    elif change == "payload":
        payload = payload[:-10]
    else:
        prompt += " Additional request."
    with pytest.raises(ValueError):
        module.verify_submission(source, prompt, altered, payload)


@pytest.mark.parametrize("name", ["../private.md", "/private.md", ".env", "references/unknown.md"])
def test_arbitrary_file_inclusion_is_rejected(source, name):
    with pytest.raises(ValueError):
        context().build_bundle(source, additional=(name,))


def test_symlinked_policy_is_not_exported(source, tmp_path):
    path = source / "SKILL.md"
    path.unlink()
    outside = tmp_path.parent / (tmp_path.name + "-secret.md")
    outside.write_text("private")
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        context().build_bundle(source)


def event(identity, command, output):
    return {
        "type": "item.completed",
        "item": {
            "id": identity,
            "type": "command_execution",
            "command": command,
            "aggregated_output": output,
            "status": "completed",
            "exit_code": 0,
        },
    }


@pytest.mark.parametrize("command", ["cat SKILL.md", "cat *.md", "python read_rules.py"])
def test_first_complete_read_after_delivery_counts_even_without_literal_path(source, command):
    module = context()
    bundle = module.build_bundle(source)
    item = event("one", command, (source / "SKILL.md").read_text())
    observation = module.observe_policy_access(source, [item, copy.deepcopy(item)], bundle)
    assert observation["complete_policy_outputs"]["SKILL.md"] == 1
    assert observation["complete_rereads"] == 1
    assert observation["reuse_pass"] is False


def test_first_unprovided_conditional_read_is_not_a_reread(source):
    module = context()
    name = "references/valuation-policy.md"
    item = event("one", "cat " + name, (source / name).read_text())
    observation = module.observe_policy_access(source, [item], module.build_bundle(source))
    assert observation["complete_rereads"] == 0
    assert observation["reuse_pass"] is True
    second = event("two", "cat " + name, (source / name).read_text())
    assert (
        module.observe_policy_access(source, [item, second], module.build_bundle(source))[
            "complete_rereads"
        ]
        == 1
    )


def test_partial_read_and_conflicting_event_are_not_reported_as_clean_reuse(source):
    module = context()
    first = event("one", "head -2 references/research-policy.md", "fragment")
    changed = event("one", "cat SKILL.md", (source / "SKILL.md").read_text())
    observation = module.observe_policy_access(
        source, [first, changed], module.build_bundle(source)
    )
    assert observation["unverified_accesses"]
    assert observation["conflicting_item_ids"] == ["one"]
    assert observation["reuse_pass"] is False


def test_agent_claims_do_not_become_file_reads_or_host_delivery(source):
    module = context()
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "I loaded every policy and verified their hashes.",
            },
        }
    ]
    assert module.observe_policy_access(source, events, None)["complete_policy_outputs"] == {}
    with pytest.raises(ValueError):
        module.verify_submission(source, "request", None, "I loaded every policy")


def test_default_bundle_matches_existing_required_document_contract():
    spec = importlib.util.spec_from_file_location(
        "loading_tests", ROOT / "tests/test_prompt_loading.py"
    )
    tests = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tests)
    assert set(context().DEFAULT_DOCUMENTS) == tests.DEFAULT_DOCUMENTS


@pytest.mark.parametrize("command", ["head -2 *.md", "cat *.md", "python read_rules.py"])
def test_indirect_or_wildcard_partial_reads_are_not_clean_reuse(source, command):
    item = event("one", command, "fragment")
    assert (
        context().observe_policy_access(source, [item], context().build_bundle(source))[
            "reuse_pass"
        ]
        is False
    )


def test_two_complete_bodies_in_one_command_are_two_deliveries(source):
    module = context()
    name = "references/valuation-policy.md"
    body = (source / name).read_text()
    item = event("one", f"cat {name} {name}", body + body)
    observation = module.observe_policy_access(source, [item], module.build_bundle(source))
    assert observation["complete_policy_outputs"][name] == 2
    assert observation["complete_rereads"] == 1


def test_complete_conditional_output_does_not_hide_partial_wildcard_reads(source):
    module = context()
    conditional = "references/valuation-policy.md"
    (source / conditional).write_text("One-line conditional rule.\n")
    body = (source / conditional).read_text() + "\npartial other policy\n"
    observation = module.observe_policy_access(
        source, [event("one", "head -n2 references/*-policy.md", body)], module.build_bundle(source)
    )
    assert observation["complete_rereads"] == 0
    assert observation["unverified_accesses"]
    assert observation["reuse_pass"] is False


def test_whitespace_is_not_a_complete_policy_body(source):
    (source / "SKILL.md").write_text(" \n\t")
    with pytest.raises(ValueError):
        context().build_bundle(source)


def test_unprovided_fixture_method_first_read_and_repeat_are_distinguished(source):
    module = context()
    name = "research/skills/earnings.md"
    path = source / name
    path.parent.mkdir(parents=True)
    path.write_text("# Earnings method\nRead the quarter and source period.\n")
    first = event("one", "cat " + name, path.read_text())
    bundle = module.build_bundle(source)
    observation = module.observe_policy_access(source, [first], bundle)
    assert observation["complete_policy_outputs"][name] == 1
    assert observation["complete_rereads"] == 0
    assert observation["reuse_pass"] is True
    second = event("two", "cat " + name, path.read_text())
    assert module.observe_policy_access(source, [first, second], bundle)["complete_rereads"] == 1
    partial = event("three", "head -1 " + name, "# Earnings method\n")
    assert module.observe_policy_access(source, [partial], bundle)["reuse_pass"] is False


def test_unknown_markdown_is_not_verified_as_a_fixture_method(source):
    name = "research/skills/private.md"
    path = source / name
    path.parent.mkdir(parents=True)
    path.write_text("Unknown instructions.\n")
    observation = context().observe_policy_access(
        source, [event("one", "cat " + name, path.read_text())], context().build_bundle(source)
    )
    assert observation["complete_policy_outputs"] == {}
    assert observation["unverified_accesses"]
    assert observation["reuse_pass"] is False


def test_complete_known_body_does_not_hide_an_unknown_markdown_read(source):
    module = context()
    known = "references/valuation-policy.md"
    observation = module.observe_policy_access(
        source,
        [event("one", f"cat {known} research/skills/private.md", (source / known).read_text())],
        module.build_bundle(source),
    )
    assert observation["complete_rereads"] == 0
    assert observation["unverified_accesses"]
