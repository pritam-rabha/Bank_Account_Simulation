"""Deposits, withdrawals, ledger records, history filters, idempotency, concurrency."""
import threading
from datetime import timedelta
from decimal import Decimal

import pytest

from app.errors import InsufficientFundsError, ValidationError
from app.utils import parse_iso
from tests.helpers import NOW, balance, ledger_count, login, make_account, post

REQUIRED = {"transaction_id", "type", "account_number", "amount", "previous_balance", "new_balance",
            "timestamp", "status", "description", "reference"}


def test_deposit_and_withdrawal_update_balance_and_ledger(bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    dep = bank.deposit(a, "250.75", "salary", now=NOW)["transactions"][0]
    wd = bank.withdraw(a, "100.25", now=NOW + timedelta(minutes=1))["transactions"][0]
    assert balance(bank, a) == "1150.50"
    assert (dep["previous_balance"], dep["new_balance"]) == ("1000.00", "1250.75")
    assert (wd["previous_balance"], wd["new_balance"]) == ("1250.75", "1150.50")
    assert dep["description"] == "salary" and wd["description"] == "Withdrawal"
    assert REQUIRED <= set(dep) and dep["status"] == "COMPLETED"
    assert dep["reference"].startswith("DEP-") and wd["reference"].startswith("WDL-")
    assert bank.audit() == []


def test_timestamps_are_consistent_utc_iso(bank):
    a = make_account(bank, now=NOW)["account_number"]
    for t in bank.query_transactions(a):
        ts = parse_iso(t["timestamp"])
        assert ts.tzinfo is not None and ts.utcoffset() == timedelta(0)


def test_transaction_ids_are_unique(bank):
    a = make_account(bank, "CURRENT")["account_number"]
    for _ in range(25):
        bank.deposit(a, "1")
    ids = [t["transaction_id"] for t in bank.query_transactions(a)]
    assert len(ids) == len(set(ids)) == 26


def test_failed_operations_change_nothing(bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    before = (balance(bank, a), ledger_count(bank))
    with pytest.raises(InsufficientFundsError, match="Insufficient balance"):
        bank.withdraw(a, "1000.01")
    for bad in ("0", "-5", "abc", "", "1.234", "1e2", "9999999.00"):
        with pytest.raises(ValidationError):
            bank.deposit(a, bad)
        with pytest.raises(ValidationError):
            bank.withdraw(a, bad)
    assert (balance(bank, a), ledger_count(bank)) == before


def test_unknown_account_rejected(bank):
    from app.errors import NotFoundError
    with pytest.raises(NotFoundError):
        bank.deposit("999999999999", "10")


def _seed_history(bank):
    a = make_account(bank, "CURRENT", amount="1000", now=NOW)["account_number"]
    bank.deposit(a, "10", now=NOW + timedelta(days=1, hours=1))
    bank.withdraw(a, "5", now=NOW + timedelta(days=2, hours=1))
    bank.deposit(a, "20", now=NOW + timedelta(days=3, hours=1))
    return a


def test_history_newest_first_and_oldest_first(bank):
    a = _seed_history(bank)
    newest = [t["type"] for t in bank.query_transactions(a)]
    assert newest == ["DEPOSIT", "WITHDRAWAL", "DEPOSIT", "OPENING_DEPOSIT"]
    oldest = [t["type"] for t in bank.query_transactions(a, newest_first=False)]
    assert oldest == newest[::-1]


def test_history_filter_by_type(bank):
    a = _seed_history(bank)
    rows = bank.query_transactions(a, types={"DEPOSIT"})
    assert len(rows) == 2 and all(t["type"] == "DEPOSIT" for t in rows)
    assert len(bank.query_transactions(a, types={"WITHDRAWAL"})) == 1


def test_history_filter_by_date_is_inclusive(bank):
    from datetime import date
    a = _seed_history(bank)
    start = date(2026, 6, 16)  # day of the first deposit (UTC 11:00 = 16:30 IST)
    rows = bank.query_transactions(a, date_from=start, date_to=date(2026, 6, 17))
    assert [t["type"] for t in rows] == ["WITHDRAWAL", "DEPOSIT"]
    assert len(bank.query_transactions(a, date_to=date(2026, 6, 15))) == 1
    assert bank.query_transactions(a, date_from=date(2026, 7, 1)) == []


def test_history_is_account_specific(bank):
    a = make_account(bank, name="Asha Rao")["account_number"]
    b = make_account(bank, "CURRENT", name="Ravi Kumar")["account_number"]
    bank.deposit(a, "10")
    assert len(bank.query_transactions(a)) == 2 and len(bank.query_transactions(b)) == 1
    assert len(bank.query_transactions(None)) == 3  # complete ledger (internal use)


def test_pagination(bank):
    a = make_account(bank, "CURRENT")["account_number"]
    for _ in range(24):
        bank.deposit(a, "1")
    rows = bank.query_transactions(a)
    p1, p3, p9 = (bank.paginate(rows, n, 10) for n in (1, 3, 9))
    assert p1["total"] == 25 and p1["pages"] == 3 and len(p1["items"]) == 10 and not p1["has_prev"]
    assert len(p3["items"]) == 5 and not p3["has_next"]
    assert p9["page"] == 3  # out-of-range page is clamped
    assert bank.paginate([], 1, 10)["pages"] == 1


def test_parse_filters_reports_invalid_values(bank):
    types, d_from, d_to, sort, errors = bank.parse_filters(
        {"type": "NOPE", "date_from": "2026-02-30", "date_to": "bad", "sort": "sideways"})
    assert set(errors) == {"type", "date_from", "date_to", "sort"}
    *_, errors = bank.parse_filters({"date_from": "2026-05-02", "date_to": "2026-05-01"})
    assert "date_to" in errors
    *_, errors = bank.parse_filters({"type": "DEPOSIT", "date_from": "2026-01-01", "sort": "oldest"})
    assert errors == {}


def test_duplicate_submission_is_applied_once(bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    key = "k" * 20
    first = bank.deposit(a, "100", idempotency_key=key)
    second = bank.deposit(a, "100", idempotency_key=key)
    assert not first["duplicate"] and second["duplicate"]
    assert second["reference"] == first["reference"]
    assert balance(bank, a) == "1100.00"
    bank.withdraw(a, "100", idempotency_key=key)  # different operation, same key text: also once
    assert bank.withdraw(a, "100", idempotency_key=key)["duplicate"]
    assert balance(bank, a) == "1000.00"


def test_concurrent_deposits_lose_no_updates(bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    threads = [threading.Thread(target=lambda: bank.deposit(a, "1.00")) for _ in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert balance(bank, a) == "1030.00" and bank.audit() == []


def test_concurrent_withdrawals_never_overdraw(bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    results = []

    def attempt():
        try:
            bank.withdraw(a, "300")
            results.append("ok")
        except InsufficientFundsError:
            results.append("denied")

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 3 and results.count("denied") == 7
    assert balance(bank, a) == "100.00" and bank.audit() == []


def test_web_deposit_withdraw_flow_and_error_messages(app, bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    c = app.test_client()
    login(c, a)
    ok = post(c, f"/account/{a}/deposit", {"amount": "250.00", "description": "gift"}, token_from="/dashboard")
    assert ok.status_code == 302 and balance(bank, a) == "1250.00"
    assert b"Deposit successful" in c.get("/dashboard").data
    too_much = post(c, f"/account/{a}/withdraw", {"amount": "99999"}, token_from="/dashboard")
    assert too_much.status_code == 422 and b"Insufficient balance" in too_much.data
    bad = post(c, f"/account/{a}/deposit", {"amount": "12.345"}, token_from="/dashboard")
    assert bad.status_code == 400 and b"at most 2 decimal places" in bad.data
    assert balance(bank, a) == "1250.00"


def test_web_double_submit_with_same_key_is_idempotent(app, bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    c = app.test_client()
    login(c, a)
    data = {"amount": "40", "idempotency_key": "double-submit-key-0001"}
    post(c, f"/account/{a}/deposit", data, token_from="/dashboard")
    post(c, f"/account/{a}/deposit", data, token_from="/dashboard")
    assert balance(bank, a) == "1040.00"


def test_history_page_filters_and_pagination(app, bank):
    a = make_account(bank, "CURRENT")["account_number"]
    for _ in range(14):
        bank.deposit(a, "1", "pagetest")
    c = app.test_client()
    login(c, a)
    p1 = c.get(f"/account/{a}/transactions").get_data(as_text=True)
    assert "Page 1 of 2" in p1 and "15 transactions found" in p1
    assert "Page 2 of 2" in c.get(f"/account/{a}/transactions?page=2").get_data(as_text=True)
    only = c.get(f"/account/{a}/transactions?type=OPENING_DEPOSIT").get_data(as_text=True)
    assert "1 transaction found" in only
    bad = c.get(f"/account/{a}/transactions?date_from=2026-13-45")
    assert bad.status_code == 400 and b"not a real calendar date" in bad.data
    assert c.get(f"/account/{a}/transactions?page=abc").status_code == 400
    api = c.get(f"/api/account/{a}/transactions?type=DEPOSIT").get_json()
    assert api["total"] == 14 and "pin_hash" not in str(api)
