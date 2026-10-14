"""Validation, parsing and formatting helpers (no Flask imports)."""
import calendar
import re
from datetime import datetime, timedelta, timezone, time as dtime
from decimal import ROUND_HALF_UP, Decimal

from .errors import ValidationError

CENT = Decimal("0.01")
_AMOUNT_RE = re.compile(r"[0-9]{1,12}(\.[0-9]{1,2})?")
_TOO_PRECISE_RE = re.compile(r"[0-9]+\.[0-9]{3,}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_IDEM_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------
def quantize(value):
    """Round a Decimal to whole paise using half-up rounding."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def money_str(value):
    """Canonical storage form: plain decimal string with two places."""
    return format(quantize(value), "f")


def D(value):
    """Load a stored decimal string."""
    return Decimal(value)


def parse_amount(raw, *, field="Amount", minimum=CENT, maximum=None, field_name="amount"):
    """Strictly parse a money value from user input.

    Accepts plain digits with at most two decimals.  Rejects signs, exponents,
    separators, NaN/Infinity, empty strings and over-precise values (they are
    *not* silently rounded).
    """
    if raw is None or not isinstance(raw, str) or not raw.strip():
        raise ValidationError(f"{field} is required.", field_name)
    text = raw.strip()
    if _TOO_PRECISE_RE.fullmatch(text):
        raise ValidationError(f"{field} can have at most 2 decimal places.", field_name)
    if not _AMOUNT_RE.fullmatch(text):
        raise ValidationError(
            f"{field} must be a positive number such as 1500 or 1500.50 "
            "(digits only, no commas or symbols).",
            field_name,
        )
    value = Decimal(text)
    if value < minimum:
        raise ValidationError(f"{field} must be at least {indian(minimum)}.", field_name)
    if maximum is not None and value > maximum:
        raise ValidationError(f"{field} cannot exceed {indian(maximum)}.", field_name)
    return value


def indian(value):
    """Format with Indian digit grouping: 1234567.5 -> '12,34,567.50'."""
    amount = quantize(Decimal(str(value)))
    sign = "-" if amount < 0 else ""
    whole, frac = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{whole}.{frac}"


def format_money(value, symbol="\u20b9"):
    try:
        return f"{symbol}{indian(value)}"
    except Exception:  # noqa: BLE001 - template filter must never crash
        return str(value)


def compact_money(value, symbol="\u20b9"):
    """Short axis-label form: 950 / 26.8k / 1.25L / 1.07Cr."""
    v = abs(Decimal(str(value)))
    if v >= Decimal("10000000"):
        text = f"{v / Decimal(10000000):.2f}".rstrip("0").rstrip(".") + "Cr"
    elif v >= Decimal("100000"):
        text = f"{v / Decimal(100000):.2f}".rstrip("0").rstrip(".") + "L"
    elif v >= Decimal("1000"):
        text = f"{v / Decimal(1000):.1f}".rstrip("0").rstrip(".") + "k"
    else:
        text = f"{v:.0f}"
    return f"{symbol}{text}"


# --------------------------------------------------------------------------
# Account numbers, names, text
# --------------------------------------------------------------------------
def luhn_check_digit(payload):
    total = 0
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def is_valid_account_number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{12}", value):
        return False
    return luhn_check_digit(value[:-1]) == int(value[-1])


def mask_account(number):
    number = str(number or "")
    if len(number) <= 4:
        return number
    return "X" * (len(number) - 4) + number[-4:]


def normalize_name(raw):
    name = " ".join((raw or "").split())
    if not 2 <= len(name) <= 60:
        raise ValidationError("Name must be between 2 and 60 characters.", "holder_name")
    if not all(ch.isalpha() or ch in " .'-" for ch in name) or sum(c.isalpha() for c in name) < 2:
        raise ValidationError(
            "Name may contain only letters, spaces, apostrophes, hyphens and full stops.",
            "holder_name",
        )
    return name


def clean_description(raw, max_len=60):
    text = " ".join((raw or "").split())
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValidationError("Description contains invalid characters.", "description")
    if len(text) > max_len:
        raise ValidationError(f"Description must be at most {max_len} characters.", "description")
    return text


def clean_idempotency_key(raw):
    if isinstance(raw, str) and _IDEM_RE.fullmatch(raw):
        return raw
    return None


def validate_new_pin(pin, confirm, min_len, max_len):
    pin = pin or ""
    if not re.fullmatch(r"[0-9]+", pin) or not min_len <= len(pin) <= max_len:
        raise ValidationError(
            f"PIN must be {min_len} to {max_len} digits (numbers only).", "pin"
        )
    if confirm is not None and pin != confirm:
        raise ValidationError("PIN and confirmation do not match.", "confirm_pin")
    digits = [int(c) for c in pin]
    steps = {digits[i + 1] - digits[i] for i in range(len(digits) - 1)}
    if len(set(digits)) == 1 or steps in ({1}, {-1}):
        raise ValidationError(
            "PIN is too easy to guess (repeated or sequential digits). Choose another.", "pin"
        )
    return pin


# --------------------------------------------------------------------------
# Dates and time
# --------------------------------------------------------------------------
def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_iso(value):
    return datetime.fromisoformat(value)


def add_months(dt, months):
    """Add calendar months, clamping the day (31 Jan + 1 month = 28/29 Feb)."""
    index = dt.month - 1 + months
    year = dt.year + index // 12
    month = index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def get_display_tz(name):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - tzdata may be missing on Windows
        if name == "Asia/Kolkata":
            return timezone(timedelta(hours=5, minutes=30), "IST")
        return timezone.utc


def format_dt(value, tz):
    try:
        dt = parse_iso(value) if isinstance(value, str) else value
        local = dt.astimezone(tz)
        return local.strftime("%d %b %Y, %I:%M %p ") + (local.tzname() or "")
    except Exception:  # noqa: BLE001
        return str(value)


def label_to_field(label):
    return "date_from" if label.lower().startswith("from") else "date_to"


def parse_date(raw, label):
    """Parse an optional YYYY-MM-DD filter value into a ``date`` (or None)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not _DATE_RE.fullmatch(raw):
        raise ValidationError(f"{label} must be a date in YYYY-MM-DD format.", label_to_field(label))
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError(f"{label} is not a real calendar date.", label_to_field(label)) from exc
    if not 2000 <= parsed.year <= 2100:
        raise ValidationError(f"{label} must be between the years 2000 and 2100.", label_to_field(label))
    return parsed


def day_bounds_utc(date_from, date_to, tz):
    """Inclusive [start, end) UTC bounds for local calendar dates."""
    start = end = None
    if date_from:
        start = datetime.combine(date_from, dtime.min, tzinfo=tz).astimezone(timezone.utc)
    if date_to:
        end = datetime.combine(date_to + timedelta(days=1), dtime.min, tzinfo=tz).astimezone(
            timezone.utc
        )
    return start, end
