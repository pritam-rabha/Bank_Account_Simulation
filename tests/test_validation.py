"""Invalid input handling and the pure validation helpers."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.errors import ValidationError
from app.utils import (add_months, clean_description, is_valid_account_number, luhn_check_digit,
                       mask_account, money_str, normalize_name, parse_amount, parse_date,
                       validate_new_pin)


@pytest.mark.parametrize("raw,expected", [
    ("100", "100"), ("0.01", "0.01"), ("1500.50", "1500.50"), ("  25 ", "25"), ("7.5", "7.5"),
])
def test_parse_amount_accepts_valid_values(raw, expected):
    value = parse_amount(raw)
    assert isinstance(value, Decimal)
    assert value == Decimal(expected)


@pytest.mark.parametrize("raw", [
    "", "   ", "abc", "-5", "+5", "1e3", "NaN", "Infinity", "-0", "0", "0.00", "1,000", "100.",
    ".5", "12 34", "\u0661\u0662\u0663", "1_000", None,
])
def test_parse_amount_rejects_malformed_values(raw):
    with pytest.raises(ValidationError):
        parse_amount(raw)


def test_parse_amount_rejects_excess_precision_instead_of_rounding():
    with pytest.raises(ValidationError, match="2 decimal places"):
        parse_amount("10.005")
    with pytest.raises(ValidationError, match="2 decimal places"):
        parse_amount("10.999")


def test_parse_amount_enforces_minimum_and_maximum():
    with pytest.raises(ValidationError):
        parse_amount("499.99", minimum=Decimal("500"))
    with pytest.raises(ValidationError):
        parse_amount("1000000.01", maximum=Decimal("1000000.00"))
    assert parse_amount("1000000.00", maximum=Decimal("1000000.00")) == Decimal("1000000.00")


def test_money_str_uses_two_places_and_half_up_rounding():
    assert money_str(Decimal("5")) == "5.00"
    assert money_str(Decimal("0.005")) == "0.01"
    assert money_str(Decimal("2.675")) == "2.68"  # float would give 2.67


def test_decimal_arithmetic_has_no_float_drift():
    total = sum((Decimal("0.10") for _ in range(10)), Decimal("0"))
    assert total == Decimal("1.00")


@pytest.mark.parametrize("name", ["Asha Rao", "O'Neil-Smith Jr.", "  Meera   Das  ", "Zoë Müller"])
def test_normalize_name_accepts(name):
    assert "  " not in normalize_name(name)


@pytest.mark.parametrize("name", ["", "A", "x" * 61, "Bob123", "<script>", "a=b", "12", "A.-"])
def test_normalize_name_rejects(name):
    with pytest.raises(ValidationError):
        normalize_name(name)


def test_description_validation():
    assert clean_description("  hello   world ") == "hello world"
    with pytest.raises(ValidationError):
        clean_description("x" * 61)
    with pytest.raises(ValidationError):
        clean_description("bad\x00char")


@pytest.mark.parametrize("pin,confirm", [
    ("12", "12"), ("abcd", "abcd"), ("1234567", "1234567"), ("0000", "0000"), ("1234", "1234"),
    ("4321", "4321"), ("482916", "111111"), ("", ""), ("48 91", "48 91"),
])
def test_validate_new_pin_rejects(pin, confirm):
    with pytest.raises(ValidationError):
        validate_new_pin(pin, confirm, 4, 6)


def test_validate_new_pin_accepts_good_pin():
    assert validate_new_pin("482916", "482916", 4, 6) == "482916"
    assert validate_new_pin("7391", "7391", 4, 6) == "7391"


def test_account_number_luhn_validation():
    payload = "10123456789"
    number = payload + str(luhn_check_digit(payload))
    assert is_valid_account_number(number)
    wrong = payload + str((luhn_check_digit(payload) + 1) % 10)
    assert not is_valid_account_number(wrong)
    for bad in ["", "abc", "12345", "1234567890123", number[:-1], None, number + " "]:
        assert not is_valid_account_number(bad)


def test_mask_account_shows_last_four_only():
    assert mask_account("102026000012") == "XXXXXXXX0012"
    assert mask_account("") == ""


def test_parse_date_validation():
    assert parse_date("2026-02-28", "From date").isoformat() == "2026-02-28"
    assert parse_date("", "From date") is None
    for bad in ["2026-02-30", "28-02-2026", "2026/02/28", "yesterday", "1999-01-01", "2026-13-01"]:
        with pytest.raises(ValidationError):
            parse_date(bad, "From date")


def test_add_months_clamps_to_month_end():
    start = datetime(2026, 1, 31, tzinfo=timezone.utc)
    assert add_months(start, 1).day == 28
    assert add_months(start, 12).year == 2027
    assert add_months(datetime(2026, 12, 15, tzinfo=timezone.utc), 2).month == 2


@pytest.mark.parametrize("value,expected", [
    ("0", "0.00"), ("5", "5.00"), ("999.5", "999.50"), ("1000", "1,000.00"), ("12345.6", "12,345.60"),
    ("100000", "1,00,000.00"), ("1234567.89", "12,34,567.89"), ("10000000", "1,00,00,000.00"),
])
def test_indian_digit_grouping(value, expected):
    from app.utils import indian
    assert indian(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("950", "\u20b9950"), ("1000", "\u20b91k"), ("26750", "\u20b926.8k"), ("125000", "\u20b91.25L"), ("10700000", "\u20b91.07Cr"),
])
def test_compact_money_for_chart_labels(value, expected):
    from app.utils import compact_money
    assert compact_money(value) == expected
