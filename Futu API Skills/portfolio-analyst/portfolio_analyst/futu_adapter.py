"""Narrow subprocess boundary around audited read-only Skill scripts."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Sequence

from .child_environment import controlled_child_environment, validate_account_id
from .skill_trust import audited_command, audited_command_names, load_audited_skill


class UnsafeFutuCommand(ValueError):
    """Raised when a command falls outside the fixed read-only boundary."""


class FutuReadError(RuntimeError):
    """Sanitized failure from an audited read-only command."""


@dataclass(frozen=True)
class FutuRequest:
    command: str
    arguments: tuple[str, ...]
    sensitive_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class _OptionSpec:
    name: str
    takes_value: bool = False
    choices: frozenset[str] | None = None
    account_id: bool = False


@dataclass(frozen=True)
class _CommandOptions:
    command: str
    options: tuple[_OptionSpec, ...]


_MARKETS = frozenset({"US", "HK", "HKCC", "CN", "SG", "MY", "JP"})
_TRADING_ENVIRONMENTS = frozenset({"REAL", "SIMULATE"})
_CURRENCIES = frozenset({"HKD", "USD", "CNH", "JPY", "AUD", "CAD", "MYR", "SGD"})
_SECURITY_FIRMS = frozenset(
    {"FUTUSECURITIES", "FUTUINC", "FUTUSG", "FUTUAU", "FUTUCA", "FUTUJP", "FUTUMY"}
)
_COMMAND_OPTIONS = (
    _CommandOptions(
        "get_accounts",
        (_OptionSpec("--json"), _OptionSpec("--show-disabled")),
    ),
    _CommandOptions(
        "get_portfolio",
        (
            _OptionSpec("--acc-id", takes_value=True, account_id=True),
            _OptionSpec("--market", takes_value=True, choices=_MARKETS),
            _OptionSpec("--trd-env", takes_value=True, choices=_TRADING_ENVIRONMENTS),
            _OptionSpec("--currency", takes_value=True, choices=_CURRENCIES),
            _OptionSpec("--security-firm", takes_value=True, choices=_SECURITY_FIRMS),
            _OptionSpec("--ctx-type", takes_value=True, choices=frozenset({"SEC", "FUTURE"})),
            _OptionSpec("--show-option-strategy-view"),
            _OptionSpec("--json"),
        ),
    ),
    _CommandOptions(
        "get_all_portfolios",
        (
            _OptionSpec("--acc-id", takes_value=True, account_id=True),
            _OptionSpec("--trd-env", takes_value=True, choices=_TRADING_ENVIRONMENTS),
            _OptionSpec("--show-option-strategy-view"),
            _OptionSpec("--json"),
        ),
    ),
)
_COMMAND_OPTION_SCHEMA = MappingProxyType(
    {record.command: record.options for record in _COMMAND_OPTIONS}
)


class FutuAdapter:
    """Execute only fixed read-only scripts from an audited futuapi Skill."""

    READ_ONLY_COMMANDS = audited_command_names()
    DENY_TERMS = frozenset(
        {
            "place_order",
            "modify_order",
            "cancel_order",
            "unlock_trade",
            "place_combo_order",
            "buy",
            "sell",
        }
    )
    CREDENTIAL_FLAG_TERMS = frozenset({"password", "passwd", "pwd", "unlock"})
    SENSITIVE_JSON_KEYS = frozenset(
        {
            "accid",
            "futuaccid",
            "accountid",
            "orderid",
            "dealid",
            "cardnum",
            "loginaccount",
            "accname",
            "accountname",
            "competitionaccname",
            "password",
            "passwd",
            "pwd",
            "token",
            "secret",
            "apikey",
            "credential",
            "credentials",
        }
    )
    REDACTION = "[REDACTED]"
    _TEXT_SENSITIVE_VALUE = re.compile(
        r"(?ix)"
        r"(?P<label>"
        r"(?:"
        r"(?:[\"']?(?:acc(?:ount)?[ _-]?id|order[ _-]?id|deal[ _-]?id|card[ _-]?num|"
        r"login[ _-]?account|acc(?:ount)?[ _-]?name|competition[ _-]?acc[ _-]?name|"
        r"api[ _-]?key|secret|password(?:[ _-]?(?:value|file|hash))?|passwd|pwd|"
        r"(?:(?:access|refresh)[ _-]?)?token(?:[ _-]?(?:value|file))?|credentials?)[\"']?|"
        r"账户\s*ID|订单\s*ID|成交\s*ID|"
        r"比赛账户名称|账户名称|账户名|账号|密码)\s*[:=]\s*"
        r"|账户\s+(?=\d)"
        r")"
        r")"
        r"(?P<value>[^\r\n]+)"
    )

    def __init__(self, skill_dir: Path, *, python_executable: str | None = None) -> None:
        audited, detail = load_audited_skill(skill_dir)
        if audited is None:
            raise ValueError(f"audited futuapi Skill required: {detail}")
        self.skill_dir = audited.root
        self.python_executable = python_executable or sys.executable

    def validate_request(self, name: str, arguments: Sequence[str]) -> FutuRequest:
        """Validate a request without exposing an executable source path."""

        normalized = name.lower()
        if any(term in normalized for term in self.DENY_TERMS):
            raise UnsafeFutuCommand(f"write-capable Futu command is forbidden: {name}")
        if audited_command(name) is None:
            raise UnsafeFutuCommand(f"Futu command is not in the read-only allowlist: {name}")
        option_names = (
            str(argument).lower() for argument in arguments if str(argument).startswith("-")
        )
        if any(term in option for option in option_names for term in self.CREDENTIAL_FLAG_TERMS):
            raise UnsafeFutuCommand("credential and unlock arguments are forbidden")
        normalized: list[str] = []
        sensitive_values: list[str] = []
        seen: set[str] = set()
        raw_arguments = tuple(map(str, arguments))
        options = _COMMAND_OPTION_SCHEMA.get(name, ())
        index = 0
        while index < len(raw_arguments):
            token = raw_arguments[index]
            if not token.startswith("--"):
                raise UnsafeFutuCommand("positional Futu arguments are forbidden")
            option, separator, attached_value = token.partition("=")
            spec = next((candidate for candidate in options if candidate.name == option), None)
            if spec is None:
                raise UnsafeFutuCommand("Futu option is not in the command policy")
            if option in seen:
                raise UnsafeFutuCommand("duplicate Futu options are forbidden")
            seen.add(option)
            normalized.append(option)
            if not spec.takes_value:
                if separator:
                    raise UnsafeFutuCommand("boolean Futu options do not accept values")
                index += 1
                continue
            if separator:
                value = attached_value
            else:
                index += 1
                if index >= len(raw_arguments) or raw_arguments[index].startswith("--"):
                    raise UnsafeFutuCommand("Futu option requires a value")
                value = raw_arguments[index]
            if not value:
                raise UnsafeFutuCommand("Futu option requires a value")
            if spec.choices is not None and value not in spec.choices:
                raise UnsafeFutuCommand("Futu option value is outside the command policy")
            if spec.account_id:
                try:
                    validate_account_id(value)
                except ValueError:
                    raise UnsafeFutuCommand("account ID has an invalid format") from None
                sensitive_values.append(value)
            normalized.append(value)
            index += 1
        return FutuRequest(name, tuple(normalized), tuple(sensitive_values))

    def run(
        self, name: str, arguments: Sequence[str], *, timeout: float = 30
    ) -> subprocess.CompletedProcess[str]:
        """Run an approved read-only command without a shell."""

        request = self.validate_request(name, arguments)
        child_environment = controlled_child_environment(purpose="adapter")
        sensitive_values = self._sensitive_values(request, child_environment)
        completed = self._run_unsanitized(name, arguments, timeout=timeout)
        return subprocess.CompletedProcess(
            args=self._sanitize_args(completed.args, sensitive_values=sensitive_values),
            returncode=completed.returncode,
            stdout=self.sanitize_output(completed.stdout, sensitive_values=sensitive_values),
            stderr=self.sanitize_output(completed.stderr, sensitive_values=sensitive_values),
        )

    def read_private_json(
        self, name: str, arguments: Sequence[str], *, timeout: float = 30
    ) -> dict[str, object]:
        """Parse structured facts internally while keeping public command output redacted.

        Callers must not log or serialize the returned source object directly. Account IDs are
        required only to join audited account and portfolio reads and must be masked in models.
        """

        request = self.validate_request(name, arguments)
        child_environment = controlled_child_environment(purpose="adapter")
        sensitive_values = self._sensitive_values(request, child_environment)
        completed = self._run_unsanitized(name, arguments, timeout=timeout)
        if completed.returncode != 0:
            details = tuple(
                detail
                for detail in (
                    self.sanitize_output(completed.stdout, sensitive_values=sensitive_values),
                    self.sanitize_output(completed.stderr, sensitive_values=sensitive_values),
                )
                if detail
            )
            raise FutuReadError(
                f"audited Futu read failed with exit code {completed.returncode}: "
                f"{' | '.join(details) if details else 'no detail'}"
            )
        candidates = [completed.stdout, *reversed(completed.stdout.splitlines())]
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                return payload
        raise FutuReadError("audited Futu read returned no valid JSON object")

    @staticmethod
    def _sensitive_values(
        request: FutuRequest, child_environment: dict[str, str]
    ) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *request.sensitive_values,
                    *(value for key, value in child_environment.items() if key == "FUTU_ACC_ID"),
                )
            )
        )

    def _run_unsanitized(
        self, name: str, arguments: Sequence[str], *, timeout: float = 30
    ) -> subprocess.CompletedProcess[str]:
        """Execute inside the pinned snapshot; raw output never crosses a public API."""

        request = self.validate_request(name, arguments)
        child_environment = controlled_child_environment(purpose="adapter")
        sensitive_values = self._sensitive_values(request, child_environment)
        audited, detail = load_audited_skill(self.skill_dir)
        if audited is None:
            raise ValueError(f"audited futuapi Skill required: {detail}")
        command_record = audited_command(request.command)
        assert command_record is not None
        with tempfile.TemporaryDirectory(prefix="portfolio-futu-") as temporary:
            snapshot_root = Path(temporary) / "futuapi"
            for record, content in audited.files:
                destination = snapshot_root / record.relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            command = [
                self.python_executable,
                "-s",
                str(snapshot_root / command_record.script_path),
                *request.arguments,
            ]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                    env=child_environment,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise subprocess.TimeoutExpired(
                    cmd=self._sanitize_args(exc.cmd, sensitive_values=sensitive_values),
                    timeout=exc.timeout,
                    output=self.sanitize_output(exc.output, sensitive_values=sensitive_values),
                    stderr=self.sanitize_output(exc.stderr, sensitive_values=sensitive_values),
                ) from None
        return completed

    @classmethod
    def sanitize_output(
        cls, output: str | bytes | None, *, sensitive_values: Sequence[str] = ()
    ) -> str:
        """Mask sensitive identifiers and credentials in JSON or human-readable output."""

        if not output:
            return ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        try:
            payload = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return cls._mask_text(output, sensitive_values=sensitive_values)
        return json.dumps(
            cls._mask_json(payload, sensitive_values=sensitive_values), ensure_ascii=False
        )

    @classmethod
    def _mask_json(cls, value, *, sensitive_values: Sequence[str] = ()):
        if isinstance(value, dict):
            return {
                key: cls.REDACTION
                if cls._is_sensitive_json_key(key)
                else cls._mask_json(item, sensitive_values=sensitive_values)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._mask_json(item, sensitive_values=sensitive_values) for item in value]
        if isinstance(value, str):
            return cls._mask_text(value, sensitive_values=sensitive_values)
        if str(value) in sensitive_values:
            return cls.REDACTION
        return value

    @classmethod
    def _is_sensitive_json_key(cls, key: object) -> bool:
        normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
        return normalized in cls.SENSITIVE_JSON_KEYS or any(
            marker in normalized
            for marker in ("password", "passwd", "token", "secret", "credential", "apikey")
        )

    @classmethod
    def _mask_text(cls, output: str, *, sensitive_values: Sequence[str] = ()) -> str:
        masked = cls._TEXT_SENSITIVE_VALUE.sub(
            lambda match: f"{match.group('label')}{cls.REDACTION}", output
        )
        for sensitive in sensitive_values:
            masked = re.sub(rf"(?<!\d){re.escape(sensitive)}(?!\d)", cls.REDACTION, masked)
        return masked

    @classmethod
    def _sanitize_args(cls, arguments, *, sensitive_values: Sequence[str] = ()):
        if isinstance(arguments, (str, bytes)):
            return cls.REDACTION
        sanitized = []
        redact_next = False
        for argument in arguments:
            text = str(argument)
            if redact_next:
                sanitized.append(cls.REDACTION)
                redact_next = False
                continue
            option, separator, _value = text.partition("=")
            if option.startswith("-") and cls._is_sensitive_json_key(option.lstrip("-")):
                if separator:
                    sanitized.append(f"{option}={cls.REDACTION}")
                else:
                    sanitized.append(text)
                    redact_next = True
            else:
                sanitized.append(cls._mask_text(text, sensitive_values=sensitive_values))
        return sanitized
