"""CSRF protection on every state-changing request."""
from tests.helpers import PIN_A, balance, get_token, login, make_account, post


def test_post_without_token_is_rejected(client, bank):
    a = make_account(bank)["account_number"]
    r = client.post("/login", data={"account_number": a, "pin": PIN_A})
    assert r.status_code == 400 and b"Security check failed" in r.data


def test_post_with_wrong_token_is_rejected(client, bank):
    a = make_account(bank)["account_number"]
    get_token(client)
    r = client.post("/login", data={"account_number": a, "pin": PIN_A, "csrf_token": "forged"})
    assert r.status_code == 400
    assert client.get("/dashboard").status_code == 302  # not logged in


def test_state_changing_routes_all_require_a_token(app, bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    b = make_account(bank, "CURRENT", name="Ravi Kumar", pin="7391", amount="1000")["account_number"]
    c = app.test_client()
    login(c, a)
    before = balance(bank, a)
    for path, data in [
        (f"/account/{a}/deposit", {"amount": "10"}), (f"/account/{a}/withdraw", {"amount": "10"}),
        (f"/account/{a}/transfer", {"destination_account": b, "amount": "10", "pin": PIN_A}),
        (f"/account/{a}/interest", {"months": "1"}),
        (f"/account/{a}/settings/pin", {"current_pin": PIN_A, "new_pin": "905137", "confirm_pin": "905137"}),
        (f"/account/{a}/settings/close", {"confirm_text": "CLOSE", "pin": PIN_A}),
        ("/logout", {}), ("/register", {"holder_name": "X Y"}),
    ]:
        assert c.post(path, data=data).status_code == 400, path
    assert balance(bank, a) == before and bank.get_account(a)["status"] == "ACTIVE"
    assert c.get("/dashboard").status_code == 200  # logout without token did not log out


def test_valid_token_is_accepted_via_form_and_header(app, bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    c = app.test_client()
    login(c, a)
    token = get_token(c, "/dashboard")
    assert c.post(f"/account/{a}/deposit", data={"amount": "5", "csrf_token": token}).status_code == 302
    assert c.post(f"/account/{a}/deposit", data={"amount": "5"}, headers={"X-CSRFToken": token}).status_code == 302
    assert balance(bank, a) == "1010.00"


def test_token_from_another_session_is_rejected(app, bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    c1, c2 = app.test_client(), app.test_client()
    login(c1, a)
    other_token = get_token(c2)
    assert c1.post(f"/account/{a}/deposit", data={"amount": "5", "csrf_token": other_token}).status_code == 400
    assert get_token(c2) != get_token(c1, "/dashboard")


def test_get_requests_do_not_need_tokens(client):
    for path in ("/", "/about", "/login", "/register", "/interest-calculator"):
        assert client.get(path).status_code == 200
