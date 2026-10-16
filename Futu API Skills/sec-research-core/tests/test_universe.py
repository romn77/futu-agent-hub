import pytest

from sec_research.evidence import ResearchError
from sec_research.universe import Universe


def config():
    return {"schema_version": 1, "universe_id": "my-managers",
            "managers": [{"cik": "1067983", "display_name": "My label"}]}


def test_empty_pool_never_selects_default_managers():
    with pytest.raises(ResearchError, match="EMPTY_UNIVERSE"):
        Universe.from_mapping({"schema_version": 1, "universe_id": "x", "managers": []})


def test_normalized_duplicates_are_rejected():
    value = config()
    value["managers"].append({"cik": "0001067983"})
    with pytest.raises(ResearchError, match="DUPLICATE_CIK"):
        Universe.from_mapping(value)


def test_notes_and_labels_do_not_change_membership_or_leak():
    before = Universe.from_mapping(config())
    value = config()
    value["managers"][0].update(notes="private note", display_name="new label")
    after = Universe.from_mapping(value)
    assert before.version == after.version
    assert before.config_revision != after.config_revision
    assert "private note" not in str(after.public_summary())
    assert after.managers[0].cik == "0001067983"


def test_disabled_entries_do_not_enter_sample():
    value = config()
    value["managers"].append({"cik": "123", "enabled": False})
    assert len(Universe.from_mapping(value).managers) == 1


def test_unknown_private_input_and_false_boolean_are_rejected():
    value = config()
    value["account_id"] = "do-not-send"
    with pytest.raises(ResearchError):
        Universe.from_mapping(value)
    value = config()
    value["managers"][0]["enabled"] = "false"
    with pytest.raises(ResearchError):
        Universe.from_mapping(value)


def test_json_configuration_needs_no_site_packages(tmp_path):
    import json
    from pathlib import Path
    import subprocess
    import sys
    path = tmp_path / "managers.json"
    path.write_text(json.dumps(config()))
    source = "from sec_research.universe import Universe; import sys; print(Universe.load(sys.argv[1]).universe_id)"
    result = subprocess.run([sys.executable, "-S", "-c", source, str(path)],
                            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
