"""Narrow subprocess boundary around audited read-only Skill scripts."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
from datetime import date
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Sequence

from .child_environment import controlled_child_environment, validate_account_id
from .skill_trust import (
    audited_command,
    audited_command_names,
    load_audited_skill,
    load_option_quote_capability,
)

try:
    import resource
except ImportError:  # pragma: no cover - Windows uses bounded concurrent pipe readers.
    resource = None


def _limit_child_output_files() -> None:
    if resource is not None:
        maximum = 4 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, maximum))


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
)
_COMMAND_OPTION_SCHEMA = MappingProxyType(
    {record.command: record.options for record in _COMMAND_OPTIONS}
)


class FutuAdapter:
    """Execute only fixed read-only scripts from an audited futuapi Skill."""

    READ_ONLY_COMMANDS = audited_command_names() | frozenset({"get_option_quote"})
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
    MAX_RESPONSE_BYTES = 4 * 1024 * 1024
    MAX_OPTION_RESPONSE_BYTES = 256 * 1024
    _TEXT_ACCOUNT_NUMERIC_IDENTIFIER = re.compile(
        r"(?ix)"
        r"(?P<label>"
        r"(?:\baccount(?:[ _-]?id)?|账户(?:\s*ID)?)"
        r"(?:\s*[:=]\s*|\s+)"
        r")"
        r"(?P<value>\d{5,})(?!\d)"
    )
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
        self._protected_sensitive_values: set[str] = set()

    def protect_sensitive_values(self, values: Sequence[str]) -> None:
        """Register discovered account IDs so every later result masks the full account set."""

        validated: set[str] = set()
        for value in values:
            try:
                validated.add(validate_account_id(str(value)))
            except ValueError:
                raise ValueError("discovered account ID has an invalid format") from None
        self._protected_sensitive_values.update(validated)

    def validate_request(self, name: str, arguments: Sequence[str]) -> FutuRequest:
        """Validate a request without exposing an executable source path."""

        normalized = name.lower()
        if any(term in normalized for term in self.DENY_TERMS):
            raise UnsafeFutuCommand(f"write-capable Futu command is forbidden: {name}")
        if name == "get_option_quote":
            return self._validate_option_quote_request(tuple(map(str, arguments)))
        if audited_command(name) is None:
            raise UnsafeFutuCommand(f"Futu command is not in the read-only allowlist: {name}")
        option_names = (
            str(argument).lower() for argument in arguments if str(argument).startswith("-")
        )
        if any(term in option for option in option_names for term in self.CREDENTIAL_FLAG_TERMS):
            raise UnsafeFutuCommand("credential and unlock arguments are forbidden")
        if name == "get_kline":
            return self._validate_kline_request(tuple(map(str, arguments)))
        if name in {"get_history_orders", "get_history_order_fill_list"}:
            return self._validate_trade_history_request(name, tuple(map(str, arguments)))
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

    def _validate_option_quote_request(self, arguments: tuple[str, ...]) -> FutuRequest:
        capability, _detail = load_option_quote_capability(self.skill_dir)
        if capability is None:
            raise UnsafeFutuCommand("option quote capability is unavailable")
        if len(arguments) != 2 or arguments[1] != "--json":
            raise UnsafeFutuCommand("option quote requires one canonical leg and --json")
        try:
            legs = json.loads(
                arguments[0],
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            raise UnsafeFutuCommand("option quote legs JSON is invalid") from None
        if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict):
            raise UnsafeFutuCommand("option quote requires exactly one leg")
        leg = legs[0]
        if set(leg) != {"code", "action", "quantity"}:
            raise UnsafeFutuCommand("option quote leg fields are outside the command policy")
        code = leg["code"]
        if (
            not isinstance(code, str)
            or re.fullmatch(r"(?:US|HK)\.[A-Z0-9][A-Z0-9.-]{0,63}", code) is None
        ):
            raise UnsafeFutuCommand("option quote identity is outside the command policy")
        if leg["action"] != "BUY" or type(leg["quantity"]) is not float or leg["quantity"] != 1.0:
            raise UnsafeFutuCommand("option quote permits only BUY quantity 1.0")
        canonical = json.dumps(
            [{"code": code, "action": "BUY", "quantity": 1.0}],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if arguments[0] != canonical:
            raise UnsafeFutuCommand("option quote legs JSON is not canonical")
        return FutuRequest("get_option_quote", (canonical, "--json"))

    @staticmethod
    def _validate_trade_history_request(name: str, arguments: tuple[str, ...]) -> FutuRequest:
        value_options = {
            "--acc-id",
            "--market",
            "--trd-env",
            "--start",
            "--end",
            "--security-firm",
            "--ctx-type",
        }
        if name == "get_history_orders":
            value_options.add("--limit")
        values: dict[str, str | None] = {}
        normalized: list[str] = []
        sensitive: list[str] = []
        index = 0
        while index < len(arguments):
            option = arguments[index]
            if option == "--json":
                if option in values:
                    raise UnsafeFutuCommand("duplicate Futu options are forbidden")
                values[option] = None
                normalized.append(option)
                index += 1
                continue
            if option not in value_options or option in values or index + 1 >= len(arguments):
                raise UnsafeFutuCommand("Futu option is not in the command policy")
            value = arguments[index + 1]
            if not value or value.startswith("--"):
                raise UnsafeFutuCommand("Futu option requires a value")
            values[option] = value
            normalized.extend((option, value))
            index += 2
        required = {
            "--acc-id",
            "--market",
            "--trd-env",
            "--start",
            "--end",
            "--security-firm",
            "--ctx-type",
            "--json",
        }
        if name == "get_history_orders":
            required.add("--limit")
        if set(values) != required:
            raise UnsafeFutuCommand("historical trade read requires the complete option policy")
        try:
            validate_account_id(str(values["--acc-id"]))
            start = date.fromisoformat(str(values["--start"]))
            end = date.fromisoformat(str(values["--end"]))
        except ValueError:
            raise UnsafeFutuCommand("historical trade account/date values are invalid") from None
        if start > end or end > date.today() or (end - start).days > 90:
            raise UnsafeFutuCommand("historical trade date range is outside the bounded policy")
        if values["--market"] not in _MARKETS:
            raise UnsafeFutuCommand("historical trade market is outside the command policy")
        if values["--trd-env"] not in _TRADING_ENVIRONMENTS:
            raise UnsafeFutuCommand("historical trade environment is outside the command policy")
        if values["--security-firm"] not in _SECURITY_FIRMS or values["--ctx-type"] != "SEC":
            raise UnsafeFutuCommand("historical trade context is outside the command policy")
        if name == "get_history_orders" and values["--limit"] != "200":
            raise UnsafeFutuCommand("historical order limit must be exactly 200")
        sensitive.append(str(values["--acc-id"]))
        return FutuRequest(name, tuple(normalized), tuple(sensitive))

    @staticmethod
    def _validate_kline_request(arguments: tuple[str, ...]) -> FutuRequest:
        """Require the one audited historical adjusted-daily invocation shape."""

        if not arguments or not re.fullmatch(r"[A-Z]{2,4}\.[A-Z0-9][A-Z0-9.-]{0,31}", arguments[0]):
            raise UnsafeFutuCommand("K-line security code is outside the command policy")
        options: dict[str, str | None] = {}
        index = 1
        value_options = {"--ktype", "--start", "--end", "--rehab", "--session", "--num"}
        while index < len(arguments):
            option = arguments[index]
            if option == "--json":
                if option in options:
                    raise UnsafeFutuCommand("duplicate Futu options are forbidden")
                options[option] = None
                index += 1
                continue
            if option not in value_options or option in options or index + 1 >= len(arguments):
                raise UnsafeFutuCommand("Futu option is not in the command policy")
            options[option] = arguments[index + 1]
            index += 2
        required = {"--ktype", "--start", "--end", "--rehab", "--session", "--num", "--json"}
        if set(options) != required:
            raise UnsafeFutuCommand("adjusted daily history requires the complete option policy")
        if options["--ktype"] != "1d" or options["--rehab"] != "forward":
            raise UnsafeFutuCommand("only forward-adjusted daily history is permitted")
        if options["--session"] != "NONE" or options["--num"] != "1000":
            raise UnsafeFutuCommand("K-line option value is outside the command policy")
        try:
            start = date.fromisoformat(str(options["--start"]))
            end = date.fromisoformat(str(options["--end"]))
        except ValueError:
            raise UnsafeFutuCommand("K-line dates must use YYYY-MM-DD") from None
        if start > end:
            raise UnsafeFutuCommand("K-line start date must not follow end date")
        normalized = [arguments[0]]
        for option in ("--ktype", "--start", "--end", "--rehab", "--session", "--num"):
            normalized.extend((option, str(options[option])))
        normalized.append("--json")
        return FutuRequest("get_kline", tuple(normalized))

    def run(
        self, name: str, arguments: Sequence[str], *, timeout: float = 30
    ) -> subprocess.CompletedProcess[str]:
        """Run an approved read-only command without a shell."""

        request = self.validate_request(name, arguments)
        child_environment = controlled_child_environment(purpose="adapter")
        sensitive_values = self._sensitive_values(request, child_environment)
        try:
            completed = self._run_unsanitized(name, arguments, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise self._sanitize_timeout(exc, sensitive_values) from None
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
        try:
            completed = self._run_unsanitized(name, arguments, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise self._sanitize_timeout(exc, sensitive_values) from None
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
                payload = (
                    json.loads(candidate, parse_float=Decimal, parse_int=Decimal)
                    if name == "get_option_quote"
                    else json.loads(candidate)
                )
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                return payload
        raise FutuReadError("audited Futu read returned no valid JSON object")

    def _sensitive_values(
        self, request: FutuRequest, child_environment: dict[str, str]
    ) -> tuple[str, ...]:
        protected = getattr(self, "_protected_sensitive_values", set())
        return tuple(
            dict.fromkeys(
                (
                    *request.sensitive_values,
                    *sorted(protected),
                    *(value for key, value in child_environment.items() if key == "FUTU_ACC_ID"),
                )
            )
        )

    @classmethod
    def _sanitize_timeout(
        cls, exc: subprocess.TimeoutExpired, sensitive_values: Sequence[str]
    ) -> subprocess.TimeoutExpired:
        return subprocess.TimeoutExpired(
            cmd=cls._sanitize_args(exc.cmd, sensitive_values=sensitive_values),
            timeout=exc.timeout,
            output=cls.sanitize_output(exc.output, sensitive_values=sensitive_values),
            stderr=cls.sanitize_output(exc.stderr, sensitive_values=sensitive_values),
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
        option_capability = None
        if request.command == "get_option_quote":
            option_capability, _detail = load_option_quote_capability(self.skill_dir)
            if option_capability is None:
                raise UnsafeFutuCommand("option quote capability is unavailable")
            command_path = option_capability.file.relative_path
        else:
            assert command_record is not None
            command_path = command_record.script_path
        with tempfile.TemporaryDirectory(prefix="portfolio-futu-") as temporary:
            snapshot_root = Path(temporary) / "futuapi"
            for record, content in audited.files:
                destination = snapshot_root / record.relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            if option_capability is not None:
                destination = snapshot_root / option_capability.file.relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(option_capability.content)
            command = [
                self.python_executable,
                "-s",
                str(snapshot_root / command_path),
                *request.arguments,
            ]
            try:
                if resource is None:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=child_environment,
                    )
                    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
                    state = {"size": 0, "overflow": False}
                    lock = threading.Lock()

                    def drain(name: str, stream: object) -> None:
                        while True:
                            chunk = stream.read(65536)  # type: ignore[attr-defined]
                            if not chunk:
                                return
                            with lock:
                                response_budget = (
                                    self.MAX_OPTION_RESPONSE_BYTES
                                    if request.command == "get_option_quote"
                                    else self.MAX_RESPONSE_BYTES
                                )
                                remaining = response_budget - state["size"]
                                if remaining > 0:
                                    chunks[name].append(chunk[:remaining])
                                state["size"] += len(chunk)
                                if state["size"] > response_budget:
                                    state["overflow"] = True
                                    process.terminate()
                                    return

                    readers = [
                        threading.Thread(
                            target=drain, args=("stdout", process.stdout), daemon=True
                        ),
                        threading.Thread(
                            target=drain, args=("stderr", process.stderr), daemon=True
                        ),
                    ]
                    for reader in readers:
                        reader.start()
                    try:
                        process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=1)
                        raise
                    finally:
                        for reader in readers:
                            reader.join(timeout=1)
                    if state["overflow"]:
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=1)
                        raise FutuReadError(
                            "audited Futu read exceeded the bounded response budget"
                        )
                    completed = subprocess.CompletedProcess(
                        process.args,
                        process.returncode,
                        b"".join(chunks["stdout"]).decode("utf-8", errors="replace"),
                        b"".join(chunks["stderr"]).decode("utf-8", errors="replace"),
                    )
                else:
                    with (
                        tempfile.TemporaryFile() as stdout_file,
                        tempfile.TemporaryFile() as stderr_file,
                    ):
                        process = subprocess.run(
                            command,
                            stdout=stdout_file,
                            stderr=stderr_file,
                            check=False,
                            encoding="utf-8",
                            errors="replace",
                            env=child_environment,
                            preexec_fn=_limit_child_output_files if resource is not None else None,
                            timeout=timeout,
                        )
                        stdout_file.seek(0, 2)
                        stderr_file.seek(0, 2)
                        stdout_size = stdout_file.tell()
                        stderr_size = stderr_file.tell()
                        process_stdout = process.stdout if isinstance(process.stdout, str) else None
                        process_stderr = process.stderr if isinstance(process.stderr, str) else None
                        captured_size = (
                            stdout_size
                            + stderr_size
                            + sum(
                                len(value.encode("utf-8"))
                                for value in (process_stdout, process_stderr)
                                if value is not None
                            )
                        )
                        response_budget = (
                            self.MAX_OPTION_RESPONSE_BYTES
                            if request.command == "get_option_quote"
                            else self.MAX_RESPONSE_BYTES
                        )
                        if captured_size > response_budget:
                            raise FutuReadError(
                                "audited Futu read exceeded the bounded response budget"
                            )
                        stdout_file.seek(0)
                        stderr_file.seek(0)
                        stdout = (
                            process_stdout
                            if process_stdout is not None
                            else stdout_file.read(stdout_size).decode("utf-8", errors="replace")
                        )
                        stderr = (
                            process_stderr
                            if process_stderr is not None
                            else stderr_file.read(stderr_size).decode("utf-8", errors="replace")
                        )
                        completed = subprocess.CompletedProcess(
                            process.args,
                            process.returncode,
                            stdout,
                            stderr,
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
        masked = cls._TEXT_ACCOUNT_NUMERIC_IDENTIFIER.sub(
            lambda match: f"{match.group('label')}{cls.REDACTION}", output
        )
        masked = cls._TEXT_SENSITIVE_VALUE.sub(
            lambda match: f"{match.group('label')}{cls.REDACTION}", masked
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
