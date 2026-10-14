"""PIN hashing, verification, throttling, sessions and authorisation."""
import json
from datetime import timedelta

import pytest

from app import security
from app.errors import AuthError, LockedError
from tests.conftest import build_app
from tests.helpers import NOW, PIN_A, PIN_B, get_token, login, make_account, post


def test_pin_is_hashed_never_plain(bank, app):
    acct = make_account(bank, pin=PIN_A)
    raw = open(app.config["DATA_FILE"], encoding="utf-8").read()
    stored = json.loads(raw)["accounts"][acct["account_number"]]["pin_hash"]
    assert stored.startswith("scrypt:") and PIN_A not in stored
    assert f'"{PIN_A}"' not in raw


def test_hash_verification_and_salting():
    h1, h2 = security.hash_pin("482916"), security.hash_pin("482916")
    assert h1 != h2  # unique salts
    assert security.verify_pin("482916", h1) and not security.verify_pin("482917", h1)
    assert not security.verify_pin("482916", "garbage-hash")
    assert not security.verify_pin("482916", None)


def test_pepper_is_part_of_verification():
    h = security.hash_pin("482916", "pepper-one")
    assert security.verify_pin("482916", h, "pepper-one")
    assert not security.verify_pin("482916", h, "pepper-two")
    assert not security.verify_pin("482916", h, "")


def test_authenticate_success_and_failure(bank):
    a = make_account(bank)["account_number"]
    assert bank.authenticate(a, PIN_A)["account_number"] == a
    with pytest.raises(AuthError) as bad:
        bank.authenticate(a, "000999")
    with pytest.raises(AuthError) as unknown:
        bank.authenticate("999999999999", PIN_A)
    assert bad.value.message == unknown.value.message  # no account enumeration


def test_failed_login_throttling_and_lock_expiry(bank):
    a = make_account(bank)["account_number"]
    for _ in range(4):
        with pytest.raises(AuthError) as exc:
            bank.authenticate(a, "000999", now=NOW)
        assert not isinstance(exc.value, LockedError)
    with pytest.raises(LockedError):  # 5th failure triggers the lock
        bank.authenticate(a, "000999", now=NOW)
    with pytest.raises(LockedError) as locked:  # even the right PIN is refused
        bank.authenticate(a, PIN_A, now=NOW + timedelta(minutes=5))
    assert locked.value.retry_after > 0
    assert bank.authenticate(a, PIN_A, now=NOW + timedelta(minutes=16))["account_number"] == a


def test_lock_survives_restart(tmp_path):
    app1 = build_app(tmp_path)
    a = make_account(app1.extensions["bank"])["account_number"]
    for _ in range(5):
        with pytest.raises(AuthError):
            app1.extensions["bank"].authenticate(a, "000999", now=NOW)
    app2 = build_app(tmp_path)  # new process, same data file
    with pytest.raises(LockedError):
        app2.extensions["bank"].authenticate(a, PIN_A, now=NOW + timedelta(minutes=1))


def test_success_resets_failure_counter(bank):
    a = make_account(bank)["account_number"]
    for _ in range(3):
        with pytest.raises(AuthError):
            bank.authenticate(a, "000999", now=NOW)
    bank.authenticate(a, PIN_A, now=NOW)
    for _ in range(4):  # would lock if the counter had not been reset
        with pytest.raises(AuthError) as exc:
            bank.authenticate(a, "000999", now=NOW)
        assert not isinstance(exc.value, LockedError)


def test_ip_throttle_blocks_guessing_across_unknown_accounts(tmp_path):
    bank = build_app(tmp_path, IP_MAX_FAILED_ATTEMPTS=3).extensions["bank"]
    a = make_account(bank)["account_number"]
    for n in ("100000000001", "100000000002"):
        with pytest.raises(AuthError):
            bank.authenticate(n, "000999", ip="9.9.9.9", now=NOW)
    with pytest.raises(LockedError):
        bank.authenticate("100000000003", "000999", ip="9.9.9.9", now=NOW)
    with pytest.raises(LockedError):
        bank.authenticate(a, PIN_A, ip="9.9.9.9", now=NOW)
    assert bank.authenticate(a, PIN_A, ip="1.1.1.1", now=NOW)  # other clients unaffected


def test_web_login_logout_and_session_flags(client, bank):
    a = make_account(bank)["account_number"]
    resp = login(client, a)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/dashboard")
    cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert client.get("/dashboard").status_code == 200
    assert client.get("/logout").status_code == 405
    out = post(client, "/logout", token_from="/dashboard")
    assert out.status_code == 302
    assert client.get("/dashboard").status_code == 302  # session is gone


def test_web_wrong_pin_and_lockout_status_codes(client, bank):
    a = make_account(bank)["account_number"]
    assert login(client, a, "000999").status_code == 401
    for _ in range(3):
        login(client, a, "000999")
    locked = login(client, a, "000999")
    assert locked.status_code == 429 and "Retry-After" in locked.headers
    assert b"temporarily locked" in locked.data
    assert login(client, a, PIN_A).status_code == 429  # correct PIN refused while locked


def test_login_next_parameter_blocks_open_redirects(client, bank):
    a = make_account(bank)["account_number"]
    resp = post(client, "/login?next=https://evil.example/", {"account_number": a, "pin": PIN_A, "next": "https://evil.example/"})
    assert resp.headers["Location"].endswith("/dashboard")
    client.get("/dashboard")
    c2 = client.application.test_client()
    resp = post(c2, "/login", {"account_number": a, "pin": PIN_A, "next": "//evil.example"})
    assert resp.headers["Location"].endswith("/dashboard")


def test_unauthenticated_access_redirects_to_login(client, bank):
    a = make_account(bank)["account_number"]
    for path in (f"/account/{a}", f"/account/{a}/transactions", f"/account/{a}/export?format=csv", "/dashboard"):
        resp = client.get(path)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert client.get(f"/api/account/{a}").status_code == 401


def test_cannot_access_another_accounts_data_by_changing_url(app, bank):
    a = make_account(bank, name="Asha Rao")["account_number"]
    b = make_account(bank, "CURRENT", name="Ravi Kumar", pin=PIN_B)["account_number"]
    bank.deposit(b, "123.45", "private note")
    c = app.test_client()
    login(c, a)
    for path in (f"/account/{b}", f"/account/{b}/transactions", f"/account/{b}/deposit",
                 f"/account/{b}/withdraw", f"/account/{b}/transfer", f"/account/{b}/interest",
                 f"/account/{b}/export?format=csv", f"/account/{b}/settings", f"/api/account/{b}",
                 f"/api/account/{b}/transactions"):
        assert c.get(path).status_code == 403, path
    unknown = "999999999999"
    assert c.get(f"/account/{unknown}").status_code == 403  # same answer: no enumeration
    before = bank.get_account(b)["balance"]
    assert post(c, f"/account/{b}/deposit", {"amount": "50"}, token_from="/dashboard").status_code == 403
    assert post(c, f"/account/{b}/withdraw", {"amount": "50"}, token_from="/dashboard").status_code == 403
    assert bank.get_account(b)["balance"] == before


def test_pin_hash_never_appears_in_pages_or_api(app, bank):
    a = make_account(bank)["account_number"]
    c = app.test_client()
    login(c, a)
    for path in ("/dashboard", f"/account/{a}", f"/account/{a}/settings", f"/account/{a}/transactions",
                 f"/api/account/{a}", f"/api/account/{a}/transactions", f"/account/{a}/export?format=csv"):
        body = c.get(path).get_data(as_text=True)
        assert "scrypt:" not in body and "pin_hash" not in body, path


def test_changing_pin_ends_other_sessions(app, bank):
    a = make_account(bank)["account_number"]
    c1, c2 = app.test_client(), app.test_client()
    login(c1, a)
    from datetime import timezone
    import time
    time.sleep(1.1)  # session timestamps have 1-second resolution
    login(c2, a)
    time.sleep(1.1)
    resp = post(c2, f"/account/{a}/settings/pin",
                {"current_pin": PIN_A, "new_pin": "905137", "confirm_pin": "905137"}, token_from="/dashboard")
    assert resp.status_code == 302
    assert c2.get("/dashboard").status_code == 200  # the session that changed it stays valid
    assert c1.get("/dashboard").status_code == 302  # the older session is logged out
    with pytest.raises(AuthError):
        bank.authenticate(a, PIN_A)
    assert bank.authenticate(a, "905137")


def test_change_pin_wrong_current_pin_rejected(app, bank):
    a = make_account(bank)["account_number"]
    c = app.test_client()
    login(c, a)
    resp = post(c, f"/account/{a}/settings/pin",
                {"current_pin": "000999", "new_pin": "905137", "confirm_pin": "905137"}, token_from="/dashboard")
    assert resp.status_code == 401
    assert bank.authenticate(a, PIN_A)
