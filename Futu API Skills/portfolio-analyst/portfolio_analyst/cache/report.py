"""Conservative request matching and deterministic report publication checks."""

from decimal import Decimal, InvalidOperation
import re

from ..child_environment import validate_account_id
from .types import CacheError, canonical, digest, utc


HEADINGS = (
    ("范围与数据时间", "Scope and data time"), ("一句话结论", "One-line conclusion"),
    ("组合概览", "Overview"), ("三个主要风险", "Three principal risks"),
    ("完整持仓表", "Complete holdings table"), ("重点研究", "Selected deep research"),
    ("期权风险", "Option risk"), ("宏观影响", "Macro impact"),
    ("观察", "Observations"), ("数据缺口与来源", "Data gaps and sources"),
)


def request_document(value):
    allowed = {"schema_version", "question", "preferences", "language", "cache_mode", "analysis_mode",
               "freshness_requirement", "account_ids", "snapshot_id", "scope", "requested_cutoff", "events"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise CacheError("INVALID_REQUEST")
    result = {"schema_version": 1, "preferences": None, "language": "zh-CN", "cache_mode": "prefer",
              "analysis_mode": "current", "freshness_requirement": "bounded", "account_ids": None,
              "snapshot_id": None, "scope": {}, "requested_cutoff": None, "events": [], **value}
    if (type(result["schema_version"]) is not int or result["schema_version"] != 1 or not isinstance(result.get("question"), str)
            or not 0 < len(result["question"].strip()) <= 8000
            or not isinstance(result["language"], str) or not 0 < len(result["language"]) <= 64
            or result["cache_mode"] not in {"prefer", "refresh_account", "refresh_all", "off"}
            or result["analysis_mode"] not in {"current", "historical"}
            or result["freshness_requirement"] not in {"bounded", "latest"}
            or not isinstance(result["scope"], dict)
            or (result["preferences"] is not None and not isinstance(result["preferences"], dict))):
        raise CacheError("INVALID_REQUEST")
    if result["account_ids"] is not None:
        ids = result["account_ids"]
        if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
            raise CacheError("INVALID_REQUEST")
        for value in ids:
            validate_account_id(value)
    if result["requested_cutoff"] is not None:
        utc(result["requested_cutoff"])
    if result["analysis_mode"] == "historical" and not isinstance(result["snapshot_id"], str):
        raise CacheError("INVALID_REQUEST")
    if not isinstance(result["events"], list) or len(result["events"]) > 32:
        raise CacheError("INVALID_REQUEST")
    for event in result["events"]:
        if event == {"type": "account_changed"}:
            continue
        if (not isinstance(event, dict) or set(event) != {"type", "cik"} or event["type"] != "sec_filing"
                or not isinstance(event["cik"], str) or not re.fullmatch(r"[0-9]{1,10}", event["cik"])):
            raise CacheError("INVALID_REQUEST")
    if len(canonical(result)) > 65536:
        raise CacheError("INVALID_REQUEST")
    return result


def fingerprint(request):
    value = {key: request[key] for key in ("preferences", "language", "analysis_mode", "snapshot_id", "scope", "requested_cutoff")}
    value["question"] = " ".join(request["question"].split())
    return digest(value)


def holdings(snapshot):
    rows = {}
    for position in snapshot["positions"]:
        if Decimal(position["quantity"]) == 0:
            continue
        key = position["market"] + "." + position["symbol"]
        row = rows.setdefault(key, {"weight": Decimal(0), "market_value_base": Decimal(0), "unrealized_pl_base": Decimal(0)})
        for name in row:
            value = position[name]
            row[name] = (None if value is None or row[name] is None else row[name] + Decimal(value))
    return rows


def _number_matches(text, expected, *, percent=False):
    value = text.strip().strip("`*").replace(",", "").replace("$", "").removesuffix("USD").strip()
    if expected is None:
        return value.lower() in {"unknown", "unavailable", "n/a", "未知", "不可用"}
    if percent:
        if not value.endswith("%"):
            return False
        value, expected = value[:-1].strip(), expected * 100
    if not re.fullmatch(r"-?\d{1,40}(?:\.\d{1,40})?", value):
        return False
    try:
        number = Decimal(value)
        tolerance = Decimal(1).scaleb(number.as_tuple().exponent) / 2
        return abs(number - expected) <= tolerance
    except InvalidOperation:
        return False


def validate_markdown(markdown, snapshot, coverage):
    if not isinstance(markdown, str) or len(markdown.encode()) > 2 * 1024 * 1024:
        raise CacheError("REPORT_INVALID")
    options = any(p["security_type"] == "OPTION" and Decimal(p["quantity"]) for p in snapshot["positions"])
    required = [pair for pair in HEADINGS if options or pair[0] != "期权风险"]
    observed = re.findall(r"^## (.+?)\s*$", markdown, re.M)
    if len(observed) != len(required) or any(title not in pair for title, pair in zip(observed, required)):
        raise CacheError("REPORT_INVALID")
    if snapshot["snapshot_id"] not in markdown or snapshot["as_of"] not in markdown:
        raise CacheError("REPORT_INVALID")
    expected = holdings(snapshot)
    if (not isinstance(coverage, dict) or set(coverage) != {"holdings", "research_complete", "open_required_items"}
            or coverage["holdings"] != sorted(expected) or type(coverage["research_complete"]) is not bool
            or not isinstance(coverage["open_required_items"], list)):
        raise CacheError("REPORT_INVALID")
    section = re.split(r"^## (?:完整持仓表|Complete holdings table)\s*$", markdown, flags=re.M)[1]
    section = re.split(r"^## ", section, maxsplit=1, flags=re.M)[0]
    lines = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    if len(lines) < 2 or "Symbol" not in lines[0]:
        raise CacheError("REPORT_INVALID")
    seen = set()
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 6 or cells[0] not in expected or cells[0] in seen:
            raise CacheError("REPORT_INVALID")
        seen.add(cells[0])
        values = expected[cells[0]]
        if not all((_number_matches(cells[1], values["weight"], percent=True),
                    _number_matches(cells[2], values["market_value_base"]),
                    _number_matches(cells[3], values["unrealized_pl_base"]))):
            raise CacheError("REPORT_INVALID")
    if seen != set(expected):
        raise CacheError("REPORT_INVALID")
