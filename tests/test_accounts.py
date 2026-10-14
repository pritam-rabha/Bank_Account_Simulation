"""All account types, their rules, and account creation."""
from datetime import timedelta
from decimal import Decimal

import pytest

from app.errors import (AccountStatusError, InsufficientFundsError, RuleError, ValidationError)
from app.utils import is_valid_account_number, parse_iso
from tests.helpers import NOW, balance, make_account


@pytest.mark.parametrize("kind,prefix", [("SAVINGS", "10"), ("CURRENT", "20"), ("FIXED_DEPOSIT", "30")])
def test_each_account_type_can_be_created_with_required_fields(bank, kind, prefix):
    acct = make_account(bank, kind, now=NOW)
    assert is_valid_account_number(acct["account_number"])
    assert acct["account_number"].startswith(prefix)
    assert acct["holder_name"] == "Asha Rao"
    assert acct["account_type"] == kind
    assert acct["status"] == "ACTIVE"
    assert parse_iso(acct["opened_at"]) == NOW
    assert Decimal(acct["balance"]) > 0
    assert "pin_hash" not in acct


def test_account_numbers_are_unique(bank):
    numbers = {make_account(bank, "CURRENT", name=f"User {c}")["account_number"] for c in "ABCDEFGHIJKLMNOPQRST"}
    assert len(numbers) == 20


def test_opening_deposit_is_recorded_as_first_transaction(bank):
    acct = make_account(bank, amount="2500.50")
    txns = bank.query_transactions(acct["account_number"])
    assert len(txns) == 1
    assert txns[0]["type"] == "OPENING_DEPOSIT"
    assert txns[0]["previous_balance"] == "0.00" and txns[0]["new_balance"] == "2500.50"


@pytest.mark.parametrize("kind,too_small", [("SAVINGS", "499.99"), ("CURRENT", "999.99"), ("FIXED_DEPOSIT", "999.99")])
def test_minimum_opening_deposits(bank, kind, too_small):
    with pytest.raises(ValidationError):
        make_account(bank, kind, amount=too_small)
    assert bank.store.snapshot()["accounts"] == {}


def test_invalid_account_type_and_fd_term_rejected(bank):
    with pytest.raises(ValidationError):
        bank.create_account("Asha Rao", "PLATINUM", "482916", "482916", "5000")
    with pytest.raises(ValidationError):
        bank.create_account("Asha Rao", "FIXED_DEPOSIT", "482916", "482916", "5000", fd_term_months="7")
    with pytest.raises(ValidationError):
        bank.create_account("Asha Rao", "FIXED_DEPOSIT", "482916", "482916", "5000")


def test_savings_minimum_balance_is_enforced(bank):
    a = make_account(bank, "SAVINGS", amount="1000")["account_number"]
    with pytest.raises(InsufficientFundsError):
        bank.withdraw(a, "500.01")
    assert balance(bank, a) == "1000.00"
    bank.withdraw(a, "500.00")  # exactly down to the minimum balance
    assert balance(bank, a) == "500.00"


def test_current_account_has_no_overdraft_but_can_reach_zero(bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    with pytest.raises(InsufficientFundsError):
        bank.withdraw(a, "1000.01")
    bank.withdraw(a, "1000.00")
    assert balance(bank, a) == "0.00"


def test_fixed_deposit_rules(bank):
    acct = make_account(bank, "FIXED_DEPOSIT", amount="50000", term=12, now=NOW)
    a = acct["account_number"]
    assert acct["interest_rate"] == "6.50" and acct["fd_term_months"] == 12
    assert parse_iso(acct["fd_maturity_date"]).year == 2027
    with pytest.raises(RuleError, match="does not accept"):
        bank.deposit(a, "100")
    with pytest.raises(RuleError, match="locked until maturity"):
        bank.withdraw(a, "100", now=NOW + timedelta(days=30))
    # after maturity withdrawals are permitted
    bank.withdraw(a, "100", now=NOW + timedelta(days=400))
    assert balance(bank, a) == "49900.00"


def test_closed_account_blocks_everything(bank):
    a = make_account(bank)["account_number"]
    bank.close_account(a, "482916")
    for call in (lambda: bank.deposit(a, "10"), lambda: bank.withdraw(a, "10"),
                 lambda: bank.apply_interest(a, "1")):
        with pytest.raises(AccountStatusError):
            call()


def test_close_account_pays_out_balance_and_requires_correct_pin(bank):
    a = make_account(bank, amount="5000")["account_number"]
    with pytest.raises(Exception):
        bank.close_account(a, "000999")
    assert bank.get_account(a)["status"] == "ACTIVE"
    result = bank.close_account(a, "482916")
    assert result["payout"] == Decimal("5000.00")
    acct = bank.get_account(a)
    assert acct["status"] == "CLOSED" and acct["balance"] == "0.00"
    assert bank.query_transactions(a)[0]["description"] == "Account closure payout"


def test_fixed_deposit_cannot_be_closed_before_maturity(bank):
    a = make_account(bank, "FIXED_DEPOSIT", now=NOW)["account_number"]
    with pytest.raises(RuleError):
        bank.close_account(a, "482916", now=NOW + timedelta(days=10))


def test_configurable_rates_are_applied(tmp_path):
    from tests.conftest import build_app
    from decimal import Decimal as D
    app = build_app(tmp_path, SAVINGS_INTEREST_RATE=D("5.25"), CURRENT_INTEREST_RATE=D("1.00"))
    bank = app.extensions["bank"]
    assert make_account(bank, "SAVINGS")["interest_rate"] == "5.25"
    assert make_account(bank, "CURRENT", name="Ravi Kumar")["interest_rate"] == "1.00"
