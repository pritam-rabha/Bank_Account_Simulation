"""Shared helpers for the BankSim test-suite."""
import re
from datetime import datetime, timezone

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
NOW = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)

PIN_A = "482916"
PIN_B = "7391"


def make_account(bank, kind="SAVINGS", name="Asha Rao", pin=PIN_A, amount=None, term=None, now=None):
    """Create an account through the service layer with sensible defaults."""
    defaults = {"SAVINGS": "10000.00", "CURRENT": "10000.00", "FIXED_DEPOSIT": "50000.00"}
    return bank.create_account(
        name, kind, pin, pin, amount or defaults[kind],
        fd_term_months=term or (12 if kind == "FIXED_DEPOSIT" else None), now=now,
    )


def get_token(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    match = CSRF_RE.search(html)
    assert match, f"no CSRF token found on {path}"
    return match.group(1)


def post(client, path, data=None, token_from="/login", **kwargs):
    """POST with a valid CSRF token fetched automatically."""
    payload = dict(data or {})
    payload.setdefault("csrf_token", get_token(client, token_from))
    return client.post(path, data=payload, **kwargs)


def login(client, number, pin=PIN_A):
    return post(client, "/login", {"account_number": number, "pin": pin})


def balance(bank, number):
    return bank.get_account(number)["balance"]


def ledger_count(bank):
    return len(bank.store.snapshot()["transactions"])
