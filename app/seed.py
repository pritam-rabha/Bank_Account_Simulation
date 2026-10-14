"""Load fictional demo accounts from ``data/sample_accounts.json``.

Seeding goes through the same ``Bank`` service methods as normal use, so the
demo ledger obeys every rule.  Timestamps are back-dated relative to *now*.
"""
import json
from datetime import timedelta

from .utils import now_utc


def seed_if_empty(bank, sample_path):
    if bank.store.snapshot()["accounts"]:
        return False
    with open(sample_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    now = now_utc()
    numbers = {}
    for item in spec["accounts"]:
        opened = now - timedelta(days=item["opened_days_ago"])
        acct = bank.create_account(
            item["holder_name"], item["account_type"], item["demo_pin"], item["demo_pin"],
            item["opening_deposit"], fd_term_months=item.get("fd_term_months"), now=opened,
            _fixed_number=item["account_number"],
        )
        numbers[item["key"]] = acct["account_number"]
    # Replay every action in global chronological order so that each account's
    # balance chain agrees with its timestamps (transfers touch two accounts).
    timeline = [(a["days_ago"], item, a) for item in spec["accounts"] for a in item.get("actions", [])]
    timeline.sort(key=lambda row: -row[0])
    for days_ago, item, action in timeline:
        number = numbers[item["key"]]
        when = now - timedelta(days=days_ago)
        kind = action["type"]
        if kind == "deposit":
            bank.deposit(number, action["amount"], action.get("description", ""), now=when)
        elif kind == "withdraw":
            bank.withdraw(number, action["amount"], action.get("description", ""), now=when)
        elif kind == "interest":
            bank.apply_interest(number, str(action.get("months", 1)), now=when)
        elif kind == "transfer":
            bank.transfer(number, numbers[action["to"]], action["amount"], item["demo_pin"],
                          action.get("description", ""), now=when)
        elif kind == "close":
            bank.close_account(number, item["demo_pin"], now=when)
    return True
