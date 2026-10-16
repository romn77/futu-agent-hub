"""Optional host context for synthetic evaluations; no account access or model calls.

A verified submission proves the CLI input bytes, not remote model retention. Actual
file reads remain separately observable and are never manufactured from this receipt.
"""

import hashlib
import json
from pathlib import Path
import re
import shlex


DEFAULT_DOCUMENTS = (
    "SKILL.md",
    "references/workflow.md",
    "references/intent-routing.md",
    "references/portfolio-analysis-policy.md",
    "references/evidence-policy.md",
    "references/failure-and-degradation.md",
    "references/tool-contract/core/snapshot.md",
    "references/tool-contract/core/risk.md",
    "references/tool-contract/core/research.md",
    "references/tool-contract/core/technical.md",
    "references/research-policy.md",
    "references/output-contract.md",
)
OPTIONAL_DOCUMENTS = (
    "references/futu-public-research.md",
    "references/recommendation-policy.md",
    "references/valuation-policy.md",
    "references/options-policy.md",
    "references/tool-contract/core/options.md",
)
MAX_POLICY_BYTES = 1024 * 1024
PYTHON_ENTRYPOINTS = {"scripts/current_analysis.py", "research/public_reader.py", "inline"}
FIXTURE_METHOD_DOCUMENTS = tuple(
    "research/skills/" + name + ".md"
    for name in ("earnings", "estimates", "valuation", "headlines")
)


def command_tokens(command):
    try:
        args = shlex.split(command)
        if (
            len(args) == 3
            and Path(args[0]).name in {"bash", "zsh", "sh"}
            and args[1] in {"-c", "-lc"}
        ):
            args = shlex.split(args[2])
        return args
    except ValueError:
        return []


def python_entrypoint(command):
    args = command_tokens(command)
    if not args or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(args[0]).name):
        return None
    args = args[1:]
    if args[:1] == ["-B"]:
        args = args[1:]
    if args and args[0] in {"-c", "-"}:
        return "inline"
    return args[0].removeprefix("./") if args else "unknown"


def recognized_command(command):
    """Audit supported forms; this is not a replacement for the CLI execution sandbox."""
    entry = python_entrypoint(command)
    if entry is not None:
        return entry in PYTHON_ENTRYPOINTS
    args = command_tokens(command)
    # This fixed listing prints paths only; do not generalize it to arbitrary chains.
    if args == [
        "pwd;",
        "rg",
        "--files",
        "-g",
        "!data.json",
        "-g",
        "!public_reader.py",
        "-g",
        "!.fixture/**",
    ]:
        return True
    return bool(args) and Path(args[0]).name in {
        "cat",
        "head",
        "tail",
        "sed",
        "awk",
        "grep",
        "rg",
        "wc",
        "ls",
        "pwd",
        "printf",
        "echo",
        "shasum",
        "sha256sum",
    }


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _read(root, name):
    root = Path(root).resolve()
    path = root / name
    if not path.resolve().is_relative_to(root) or any(
        parent.is_symlink()
        for parent in (path, *path.parents)
        if parent != root and parent.is_relative_to(root)
    ):
        raise ValueError("POLICY_PATH_ESCAPE_OR_SYMLINK")
    with path.open("rb") as stream:
        data = stream.read(MAX_POLICY_BYTES + 1)
    if not data.strip() or len(data) > MAX_POLICY_BYTES:
        raise ValueError("POLICY_SIZE_INVALID")
    return data


def build_bundle(root, *, additional=()):
    """Explicit paths only; never recursively preload optional or maintenance policies."""
    if len(set(additional)) != len(additional) or not set(additional) <= set(OPTIONAL_DOCUMENTS):
        raise ValueError("POLICY_SELECTION_INVALID")
    documents = []
    for name in (*DEFAULT_DOCUMENTS, *sorted(additional)):
        data = _read(root, name)
        documents.append(
            {"path": name, "sha256": digest(data), "bytes": len(data), "body": data.decode("utf-8")}
        )
    if sum(doc["bytes"] for doc in documents) > MAX_POLICY_BYTES:
        raise ValueError("POLICY_BUNDLE_TOO_LARGE")
    return {"format_version": 1, "bundle_id": digest(_canonical(documents)), "documents": documents}


def render_submission(request, bundle):
    blocks = [
        "The host has supplied the complete current-version policy documents below. "
        "Their bodies are already available in this context; apply them as the referenced Skill. "
        "Paths identify the original files, which remain available. Load additional policies only "
        "when needed and absent from this supplied context. Document hashes describe provenance, "
        "not portfolio facts. Do not include this loading metadata in the user report.\n",
        f"Policy bundle: {bundle['bundle_id']}\n",
    ]
    for doc in bundle["documents"]:
        blocks.append(
            f'\n<host-policy path={json.dumps(doc["path"])} sha256="{doc["sha256"]}" bytes="{doc["bytes"]}">\n{doc["body"]}\n</host-policy>\n'
        )
    blocks.append("\n<user-request>\n" + request + "\n</user-request>\n")
    return "".join(blocks)


def verify_submission(root, request, bundle, payload):
    """Fail closed if the source, full bundle, request or actual stdin payload differs."""
    try:
        names = [doc["path"] for doc in bundle["documents"]]
        if len(set(names)) != len(names) or not set(DEFAULT_DOCUMENTS) <= set(names):
            raise ValueError("POLICY_COVERAGE_INVALID")
        expected = build_bundle(
            root, additional=tuple(name for name in names if name not in DEFAULT_DOCUMENTS)
        )
        if bundle != expected or payload != render_submission(request, expected):
            raise ValueError("POLICY_SUBMISSION_MISMATCH")
    except (KeyError, TypeError, UnicodeError) as exc:
        raise ValueError("POLICY_SUBMISSION_INVALID") from exc
    return {
        "status": "VERIFIED_CLI_INPUT",
        "bundle_id": bundle["bundle_id"],
        "payload_sha256": digest(payload.encode()),
        "payload_bytes": len(payload.encode()),
        "document_sha256": {doc["path"]: doc["sha256"] for doc in bundle["documents"]},
        "policy_bytes": sum(doc["bytes"] for doc in bundle["documents"]),
    }


def observe_policy_access(root, events, bundle):
    """Count full output bodies even for wildcards or an indirect script invocation."""
    root = Path(root)
    names = [
        "SKILL.md",
        *sorted(str(p.relative_to(root)) for p in (root / "references").rglob("*.md")),
        *(name for name in FIXTURE_METHOD_DOCUMENTS if (root / name).exists()),
    ]
    bodies = {name: _read(root, name).decode("utf-8") for name in names}
    provided = {doc["path"] for doc in bundle["documents"]} if bundle else set()
    counts, seen, conflicts, unverified = {}, {}, set(), []
    output_chars = 0
    for event in events:
        item = event.get("item", {})
        if event.get("type") != "item.completed" or item.get("type") != "command_execution":
            continue
        identity = item.get("id")
        if identity is not None:
            if identity in seen:
                if seen[identity] != item:
                    conflicts.add(str(identity))
                continue
            seen[identity] = item
        command, output = item.get("command", ""), item.get("aggregated_output", "")
        success = item.get("status") == "completed" and item.get("exit_code") == 0
        matches = {
            name: output.count(body.strip())
            for name, body in bodies.items()
            if body.strip() in output
        }
        complete = set(matches) if success else set()
        mentioned = {name for name in bodies if name in command}
        unresolved_pattern = False
        for token in command_tokens(command):
            if any(char in token for char in "*?[]{}") and (
                ".md" in token or token.startswith("references/")
            ):
                if Path(token).is_absolute() or ".." in Path(token).parts:
                    unresolved_pattern = True
                    continue
                expanded = {str(path.relative_to(root)) for path in root.glob(token)}
                matched = expanded & bodies.keys()
                mentioned.update(matched)
                unresolved_pattern |= not matched or bool(expanded - bodies.keys())
            elif token.endswith(".md") and token.removeprefix("./") not in bodies:
                unresolved_pattern = True
        possible_read = bool(
            re.search(r"\b(?:cat|head|tail|sed|awk|grep|rg|open|read_text|read_bytes)\b", command)
        )
        broad_read = possible_read and ("references/" in command or ".md" in command)
        unknown_script = python_entrypoint(command) not in (None, *PYTHON_ENTRYPOINTS)
        if matches or (possible_read and mentioned) or broad_read or unknown_script:
            output_chars += len(output)
        if (
            (possible_read and (mentioned - complete or broad_read and not complete))
            or unknown_script
            or unresolved_pattern
            or (matches and not success)
        ):
            unverified.append(
                {"item_id": identity, "paths": sorted(mentioned - complete), "command": command}
            )
        for name in complete:
            counts[name] = counts.get(name, 0) + matches[name]
    repeated = {
        name: count if name in provided else max(0, count - 1) for name, count in counts.items()
    }
    repeat_count = sum(repeated.values())
    return {
        "complete_policy_outputs": dict(sorted(counts.items())),
        "complete_rereads": repeat_count,
        "repeated_policy_characters": sum(
            len(bodies[name]) * count for name, count in repeated.items()
        ),
        "policy_output_characters": output_chars,
        "unverified_accesses": unverified,
        "conflicting_item_ids": sorted(conflicts),
        "reuse_pass": not repeat_count and not unverified and not conflicts,
    }
