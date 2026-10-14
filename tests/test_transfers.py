"""Fund transfers: rules, atomicity, locking, and the confirmation page."""
import os
import threading
from decimal import Decimal as D

import pytest

from app.errors import (AccountStatusError, AuthError, InsufficientFundsError, LockedError,
                        NotFoundError, RuleError, StorageError, ValidationError)
from app.services import Bank
from tests.helpers import NOW, PIN_A, PIN_B, balance, ledger_count, login, make_account, post


@pytest.fixture
def pair(bank):
    a = make_account(bank, "CURRENT", name="Asha Rao", pin=PIN_A, amount="5000")["account_number"]
    b = make_account(bank, "SAVINGS", name="Ravi Kumar", pin=PIN_B, amount="2000")["account_number"]
    return a, b


def snapshot(bank, *numbers):
    return tuple(balance(bank, n) for n in numbers) + (ledger_count(bank),)


def test_successful_transfer_moves_money_and_records_both_entries(bank, pair):
    a, b = pair
    res = bank.transfer(a, b, "1250.50", PIN_A, "rent", now=NOW)
    debit, credit = res["transactions"]
    assert balance(bank, a) == "3749.50" and balance(bank, b) == "3250.50"
    assert debit["type"] == "TRANSFER_OUT" and credit["type"] == "TRANSFER_IN"
    assert debit["reference"] == credit["reference"] == res["reference"]
    assert res["reference"].startswith("TRF-")
    assert debit["timestamp"] == credit["timestamp"]
    assert (debit["account_number"], debit["counterparty"]) == (a, b)
    assert (credit["account_number"], credit["counterparty"]) == (b, a)
    assert (debit["previous_balance"], debit["new_balance"]) == ("5000.00", "3749.50")
    assert (credit["previous_balance"], credit["new_balance"]) == ("2000.00", "3250.50")
    assert bank.audit() == []


def test_transfer_references_are_unique(bank, pair):
    a, b = pair
    refs = {bank.transfer(a, b, "1", PIN_A)["reference"] for _ in range(10)}
    assert len(refs) == 10


def test_same_account_transfer_rejected(bank, pair):
    a, _ = pair
    before = snapshot(bank, a)
    with pytest.raises(ValidationError, match="different"):
        bank.transfer(a, a, "10", PIN_A)
    assert snapshot(bank, a) == before


@pytest.mark.parametrize("dest", ["", "123", "abcdefghijkl", "100000000000", "10202600001299"])
def test_invalid_destination_numbers_rejected(bank, pair, dest):
    a, _ = pair
    before = snapshot(bank, a)
    with pytest.raises(ValidationError):
        bank.transfer(a, dest, "10", PIN_A)
    assert snapshot(bank, a) == before


def test_unknown_but_well_formed_destination_rejected(bank, pair):
    a, _ = pair
    with pytest.raises(NotFoundError):
        bank.transfer(a, "102026000012", "10", PIN_A)  # valid check digit, no such account


@pytest.mark.parametrize("amount", ["0", "-10", "abc", "", "1.999", "1e3", "NaN", "99999999.00"])
def test_invalid_amounts_rejected(bank, pair, amount):
    a, b = pair
    before = snapshot(bank, a, b)
    with pytest.raises(ValidationError):
        bank.transfer(a, b, amount, PIN_A)
    assert snapshot(bank, a, b) == before


def test_insufficient_balance_and_minimum_balance(bank, pair):
    a, b = pair
    before = snapshot(bank, a, b)
    with pytest.raises(InsufficientFundsError):
        bank.transfer(a, b, "5000.01", PIN_A)
    assert snapshot(bank, a, b) == before
    with pytest.raises(InsufficientFundsError):  # savings must keep its 500 minimum
        bank.transfer(b, a, "1500.01", PIN_B)
    bank.transfer(b, a, "1500.00", PIN_B)
    assert balance(bank, b) == "500.00"


def test_wrong_pin_blocks_transfer_and_is_throttled(bank, pair):
    a, b = pair
    before = snapshot(bank, a, b)
    for _ in range(4):
        with pytest.raises(AuthError) as exc:
            bank.transfer(a, b, "10", "000999", now=NOW)
        assert not isinstance(exc.value, LockedError)
    with pytest.raises(LockedError):
        bank.transfer(a, b, "10", "000999", now=NOW)
    with pytest.raises(LockedError):
        bank.transfer(a, b, "10", PIN_A, now=NOW)
    assert snapshot(bank, a, b) == before


def test_closed_source_account_rejected(bank, pair):
    a, b = pair
    bank.close_account(a, PIN_A)
    before = snapshot(bank, a, b)
    with pytest.raises(AccountStatusError, match="closed"):
        bank.transfer(a, b, "10", PIN_A)
    assert snapshot(bank, a, b) == before


def test_closed_destination_account_rejected(bank, pair):
    a, b = pair
    bank.close_account(b, PIN_B)
    before = snapshot(bank, a, b)
    with pytest.raises(AccountStatusError, match="Destination account is closed"):
        bank.transfer(a, b, "10", PIN_A)
    assert snapshot(bank, a, b) == before


def test_fixed_deposit_cannot_receive_or_send_before_maturity(bank, pair):
    a, _ = pair
    fd = make_account(bank, "FIXED_DEPOSIT", name="Kabir Singh", pin="905137", now=NOW)["account_number"]
    with pytest.raises(RuleError):
        bank.transfer(a, fd, "10", PIN_A)
    with pytest.raises(RuleError, match="locked"):
        bank.transfer(fd, a, "10", "905137", now=NOW)


# ----------------------------------------------------------------- atomicity
def test_transfer_is_atomic_when_the_credit_step_fails(bank, pair, monkeypatch):
    a, b = pair
    before = snapshot(bank, a, b)
    real_post = Bank._post
    calls = {"n": 0}

    def flaky(self, st, acct, tx_type, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # the debit already succeeded in memory...
            raise RuntimeError("simulated crash while crediting")
        return real_post(self, st, acct, tx_type, *args, **kwargs)

    monkeypatch.setattr(Bank, "_post", flaky)
    with pytest.raises(RuntimeError):
        bank.transfer(a, b, "1000", PIN_A)
    monkeypatch.setattr(Bank, "_post", real_post)
    assert snapshot(bank, a, b) == before  # ...but nothing reached the disk
    assert bank.audit() == []
    bank.transfer(a, b, "1000", PIN_A)  # service is healthy afterwards
    assert balance(bank, a) == "4000.00" and balance(bank, b) == "3000.00"


def test_transfer_is_atomic_when_the_disk_write_fails(bank, pair, monkeypatch):
    a, b = pair
    before = snapshot(bank, a, b)
    data_dir = os.path.dirname(bank.store.path)

    def boom(src, dst):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(StorageError):
        bank.transfer(a, b, "1000", PIN_A)
    monkeypatch.undo()
    assert snapshot(bank, a, b) == before
    assert [f for f in os.listdir(data_dir) if f.endswith(".tmp")] == []  # no stray temp files
    bank.transfer(a, b, "1000", PIN_A)
    assert balance(bank, a) == "4000.00"


def test_duplicate_transfer_submission_is_applied_once(bank, pair):
    a, b = pair
    key = "transfer-key-00000001"
    first = bank.transfer(a, b, "100", PIN_A, idempotency_key=key)
    second = bank.transfer(a, b, "100", PIN_A, idempotency_key=key)
    assert second["duplicate"] and second["reference"] == first["reference"]
    assert balance(bank, a) == "4900.00" and balance(bank, b) == "2100.00"


def test_concurrent_opposing_transfers_conserve_money(bank, pair):
    a, b = pair
    total = D(balance(bank, a)) + D(balance(bank, b))

    def run(src, dst, pin):
        for _ in range(10):
            try:
                bank.transfer(src, dst, "7.25", pin)
            except InsufficientFundsError:
                pass

    threads = [threading.Thread(target=run, args=(a, b, PIN_A)), threading.Thread(target=run, args=(b, a, PIN_B)),
               threading.Thread(target=run, args=(a, b, PIN_A)), threading.Thread(target=run, args=(b, a, PIN_B))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert D(balance(bank, a)) + D(balance(bank, b)) == total
    assert D(balance(bank, a)) >= 0 and D(balance(bank, b)) >= 500
    assert bank.audit() == []


def test_concurrent_transfers_cannot_overdraw_source(bank, pair):
    a, b = pair
    outcomes = []

    def attempt():
        try:
            bank.transfer(a, b, "2000", PIN_A)
            outcomes.append(True)
        except InsufficientFundsError:
            outcomes.append(False)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count(True) == 2 and balance(bank, a) == "1000.00" and balance(bank, b) == "6000.00"


# ----------------------------------------------------------------- web layer
def test_web_transfer_flow_shows_confirmation(app, bank, pair):
    a, b = pair
    c = app.test_client()
    login(c, a, PIN_A)
    resp = post(c, f"/account/{a}/transfer",
                {"destination_account": b, "amount": "300.00", "description": "books", "pin": PIN_A},
                token_from="/dashboard")
    assert resp.status_code == 302 and "/transfer/receipt/TRF-" in resp.headers["Location"]
    page = c.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Transfer confirmed" in page and "TRF-" in page and "XXXXXXXX" in page
    assert b not in page  # the destination number is masked on the receipt
    assert balance(bank, a) == "4700.00" and balance(bank, b) == "2300.00"


def test_web_transfer_wrong_pin_changes_nothing(app, bank, pair):
    a, b = pair
    c = app.test_client()
    login(c, a, PIN_A)
    resp = post(c, f"/account/{a}/transfer", {"destination_account": b, "amount": "300", "pin": "000999"},
                token_from="/dashboard")
    assert resp.status_code == 401 and b"Incorrect PIN" in resp.data
    assert balance(bank, a) == "5000.00"


def test_web_transfer_error_messages(app, bank, pair):
    a, b = pair
    c = app.test_client()
    login(c, a, PIN_A)
    same = post(c, f"/account/{a}/transfer", {"destination_account": a, "amount": "5", "pin": PIN_A}, token_from="/dashboard")
    assert same.status_code == 400 and b"must be different" in same.data
    bank.close_account(b, PIN_B)
    closed = post(c, f"/account/{a}/transfer", {"destination_account": b, "amount": "5", "pin": PIN_A}, token_from="/dashboard")
    assert closed.status_code == 422 and b"closed" in closed.data


def test_receipt_of_another_user_is_forbidden_or_missing(app, bank, pair):
    a, b = pair
    ref = bank.transfer(a, b, "10", PIN_A)["reference"]
    c = app.test_client()
    login(c, b, PIN_B)
    assert c.get(f"/account/{a}/transfer/receipt/{ref}").status_code == 403
    assert c.get(f"/account/{b}/transfer/receipt/{ref}").status_code == 404  # receiver has no debit entry
