"""Small, strict boundaries shared by offline calculations and public readers."""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from zoneinfo import ZoneInfo


class ResearchError(ValueError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


def require(condition, code, message=""):
    if not condition:
        raise ResearchError(code, message)


def fields(value, allowed, required=()):
    require(isinstance(value, dict), "INVALID_INPUT", "Expected an object")
    require(not set(value) - set(allowed), "UNKNOWN_FIELDS",
            "Unrecognized fields are not accepted")
    require(set(required) <= set(value), "MISSING_FIELDS")


def cik(value):
    require(isinstance(value, (str, int)) and not isinstance(value, bool),
            "INVALID_CIK")
    text = str(value)
    require(bool(re.fullmatch(r"[0-9]{1,10}", text)) and int(text) > 0, "INVALID_CIK")
    return text.zfill(10)


def accession(value):
    require(isinstance(value, str) and
            bool(re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", value)), "INVALID_ACCESSION")
    return value


def number(value, *, nonnegative=False):
    if value is None:
        return None
    require(not isinstance(value, bool), "INVALID_NUMBER")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ResearchError("INVALID_NUMBER") from None
    require(result.is_finite(), "INVALID_NUMBER")
    require(not nonnegative or result >= 0, "NEGATIVE_VALUE")
    return result


def day(value):
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ResearchError("INVALID_DATE") from None


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ResearchError("INVALID_TIMESTAMP") from None
    require(parsed.tzinfo is not None, "TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def available_by(published, cutoff):
    """A date-only filing is conservatively available after that Eastern day."""
    cutoff_time = timestamp(cutoff)
    if len(str(published)) == 10:
        upper_bound = datetime.combine(day(published) + timedelta(days=1), time(),
                                       ZoneInfo("America/New_York"))
        return upper_bound <= cutoff_time
    return timestamp(published) <= cutoff_time


def quarterly_total(facts):
    require(len(facts) == 4, "PERIOD_UNVERIFIED", "Exactly four quarters required")
    ordered = sorted(facts, key=lambda fact: day(fact["start"]))
    bases = {(f.get("concept"), f.get("unit"), f.get("basis")) for f in ordered}
    require(len(bases) == 1 and all(next(iter(bases))), "BASIS_MISMATCH")
    require(all(f.get("aggregation") == "additive" for f in ordered),
            "NONADDITIVE_METRIC", "An explicitly verified additive flow is required")
    unit = str(ordered[0]["unit"]).lower()
    concept = str(ordered[0]["concept"]).lower()
    require("share" not in unit and unit not in {"pure", "percent", "%"}
            and "earningspershare" not in concept and "weightedaverage" not in concept,
            "NONADDITIVE_METRIC")
    previous = None
    total = Decimal(0)
    for fact in ordered:
        require(fact.get("period_kind") == "standalone", "PERIOD_UNVERIFIED",
                "Four standalone quarters required")
        start, end = day(fact["start"]), day(fact["end"])
        require(75 <= (end - start).days + 1 <= 105, "PERIOD_UNVERIFIED")
        require(previous is None or start == previous + timedelta(days=1),
                "PERIOD_GAP_OR_OVERLAP")
        value = number(fact.get("value"))
        require(value is not None, "MISSING_VALUE")
        total += value
        previous = end
    return total


def json_value(value):
    """Serialize precise decimal values without silently converting to float."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value
