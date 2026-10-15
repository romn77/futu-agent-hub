"""Read costs must come from successful tool receipts, not agent prose."""

import importlib.util
from pathlib import Path


def producer():
    path = Path(__file__).resolve().parents[1] / "evals/run_codex_evals.py"
    spec = importlib.util.spec_from_file_location("read_cost_producer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(identity, command, output, status="completed", exit_code=0):
    return {
        "id": identity,
        "type": "command_execution",
        "command": command,
        "aggregated_output": output,
        "status": status,
        "exit_code": exit_code,
    }


def test_repeated_complete_reads_and_batched_output_are_measured_separately():
    refs = {"SKILL.md": "# Skill\nFull instructions.", "references/workflow.md": "完整流程。"}
    items = [
        read("1", "cat SKILL.md references/workflow.md", "\n".join(refs.values())),
        read("2", "cat SKILL.md", refs["SKILL.md"]),
    ]
    result = producer().observe_reference_reads(items, refs)
    assert result["complete_reads"] == {"SKILL.md": 2, "references/workflow.md": 1}
    assert result["duplicate_full_reads"] == 1
    assert result["repeated_reference_characters"] == len(refs["SKILL.md"])
    assert result["observed_output_characters"] == sum(len(x["aggregated_output"]) for x in items)
    assert result["observed_output_bytes"] > result["observed_output_characters"]


def test_failed_and_truncated_reads_are_not_counted_as_complete():
    items = [
        read("1", "cat SKILL.md", "Partial"),
        read("2", "cat SKILL.md", "Full body", exit_code=1),
        {"type": "agent_message", "text": "I read SKILL.md twice: Full body"},
    ]
    result = producer().observe_reference_reads(items, {"SKILL.md": "Full body"})
    assert result["complete_reads"] == {}
    assert result["unverified_attempts"] == 2
    assert result["duplicate_full_reads"] == 0


def test_duplicate_host_event_id_is_not_a_second_read():
    item = read("same", "cat SKILL.md", "Full body")
    result = producer().observe_reference_reads([item, dict(item)], {"SKILL.md": "Full body"})
    assert result["complete_reads"] == {"SKILL.md": 1}
    assert result["duplicate_full_reads"] == 0


def test_conflicting_event_identity_is_disclosed_not_silently_deduplicated():
    result = producer().observe_reference_reads(
        [read("same", "cat SKILL.md", "Full body"), read("same", "cat SKILL.md", "different")],
        {"SKILL.md": "Full body"},
    )
    assert result["status"] == "PARTIAL"
    assert result["conflicting_item_ids"] == ["same"]


def test_usage_absence_and_partial_fields_remain_unknown():
    module = producer()
    assert module.observe_token_usage([]) == {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
    }
    result = module.observe_token_usage(
        [
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}},
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 10},
            },
            {"type": "agent_message", "text": "input_tokens: 0"},
        ]
    )
    assert result == {"input_tokens": 150, "cached_input_tokens": None, "output_tokens": 30}


def test_invalid_usage_cannot_be_turned_into_a_measured_zero():
    result = producer().observe_token_usage(
        [
            {
                "type": "turn.completed",
                "usage": {"input_tokens": True, "cached_input_tokens": -1, "output_tokens": 0},
            },
        ]
    )
    assert result == {"input_tokens": None, "cached_input_tokens": None, "output_tokens": 0}
