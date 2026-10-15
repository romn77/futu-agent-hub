"""Evaluation-only checks against retained evidence, never a production account reader.

Supported Markdown tables use explicit symbol/weight/value/P&L headers. Unrecognized
formats remain unverified; this module never repairs reports or certifies research prose.
"""

from decimal import Decimal, localcontext
import re


ALIASES = {
    "symbol": ("symbol", "ticker", "标的", "代码", "股票代码", "证券代码"),
    "weight": ("current weight", "weight", "当前权重", "净资产权重", "权重", "仓位", "仓位占比"),
    "market_value_base": ("market value", "市值", "持仓市值"),
    "unrealized_pl_base": ("unrealized p&l", "unrealized pnl", "未实现盈亏", "浮动盈亏", "浮盈亏"),
}
UNKNOWN = {"unknown", "unavailable", "n/a", "未知", "不可用", "无法取得", "未提供", "缺失"}
CURRENCIES = re.compile(r"\b(?:USD|HKD|CNY|EUR|JPY|GBP|CAD|AUD|SGD)\b", re.I)
LOCAL_CURRENCIES = {
    "美元": "USD",
    "港元": "HKD",
    "港币": "HKD",
    "人民币": "CNY",
    "欧元": "EUR",
    "日元": "JPY",
    "英镑": "GBP",
    "加元": "CAD",
    "澳元": "AUD",
    "新加坡元": "SGD",
}
SCALES = {
    "billion": "1e9",
    "十亿": "1e9",
    "million": "1e6",
    "百万": "1e6",
    "thousand": "1e3",
    "千": "1e3",
    "亿": "1e8",
    "万": "1e4",
}


def _clean(text):
    return text.strip().strip("*` ").replace("−", "-").replace("％", "%")


def _columns(cells):
    found = {}
    for index, cell in enumerate(cells):
        title = _clean(cell).casefold()
        for field, names in ALIASES.items():
            if any(
                title == name or re.match(re.escape(name) + r"\s*[(（/%]", title) for name in names
            ):
                if field in found:
                    return {}
                found[field] = index
    return found if set(found) == set(ALIASES) else {}


def _scale(text):
    for suffix, scale in SCALES.items():
        if suffix in text.casefold():
            return Decimal(scale), suffix
    return Decimal(1), ""


def _number(text, header, *, percent=False):
    value = _clean(text)
    if not percent and ("%" in value or "%" in header or "百分比" in header):
        raise ValueError("percentage is not a monetary amount")
    if not value:
        raise ValueError("empty cell")
    if value.casefold() in UNKNOWN:
        return None, Decimal(0)
    value = CURRENCIES.sub("", value).replace("美元", "").replace("$", "").strip()
    scale, suffix = _scale(value)
    if suffix:
        value = re.sub(re.escape(suffix), "", value, flags=re.I).strip()
    else:
        scale, _ = _scale(header)
    if percent:
        if "%" not in value and "%" not in header:
            raise ValueError("weight unit missing")
        value = value.removesuffix("%").strip()
        scale = Decimal("0.01")
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1].strip()
    # No prose, multiple numbers, malformed thousands, infinity or guessed units.
    if len(value) > 100 or not re.fullmatch(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", value):
        raise ValueError("unsupported numeric cell")
    amount = Decimal(value.replace(",", ""))
    quantum = Decimal(1).scaleb(amount.as_tuple().exponent) * scale
    return amount * scale, quantum / 2


def _expected(snapshot):
    rows = {}
    for position in snapshot["positions"]:
        if Decimal(position["quantity"]) == 0:
            continue
        identity = position["market"] + "." + position["symbol"]
        values = {
            field: None if position[field] is None else Decimal(position[field])
            for field in ALIASES
            if field != "symbol"
        }
        if identity in rows:
            values = {
                field: None
                if value is None or rows[identity][field] is None
                else value + rows[identity][field]
                for field, value in values.items()
            }
        rows[identity] = values
    return rows


def check_holdings(section, snapshot):
    """Compare nonzero canonical holdings using source values, not reconstructed NAV."""
    with localcontext() as context:
        context.prec = 600
        return _check_holdings(section, snapshot)


def _check_holdings(section, snapshot):
    expected = _expected(snapshot)
    issues, seen, checked = [], set(), 0
    columns, headers, fence = {}, [], None
    for line_number, line in enumerate(section.splitlines(), 1):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
            continue
        if fence or not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        candidate = _columns(cells)
        if candidate:
            columns, headers = candidate, cells
            continue
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        if not columns:
            continue
        symbol_index = columns["symbol"]
        label = _clean(cells[symbol_index]) if symbol_index < len(cells) else ""
        identity = label if label in expected else "US." + label
        if identity not in expected:
            if re.fullmatch(r"(?:[A-Z]{2}\.)?[A-Z0-9][A-Z0-9.-]*", label):
                issues.append({"code": "UNEXPECTED_HOLDING", "symbol": label, "line": line_number})
            continue
        if identity in seen:
            issues.append({"code": "DUPLICATE_HOLDING", "symbol": identity, "line": line_number})
        seen.add(identity)
        if len(cells) != len(headers):
            issues.append({"code": "COLUMN_COUNT", "symbol": identity, "line": line_number})
            continue
        for field, wanted in expected[identity].items():
            index = columns[field]
            header, cell = headers[index], cells[index]
            declared = {currency.upper() for currency in CURRENCIES.findall(header + " " + cell)}
            declared.update(
                code for label, code in LOCAL_CURRENCIES.items() if label in header + " " + cell
            )
            if declared - {snapshot["base_currency"]}:
                issues.append({"code": "CURRENCY_MISMATCH", "symbol": identity, "field": field})
                continue
            try:
                actual, tolerance = _number(cell, header, percent=field == "weight")
            except ValueError:
                issues.append({"code": "VALUE_UNVERIFIABLE", "symbol": identity, "field": field})
                continue
            checked += 1
            equal = actual is None and wanted is None
            if actual is not None and wanted is not None:
                equal = abs(actual - wanted) <= tolerance
            if not equal:
                issues.append({"code": "VALUE_MISMATCH", "symbol": identity, "field": field})
    if not columns:
        issues.append({"code": "HEADER_UNVERIFIABLE"})
    issues.extend(
        {"code": "MISSING_HOLDING", "symbol": identity}
        for identity in sorted(expected.keys() - seen)
    )
    return {
        "status": "VERIFIED" if not issues else "UNVERIFIED",
        "issues": issues,
        "checked_fields": checked,
        "symbols": sorted(seen),
    }
