"""Interest calculator, validation, and crediting interest to accounts."""
from datetime import timedelta
from decimal import Decimal as D

import pytest

from app import interest
from app.errors import RuleError, ValidationError
from tests.helpers import NOW, balance, ledger_count, login, make_account


def calc(p, r, t, unit="years", method="simple", n=1):
    return interest.calculate(D(p), D(r), D(t), unit, method, n)


def test_simple_interest_values():
    r = calc("10000", "8", "2")
    assert r["interest"] == D("1600.00") and r["maturity"] == D("11600.00")
    assert isinstance(r["interest"], D)
    assert calc("10000", "12", "6", "months")["interest"] == D("600.00")
    assert calc("1.00", "0.5", "1")["interest"] == D("0.01")  # 0.005 rounds half-up


def test_compound_interest_values():
    assert calc("10000", "8", "2", method="compound", n=4)["interest"] == D("1716.59")
    assert calc("10000", "12", "1", method="compound", n=12)["interest"] == D("1268.25")
    assert calc("10000", "10", "3", method="compound", n=1)["interest"] == D("3310.00")


def test_compound_exceeds_simple_for_same_inputs():
    assert calc("5000", "7", "5", method="compound", n=12)["interest"] > calc("5000", "7", "5")["interest"]


def test_zero_rate_gives_zero_interest():
    assert calc("5000", "0", "3")["interest"] == D("0.00")


def test_result_documents_method_and_formula():
    r = calc("1000", "5", "1", method="compound", n=4)
    assert "compound" in r["method_label"].lower() and "^" in r["formula"]
    assert calc("1000", "5", "1")["formula"].startswith("I = P")
    assert len(calc("1000", "5", "3")["schedule"]) == 3


def ok_args(**over):
    base = dict(principal_raw="10000", rate_raw="8", time_raw="2", unit_raw="years",
                method_raw="simple", compounding_raw="1")
    base.update(over)
    return base


@pytest.mark.parametrize("override", [
    {"principal_raw": ""}, {"principal_raw": "-100"}, {"principal_raw": "0"}, {"principal_raw": "abc"},
    {"principal_raw": "100.123"}, {"principal_raw": "9999999999"}, {"rate_raw": "-1"}, {"rate_raw": "100.01"},
    {"rate_raw": "x"}, {"rate_raw": ""}, {"rate_raw": "1.23456"}, {"time_raw": "0"}, {"time_raw": "-2"},
    {"time_raw": ""}, {"time_raw": "101"}, {"time_raw": "1.234"}, {"unit_raw": "days"},
    {"method_raw": "magic"}, {"method_raw": "compound", "compounding_raw": "5"},
    {"method_raw": "compound", "compounding_raw": "x"}, {"unit_raw": "months", "time_raw": "1201"},
])
def test_calculator_rejects_invalid_inputs(override):
    with pytest.raises(ValidationError):
        interest.parse_inputs(**ok_args(**override))


def test_calculator_accepts_boundaries():
    p, r, t, unit, method, n = interest.parse_inputs(**ok_args(rate_raw="0", time_raw="100"))
    assert (p, r, t) == (D("10000"), D("0"), D("100")) and method == "simple"
    interest.parse_inputs(**ok_args(rate_raw="100", unit_raw="months", time_raw="1200"))


def test_savings_interest_is_credited_as_a_transaction(bank):
    a = make_account(bank, "SAVINGS", amount="12000")["account_number"]
    res = bank.apply_interest(a, "3", now=NOW)
    t = res["transactions"][0]
    assert t["type"] == "INTEREST_CREDIT" and t["amount"] == "120.00"  # 12000 * 4% * 3/12
    assert (t["previous_balance"], t["new_balance"]) == ("12000.00", "12120.00")
    assert t["reference"].startswith("INT-") and "4.00% p.a." in t["description"]
    assert balance(bank, a) == "12120.00" and bank.audit() == []


def test_estimates_do_not_change_balances_or_ledger(app, bank):
    a = make_account(bank, "SAVINGS", amount="12000")["account_number"]
    before = (balance(bank, a), ledger_count(bank))
    status = bank.interest_status(bank.get_account(a), 6)
    assert status["eligible"] and status["estimate"] == D("240.00")
    c = app.test_client()
    assert c.get("/interest-calculator?calculate=1&principal=12000&rate=4&time_value=1&time_unit=years").status_code == 200
    login(c, a)
    page = c.get(f"/account/{a}/interest?months=6").get_data(as_text=True)
    assert "Estimate" in page and "not yet credited" in page
    assert (balance(bank, a), ledger_count(bank)) == before


@pytest.mark.parametrize("months", ["0", "13", "-1", "x", "", "1.5"])
def test_invalid_months_rejected(bank, months):
    a = make_account(bank)["account_number"]
    with pytest.raises(ValidationError):
        bank.apply_interest(a, months)


def test_current_account_earns_no_interest_by_default(bank):
    a = make_account(bank, "CURRENT")["account_number"]
    assert not bank.interest_status(bank.get_account(a))["eligible"]
    with pytest.raises(RuleError, match="does not earn interest"):
        bank.apply_interest(a, "1")


def test_fixed_deposit_interest_only_after_maturity_and_only_once(bank):
    a = make_account(bank, "FIXED_DEPOSIT", amount="100000", term=12, now=NOW)["account_number"]
    with pytest.raises(RuleError, match="after maturity"):
        bank.apply_interest(a, "1", now=NOW + timedelta(days=100))
    later = NOW + timedelta(days=370)
    res = bank.apply_interest(a, "1", now=later)
    assert res["transactions"][0]["amount"] == "6500.00"  # 100000 * 6.5% * 12/12
    assert balance(bank, a) == "106500.00"
    with pytest.raises(RuleError, match="already been credited"):
        bank.apply_interest(a, "1", now=later)


def test_interest_credit_is_idempotent(bank):
    a = make_account(bank, amount="12000")["account_number"]
    key = "interest-key-000000001"
    bank.apply_interest(a, "1", key)
    assert bank.apply_interest(a, "1", key)["duplicate"]
    assert balance(bank, a) == "12040.00"


def test_web_interest_credit_flow_and_invalid_calculator_input(app, bank):
    from tests.helpers import post
    a = make_account(bank, amount="12000")["account_number"]
    c = app.test_client()
    login(c, a)
    r = post(c, f"/account/{a}/interest", {"months": "12"}, token_from="/dashboard")
    assert r.status_code == 302 and balance(bank, a) == "12480.00"
    bad = c.get("/interest-calculator?calculate=1&principal=-5&rate=8&time_value=1&time_unit=years")
    assert bad.status_code == 400 and b"Principal" in bad.data
