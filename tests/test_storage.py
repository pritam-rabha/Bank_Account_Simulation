"""JSON persistence, atomic commits, locking and seeding."""
import json
import os
import threading

import pytest

from app.errors import StorageError
from app.seed import seed_if_empty
from app.storage import JsonStore
from tests.conftest import build_app
from tests.helpers import PIN_A, balance, make_account


def test_data_persists_across_application_restarts(tmp_path):
    app1 = build_app(tmp_path)
    a = make_account(app1.extensions["bank"], amount="4321.09")["account_number"]
    app1.extensions["bank"].deposit(a, "10.01", "persist me")
    app2 = build_app(tmp_path)  # simulates a restart
    bank2 = app2.extensions["bank"]
    assert balance(bank2, a) == "4331.10"
    assert [t["description"] for t in bank2.query_transactions(a)][0] == "persist me"
    assert bank2.authenticate(a, PIN_A)["account_number"] == a


def test_money_is_stored_as_decimal_strings_and_no_plain_pin(tmp_path):
    app = build_app(tmp_path)
    a = make_account(app.extensions["bank"], amount="1234.50")["account_number"]
    raw = open(app.config["DATA_FILE"], encoding="utf-8").read()
    data = json.loads(raw)
    acct, txn = data["accounts"][a], data["transactions"][0]
    for value in (acct["balance"], txn["amount"], txn["previous_balance"], txn["new_balance"]):
        assert isinstance(value, str)
    assert acct["balance"] == "1234.50"
    assert PIN_A not in raw.replace(a, "")  # the PIN text is nowhere in the file


def test_exception_inside_transaction_discards_changes(tmp_path):
    store = JsonStore(tmp_path / "s.json")
    with store.transaction() as st:
        st["meta"]["next_seq"] = 5
    with pytest.raises(RuntimeError):
        with store.transaction() as st:
            st["meta"]["next_seq"] = 999
            raise RuntimeError("boom")
    assert store.snapshot()["meta"]["next_seq"] == 5


def test_failed_write_keeps_previous_file_intact(tmp_path, monkeypatch):
    store = JsonStore(tmp_path / "s.json")
    with store.transaction() as st:
        st["meta"]["next_seq"] = 7
    good = (tmp_path / "s.json").read_text()

    def boom(*a, **k):
        raise OSError("simulated")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(StorageError):
        with store.transaction() as st:
            st["meta"]["next_seq"] = 8
    monkeypatch.undo()
    assert (tmp_path / "s.json").read_text() == good
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    with store.transaction() as st:  # lock was released; store still usable
        assert st["meta"]["next_seq"] == 7


def test_backup_copy_of_previous_version_is_kept(tmp_path):
    store = JsonStore(tmp_path / "s.json")
    for n in (1, 2, 3):
        with store.transaction() as st:
            st["meta"]["next_seq"] = n
    assert json.loads((tmp_path / "s.json.bak").read_text())["meta"]["next_seq"] == 2


def test_corrupt_or_unsupported_file_raises_clear_error(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    with pytest.raises(StorageError, match="corrupted"):
        JsonStore(path).snapshot()
    path.write_text(json.dumps({"schema_version": 99}))
    with pytest.raises(StorageError, match="unsupported"):
        JsonStore(path).snapshot()


def test_corrupt_file_gives_friendly_500_page(tmp_path):
    app = build_app(tmp_path)
    open(app.config["DATA_FILE"], "w").write("garbage")
    r = app.test_client().get("/healthz")
    assert r.status_code == 500 and b"garbage" not in r.data


def test_nested_transactions_are_refused(tmp_path):
    store = JsonStore(tmp_path / "s.json")
    with store.transaction():
        with pytest.raises(StorageError, match="Nested"):
            with store.transaction():
                pass


def test_file_lock_prevents_lost_updates_between_store_instances(tmp_path):
    path = tmp_path / "s.json"
    JsonStore(path)  # create dir
    def worker():
        store = JsonStore(path)  # separate instance => only the OS file lock serialises
        for _ in range(15):
            with store.transaction() as st:
                st["meta"]["next_seq"] += 1
    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert JsonStore(path).snapshot()["meta"]["next_seq"] == 1 + 60


def test_sample_data_seeds_a_consistent_ledger_once(tmp_path):
    app = build_app(tmp_path)
    bank = app.extensions["bank"]
    assert seed_if_empty(bank, app.config["SAMPLE_DATA_FILE"]) is True
    assert seed_if_empty(bank, app.config["SAMPLE_DATA_FILE"]) is False
    accounts = bank.store.snapshot()["accounts"]
    assert {a["account_type"] for a in accounts.values()} == {"SAVINGS", "CURRENT", "FIXED_DEPOSIT"}
    assert {a["status"] for a in accounts.values()} == {"ACTIVE", "CLOSED"}
    assert bank.audit() == []
    assert bank.authenticate("102026000012", "482916")["holder_name"] == "Aarav Sharma"


def test_seed_on_startup_flag(tmp_path):
    app = build_app(tmp_path, SEED_SAMPLE_DATA=True)
    assert len(app.extensions["bank"].store.snapshot()["accounts"]) == 4
