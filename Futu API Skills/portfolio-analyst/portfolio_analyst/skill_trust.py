"""Immutable content manifest and compatibility validation for futuapi."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AuditedFile:
    relative_path: Path
    sha256: str


@dataclass(frozen=True)
class AuditedCommand:
    name: str
    script_path: Path


# Audited against the repository's current official Skill. Every command imports only
# scripts/common.py locally; all other imports are Python stdlib or the external futu SDK.
_AUDITED_FILES = (
    AuditedFile(
        Path("SKILL.md"), "5619a3fa58c8e36855f68b71238e39a65f140fe6822092bf5188716ad020c1cd"
    ),
    AuditedFile(
        Path("scripts/common.py"),
        "5a8e160e09d79e98e383b17ef28479dd44ab0ee4d8ba1781febefbf09bdae6fa",
    ),
    AuditedFile(
        Path("scripts/trade/get_accounts.py"),
        "dd76b1816b9c385ae8e8ef79a40c6490a78b883ca793b0415cd00961acf162c6",
    ),
    AuditedFile(
        Path("scripts/trade/get_portfolio.py"),
        "0286bc8d0a0c0aef727e0b4c4f54c2791f6cc48e707ff0f5f1a55be04e2166be",
    ),
    AuditedFile(
        Path("scripts/quote/get_kline.py"),
        "b60f94cd952be5617a098ffd56e19a4c7ee48780ef0346c47d65b77dd63fc345",
    ),
    AuditedFile(
        Path("scripts/trade/get_history_orders.py"),
        "c49d0db84f46b13f680943ebc2c68434b4d53ad7190edb9587cdc2f787059946",
    ),
    AuditedFile(
        Path("scripts/trade/get_history_order_fill_list.py"),
        "94a7885729f7a68d912ca0c24d8295da91e2c722d2e750d6b09e8d2aba4b418a",
    ),
)
_AUDITED_COMMANDS = (
    AuditedCommand("get_accounts", Path("scripts/trade/get_accounts.py")),
    AuditedCommand("get_portfolio", Path("scripts/trade/get_portfolio.py")),
    AuditedCommand("get_kline", Path("scripts/quote/get_kline.py")),
    AuditedCommand("get_history_orders", Path("scripts/trade/get_history_orders.py")),
    AuditedCommand(
        "get_history_order_fill_list",
        Path("scripts/trade/get_history_order_fill_list.py"),
    ),
)
_AUDITED_COMMAND_NAMES = frozenset(command.name for command in _AUDITED_COMMANDS)
OPTION_QUOTE_SHA256 = "0ea054b6b8f6f37938885041b7257653627941408462fc9c3c53d0395d4c8fd8"
_OPTION_QUOTE_FILE = AuditedFile(
    Path("scripts/quote/get_option_quote.py"),
    OPTION_QUOTE_SHA256,
)


@dataclass(frozen=True)
class AuditedSkill:
    root: Path
    files: tuple[tuple[AuditedFile, bytes], ...]

    def content(self, relative_path: Path) -> bytes:
        for record, content in self.files:
            if record.relative_path == relative_path:
                return content
        raise KeyError(relative_path)


@dataclass(frozen=True)
class SkillValidation:
    root: Path | None
    detail: str

    @property
    def valid(self) -> bool:
        return self.root is not None


@dataclass(frozen=True)
class OptionQuoteCapability:
    root: Path
    file: AuditedFile
    content: bytes


def audited_files() -> tuple[AuditedFile, ...]:
    return _AUDITED_FILES


def audited_commands() -> tuple[AuditedCommand, ...]:
    return _AUDITED_COMMANDS


def audited_command_names() -> frozenset[str]:
    return _AUDITED_COMMAND_NAMES


def audited_command(name: str) -> AuditedCommand | None:
    for command in _AUDITED_COMMANDS:
        if command.name == name:
            return command
    return None


def canonicalize_bytes(content: bytes) -> bytes:
    """Normalize line endings so pinned identity is stable across supported platforms."""

    return content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _frontmatter_name(content: bytes) -> str | None:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return None
    for line in lines[1:closing]:
        match = re.fullmatch(r"\s*name\s*:\s*([^#]+?)\s*", line)
        if match:
            return match.group(1).strip().strip("\"'")
    return None


def load_audited_skill(candidate: Path) -> tuple[AuditedSkill | None, str]:
    """Open each pinned file once and retain only digest-verified canonical bytes."""

    try:
        root = candidate.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "candidate directory does not exist"
    if not root.is_dir():
        return None, "candidate is not a directory"

    loaded: list[tuple[AuditedFile, bytes]] = []
    for record in _AUDITED_FILES:
        source = root / record.relative_path
        try:
            resolved = source.resolve(strict=True)
        except (OSError, RuntimeError):
            return None, f"required audited file is missing: {record.relative_path}"
        if not resolved.is_relative_to(root):
            return None, f"required audited file escapes Skill directory: {record.relative_path}"
        if not resolved.is_file():
            return None, f"required audited file is not a file: {record.relative_path}"
        try:
            with resolved.open("rb") as handle:
                content = canonicalize_bytes(handle.read())
        except OSError as exc:
            return None, f"required audited file could not be read: {type(exc).__name__}"
        digest = hashlib.sha256(content).hexdigest()
        if digest != record.sha256:
            return None, f"audited content digest mismatch: {record.relative_path}"
        loaded.append((record, content))

    skill_content = next(
        content for record, content in loaded if record.relative_path == Path("SKILL.md")
    )
    if _frontmatter_name(skill_content) != "futuapi":
        return None, "SKILL.md frontmatter must declare name: futuapi"
    return AuditedSkill(root, tuple(loaded)), "audited futuapi content identity matched"


def validate_futuapi_skill(candidate: Path) -> SkillValidation:
    """Validate containment and pinned identity for every trust-relevant file."""

    audited, detail = load_audited_skill(candidate)
    return SkillValidation(audited.root if audited is not None else None, detail)


def load_option_quote_capability(
    candidate: Path,
) -> tuple[OptionQuoteCapability | None, str]:
    """Load the separately pinned option quote script without widening core trust."""

    audited, detail = load_audited_skill(candidate)
    if audited is None:
        return None, detail
    source = audited.root / _OPTION_QUOTE_FILE.relative_path
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, f"optional audited file is missing: {_OPTION_QUOTE_FILE.relative_path}"
    if not resolved.is_relative_to(audited.root) or not resolved.is_file():
        return None, f"optional audited file is unsafe: {_OPTION_QUOTE_FILE.relative_path}"
    try:
        with resolved.open("rb") as handle:
            content = canonicalize_bytes(handle.read())
    except OSError as exc:
        return None, f"optional audited file could not be read: {type(exc).__name__}"
    if hashlib.sha256(content).hexdigest() != _OPTION_QUOTE_FILE.sha256:
        return None, f"optional audited content digest mismatch: {_OPTION_QUOTE_FILE.relative_path}"
    return (
        OptionQuoteCapability(audited.root, _OPTION_QUOTE_FILE, content),
        "audited option quote capability identity matched",
    )


def validate_option_quote_capability(candidate: Path) -> SkillValidation:
    capability, detail = load_option_quote_capability(candidate)
    return SkillValidation(capability.root if capability is not None else None, detail)
