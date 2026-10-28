"""Deterministic normalization used for source matching, item matching, and duplicate detection."""

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

_NUMERIC_TOKEN = re.compile(r"[\dOo$][\dOo,.$]*")
_TRAILING_PAREN = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
_THOUSANDS_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_PLAIN_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%b %d %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%B %d, %Y",
    "%d %B %Y",
)
_CURRENCY_ALIASES = {"$": "USD", "US$": "USD", "USD": "USD", "€": "EUR", "£": "GBP"}


def fix_ocr_digits(text: str) -> str:
    """Letter O inside a number is a zero: '2O26' -> '2026', '$3,500.O0' -> '$3,500.00'."""

    def repl(match: re.Match[str]) -> str:
        token = match.group()
        return re.sub("[Oo]", "0", token) if any(c.isdigit() for c in token) else token

    return _NUMERIC_TOKEN.sub(repl, text)


def squash(text: str) -> str:
    """Lowercase alphanumerics only, after OCR fixes. Used for substring source matching."""
    return re.sub(r"[^a-z0-9]", "", fix_ocr_digits(text).lower())


def numbers_in(text: str) -> set[Decimal]:
    """Every number that appears in the text. Ambiguous comma groups are read both ways."""
    fixed = fix_ocr_digits(text)
    found = {Decimal(m.replace(",", "")) for m in _THOUSANDS_NUMBER.findall(fixed)}
    found |= {Decimal(m) for m in _PLAIN_NUMBER.findall(fixed)}
    return found


def to_decimal(value: float) -> Decimal:
    return Decimal(str(value))


def round_cents(amount: Decimal) -> int:
    """Half-up to a whole number of cents."""
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents(value: float | None) -> int | None:
    if value is None:
        return None
    return round_cents(to_decimal(value) * 100)


def split_parenthetical(text: str) -> tuple[str, str | None]:
    """'WidgetA (rush order)' -> ('WidgetA', 'rush order')."""
    match = _TRAILING_PAREN.match(text.strip())
    if not match or not match.group(1):
        return text.strip(), None
    return match.group(1), match.group(2).strip() or None


def item_key(description: str) -> str:
    """Catalog comparison key: whitespace removed. 'Widget A' -> 'WidgetA'."""
    return re.sub(r"\s+", "", description)


def normalize_invoice_number(raw: str | None) -> str | None:
    """'Inv #: 1002', 'INV 1012', '#INV-1010' -> 'INV-1002', 'INV-1012', 'INV-1010'."""
    if not raw:
        return None
    core = re.sub(r"[^A-Z0-9]", "", fix_ocr_digits(raw).upper())
    core = re.sub(r"^(INVOICE|INV)(NUMBER|NUM|NO)?", "", core)
    return f"INV-{core}" if core else None


def normalize_vendor(raw: str | None) -> str | None:
    """'Widgets Inc.' -> 'widgets inc'; a trailing parenthetical is dropped."""
    if not raw:
        return None
    name, _ = split_parenthetical(raw)
    name = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    return name or None


def normalize_currency(raw: str | None) -> str:
    """Missing or '$' means USD."""
    if not raw or not raw.strip():
        return "USD"
    value = raw.strip().upper()
    return _CURRENCY_ALIASES.get(value, value)


def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", fix_ocr_digits(raw).strip())
    text = re.sub(r"\s+", " ", text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def due_date_text(raw: str | None) -> str:
    """One format for every invoice ('02/28/2026' -> '2026-02-28'); a value that isn't a date is shown as written."""
    if raw is None:
        return "—"
    parsed = parse_date(raw)
    return parsed.isoformat() if parsed else f"{raw} (not a date)"


def format_money(amount_cents: int | None, currency: str = "USD") -> str:
    if amount_cents is None:
        return "—"
    sign = "-" if amount_cents < 0 else ""
    value = f"{abs(amount_cents) / 100:,.2f}"
    return f"{sign}${value}" if currency == "USD" else f"{sign}{value} {currency}"
