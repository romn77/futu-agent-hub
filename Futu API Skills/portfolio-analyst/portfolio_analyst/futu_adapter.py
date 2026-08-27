"""Narrow subprocess boundary around compatible read-only Skill scripts."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .skill_trust import READ_ONLY_SCRIPTS as ALLOWED_READ_ONLY_SCRIPTS
from .skill_trust import validate_futuapi_skill


class UnsafeFutuCommand(ValueError):
    """Raised when a command falls outside the fixed read-only boundary."""


class FutuAdapter:
    """Execute only fixed read-only scripts from a compatible futuapi Skill."""

    READ_ONLY_SCRIPTS = ALLOWED_READ_ONLY_SCRIPTS
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
        validation = validate_futuapi_skill(skill_dir)
        if not validation.valid:
            raise ValueError(f"compatible futuapi Skill required: {validation.detail}")
        assert validation.root is not None
        self.skill_dir = validation.root
        self.python_executable = python_executable or sys.executable

    def command(self, name: str, arguments: Sequence[str]) -> list[str]:
        """Build an argument-array command for an approved script."""

        validation = validate_futuapi_skill(self.skill_dir)
        if not validation.valid:
            raise ValueError(f"compatible futuapi Skill required: {validation.detail}")

        normalized = name.lower()
        if any(term in normalized for term in self.DENY_TERMS):
            raise UnsafeFutuCommand(f"write-capable Futu command is forbidden: {name}")
        relative_script = self.READ_ONLY_SCRIPTS.get(name)
        if relative_script is None:
            raise UnsafeFutuCommand(f"Futu command is not in the read-only allowlist: {name}")
        option_names = (
            str(argument).lower() for argument in arguments if str(argument).startswith("-")
        )
        if any(term in option for option in option_names for term in self.CREDENTIAL_FLAG_TERMS):
            raise UnsafeFutuCommand("credential and unlock arguments are forbidden")
        script = (self.skill_dir / relative_script).resolve()
        if not script.is_relative_to(self.skill_dir) or not script.is_file():
            raise FileNotFoundError(f"approved futuapi script not found: {relative_script}")
        return [self.python_executable, str(script), *map(str, arguments)]

    def run(
        self, name: str, arguments: Sequence[str], *, timeout: float = 30
    ) -> subprocess.CompletedProcess[str]:
        """Run an approved read-only command without a shell."""

        command = self.command(name, arguments)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise subprocess.TimeoutExpired(
                cmd=self._sanitize_args(exc.cmd),
                timeout=exc.timeout,
                output=self.sanitize_output(exc.output),
                stderr=self.sanitize_output(exc.stderr),
            ) from None
        return subprocess.CompletedProcess(
            args=self._sanitize_args(completed.args),
            returncode=completed.returncode,
            stdout=self.sanitize_output(completed.stdout),
            stderr=self.sanitize_output(completed.stderr),
        )

    @classmethod
    def sanitize_output(cls, output: str | bytes | None) -> str:
        """Mask sensitive identifiers and credentials in JSON or human-readable output."""

        if not output:
            return ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        try:
            payload = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return cls._mask_text(output)
        return json.dumps(cls._mask_json(payload), ensure_ascii=False)

    @classmethod
    def _mask_json(cls, value):
        if isinstance(value, dict):
            return {
                key: cls.REDACTION if cls._is_sensitive_json_key(key) else cls._mask_json(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._mask_json(item) for item in value]
        if isinstance(value, str):
            return cls._mask_text(value)
        return value

    @classmethod
    def _is_sensitive_json_key(cls, key: object) -> bool:
        normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
        return normalized in cls.SENSITIVE_JSON_KEYS or any(
            marker in normalized
            for marker in ("password", "passwd", "token", "secret", "credential", "apikey")
        )

    @classmethod
    def _mask_text(cls, output: str) -> str:
        return cls._TEXT_SENSITIVE_VALUE.sub(
            lambda match: f"{match.group('label')}{cls.REDACTION}", output
        )

    @classmethod
    def _sanitize_args(cls, arguments):
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
                sanitized.append(text)
        return sanitized
