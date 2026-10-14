"""Interest calculator (Python ``Decimal`` throughout).

Two methods are offered; the choice is always shown to the user:

SIMPLE interest      I = P x R x T / 100          A = P + I
COMPOUND interest    A = P x (1 + R / (100 x n)) ^ (n x T)      I = A - P

P principal, R annual rate in percent, T time in YEARS (months / 12),
n compounding periods per year (1, 2, 4 or 12).

Assumptions / limitations (also in the README and About page)
* Educational model - not any real bank's product or day-count convention.
* A year has exactly 12 equal months; no leap-year or actual/365 handling.
* Rate is constant for the whole period; no tax (TDS), fees or penalties.
* Fractional compounding periods use a fractional exponent (the formula is
  applied continuously in T), which is a modelling simplification.
* Only the FINAL interest figure is rounded (half-up to 0.01); intermediate
  values keep 40 significant digits.
* An *estimate* never changes a balance.  Interest is only *credited* by
  ``Bank.apply_interest`` which writes an INTEREST_CREDIT transaction.
"""
import re
from decimal import Decimal, localcontext

from .errors import ValidationError
from .utils import CENT, indian, quantize

MAX_PRINCIPAL = Decimal("1000000000.00")
MAX_RATE = Decimal("100")
MAX_YEARS = Decimal("100")
COMPOUNDING_CHOICES = {1: "Yearly", 2: "Half-yearly", 4: "Quarterly", 12: "Monthly"}
METHODS = {"simple": "Simple interest", "compound": "Compound interest"}

_RATE_RE = re.compile(r"[0-9]{1,3}(\.[0-9]{1,4})?")
_TIME_RE = re.compile(r"[0-9]{1,4}(\.[0-9]{1,2})?")

FORMULAS = {
    "simple": "I = P \u00d7 R \u00d7 T \u00f7 100,   A = P + I",
    "compound": "A = P \u00d7 (1 + R \u00f7 (100 \u00d7 n)) ^ (n \u00d7 T),   I = A \u2212 P",
}


def years_from(value, unit):
    with localcontext() as ctx:
        ctx.prec = 40
        return value / Decimal(12) if unit == "months" else value


def simple_interest_amount(principal, rate_percent, months):
    """Simple interest on ``principal`` for a whole number of months (rounded)."""
    with localcontext() as ctx:
        ctx.prec = 40
        raw = principal * rate_percent * Decimal(months) / Decimal(1200)
    return quantize(raw)


def calculate(principal, rate_percent, time_value, time_unit, method="simple", compounds_per_year=1):
    """Return a dict describing the estimate.  Inputs must already be validated."""
    years = years_from(time_value, time_unit)

    def _interest_for(t_years):
        with localcontext() as ctx:
            ctx.prec = 40
            r = rate_percent / Decimal(100)
            if method == "simple":
                return principal * r * t_years
            n = Decimal(compounds_per_year)
            return principal * (Decimal(1) + r / n) ** (n * t_years) - principal

    interest = quantize(_interest_for(years))
    result = {
        "method": method,
        "method_label": METHODS[method],
        "formula": FORMULAS[method],
        "principal": quantize(principal),
        "rate": rate_percent,
        "time_value": time_value,
        "time_unit": time_unit,
        "years": years.quantize(Decimal("0.0001")),
        "compounds_per_year": compounds_per_year if method == "compound" else None,
        "interest": interest,
        "maturity": quantize(principal) + interest,
        "schedule": [],
    }
    # Year-by-year table (whole years, then the final partial period).
    whole = int(years)
    if whole and whole <= 50:
        points = [Decimal(y) for y in range(1, whole + 1)]
        if years != whole:
            points.append(years)
        running = Decimal(0)
        for t in points:
            total_int = quantize(_interest_for(t))
            result["schedule"].append(
                {
                    "label": f"Year {t.normalize():f}" if t == int(t) else f"End ({years.quantize(Decimal('0.01'))} yrs)",
                    "interest_this_period": total_int - running,
                    "interest_to_date": total_int,
                    "balance": quantize(principal) + total_int,
                }
            )
            running = total_int
    return result


def parse_inputs(principal_raw, rate_raw, time_raw, unit_raw, method_raw, compounding_raw):
    """Validate calculator form values; raise ValidationError(field=...)."""
    principal_raw = (principal_raw or "").strip()
    if not principal_raw:
        raise ValidationError("Principal is required.", "principal")
    if not re.fullmatch(r"[0-9]{1,10}(\.[0-9]{1,2})?", principal_raw):
        raise ValidationError(
            "Principal must be a positive amount with at most 2 decimal places.", "principal"
        )
    principal = Decimal(principal_raw)
    if principal < CENT:
        raise ValidationError("Principal must be greater than zero.", "principal")
    if principal > MAX_PRINCIPAL:
        raise ValidationError(f"Principal cannot exceed {indian(MAX_PRINCIPAL)}.", "principal")

    rate_raw = (rate_raw or "").strip()
    if not rate_raw:
        raise ValidationError("Annual interest rate is required.", "rate")
    if not _RATE_RE.fullmatch(rate_raw):
        raise ValidationError(
            "Rate must be a number from 0 to 100 with at most 4 decimal places.", "rate"
        )
    rate = Decimal(rate_raw)
    if rate > MAX_RATE:
        raise ValidationError("Rate cannot exceed 100% per year.", "rate")

    unit = (unit_raw or "years").strip().lower()
    if unit not in ("years", "months"):
        raise ValidationError("Choose a time unit of years or months.", "time_unit")
    time_raw = (time_raw or "").strip()
    if not time_raw:
        raise ValidationError("Time period is required.", "time_value")
    if not _TIME_RE.fullmatch(time_raw):
        raise ValidationError(
            "Time must be a positive number with at most 2 decimal places.", "time_value"
        )
    time_value = Decimal(time_raw)
    if time_value <= 0:
        raise ValidationError("Time period must be greater than zero.", "time_value")
    if years_from(time_value, unit) > MAX_YEARS:
        raise ValidationError("Time period cannot exceed 100 years.", "time_value")

    method = (method_raw or "simple").strip().lower()
    if method not in METHODS:
        raise ValidationError("Choose simple or compound interest.", "method")
    try:
        n = int((compounding_raw or "1").strip())
    except ValueError as exc:
        raise ValidationError("Invalid compounding frequency.", "compounding") from exc
    if method == "compound" and n not in COMPOUNDING_CHOICES:
        raise ValidationError("Choose yearly, half-yearly, quarterly or monthly compounding.", "compounding")
    return principal, rate, time_value, unit, method, n
