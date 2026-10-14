"""Pages, error pages, headers, registration and settings flows."""
import re

import pytest

from tests.helpers import PIN_A, balance, login, make_account, post

FOOTER_1 = "Copyright © 2026 Pritam Rabha. All rights reserved."
FOOTER_2 = "BankSim — MCA Python Programming Project"


@pytest.mark.parametrize("path", ["/", "/about", "/register", "/login", "/interest-calculator"])
def test_public_pages_render_with_footer_and_disclaimer(client, path):
    r = client.get(path)
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert FOOTER_1 in html and FOOTER_2 in html
    assert "Educational simulation" in html


def test_about_page_has_project_details(client):
    html = client.get("/about").get_data(as_text=True)
    for text in ("Pritam Rabha", "MCA, 3rd Semester", "Dr. Pangambam Sendash Singh",
                 "Dr. Hari Singh Gour Vishwavidyalaya", "Educational simulation"):
        assert text in html
    assert "Limitations of JSON storage" in html


def test_logged_in_pages_render(app, bank):
    a = make_account(bank, amount="9000")["account_number"]
    bank.deposit(a, "5")
    c = app.test_client()
    login(c, a)
    for path in ("/dashboard", f"/account/{a}", f"/account/{a}/deposit", f"/account/{a}/withdraw",
                 f"/account/{a}/transfer", f"/account/{a}/interest", f"/account/{a}/transactions",
                 f"/account/{a}/export", f"/account/{a}/settings", "/about", "/interest-calculator"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert FOOTER_1 in r.get_data(as_text=True)


def test_custom_error_pages(app, bank):
    c = app.test_client()
    r = c.get("/no/such/page")
    assert r.status_code == 404 and b"Page not found" in r.data and FOOTER_1.encode() in r.data
    assert c.post("/about").status_code in (400, 405)
    r = c.delete("/about", headers={"X-CSRFToken": "x"})
    assert r.status_code == 400 or r.status_code == 405
    a = make_account(bank)["account_number"]
    c2 = app.test_client()
    login(c2, a)
    r = c2.get("/account/999999999999")
    assert r.status_code == 403 and b"Access denied" in r.data
    assert c2.get("/logout").status_code == 405 and b"Method not allowed" in c2.get("/logout").data


def test_unhandled_exception_shows_generic_500_without_details(app, bank, monkeypatch):
    from app.services import Bank
    monkeypatch.setattr(Bank, "query_transactions", lambda *a, **k: 1 / 0)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    a = make_account(bank)["account_number"]
    c = app.test_client()
    login(c, a)
    r = c.get("/dashboard")
    assert r.status_code == 500 and b"Something went wrong" in r.data
    assert b"ZeroDivisionError" not in r.data and b"Traceback" not in r.data


def test_security_headers_and_no_inline_scripts(client):
    r = client.get("/")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"
    html = client.get("/register").get_data(as_text=True)
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert " style=" not in html and " onclick=" not in html


def test_forms_have_labels_for_every_visible_input(client):
    html = client.get("/login").get_data(as_text=True)
    for ident in re.findall(r'<input[^>]*type="(?:text|password)"[^>]*id="([^"]+)"', html):
        assert f'for="{ident}"' in html


def test_html_escapes_user_supplied_text(app, bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    bank.deposit(a, "5", "<img src=x onerror=alert(1)>")
    c = app.test_client()
    login(c, a)
    html = c.get(f"/account/{a}/transactions").get_data(as_text=True)
    assert "<img src=x" not in html and "&lt;img src=x" in html


def test_registration_flow_logs_in_and_creates_account(client, bank):
    r = post(client, "/register", {"holder_name": "Nisha Patel", "account_type": "FIXED_DEPOSIT",
                                   "opening_deposit": "20000", "fd_term_months": "24",
                                   "pin": "482916", "confirm_pin": "482916"}, token_from="/register")
    assert r.status_code == 302
    html = client.get("/dashboard").get_data(as_text=True)
    assert "Nisha Patel" in html and "Account created" in html
    acct = list(bank.store.snapshot()["accounts"].values())[0]
    assert acct["account_type"] == "FIXED_DEPOSIT" and acct["fd_term_months"] == 24 and acct["interest_rate"] == "6.75"


@pytest.mark.parametrize("data,message", [
    ({"holder_name": "N", "pin": "482916", "confirm_pin": "482916", "opening_deposit": "1000"}, b"between 2 and 60"),
    ({"holder_name": "Nisha", "pin": "1234", "confirm_pin": "1234", "opening_deposit": "1000"}, b"too easy to guess"),
    ({"holder_name": "Nisha", "pin": "482916", "confirm_pin": "482917", "opening_deposit": "1000"}, b"do not match"),
    ({"holder_name": "Nisha", "pin": "482916", "confirm_pin": "482916", "opening_deposit": "10"}, b"at least"),
    ({"holder_name": "Nisha", "pin": "482916", "confirm_pin": "482916", "opening_deposit": "abc"}, b"positive number"),
    ({"holder_name": "Nisha", "pin": "48a916", "confirm_pin": "48a916", "opening_deposit": "1000"}, b"digits"),
])
def test_registration_validation_errors(client, bank, data, message):
    r = post(client, "/register", {"account_type": "SAVINGS", **data}, token_from="/register")
    assert r.status_code == 400 and message in r.data
    assert bank.store.snapshot()["accounts"] == {}
    assert b'value="482916"' not in r.data  # PIN is never echoed back


def test_close_account_flow(app, bank):
    a = make_account(bank, amount="3000")["account_number"]
    c = app.test_client()
    login(c, a)
    r = post(c, f"/account/{a}/settings/close", {"confirm_text": "nope", "pin": PIN_A}, token_from="/dashboard")
    assert r.status_code == 400 and bank.get_account(a)["status"] == "ACTIVE"
    r = post(c, f"/account/{a}/settings/close", {"confirm_text": "CLOSE", "pin": "000999"}, token_from="/dashboard")
    assert r.status_code == 401 and bank.get_account(a)["status"] == "ACTIVE"
    r = post(c, f"/account/{a}/settings/close", {"confirm_text": "CLOSE", "pin": PIN_A}, token_from="/dashboard")
    assert r.status_code == 302 and bank.get_account(a)["status"] == "CLOSED"
    page = c.get(f"/account/{a}/deposit")
    assert b"closed" in page.data
    denied = post(c, f"/account/{a}/deposit", {"amount": "10"}, token_from="/dashboard")
    assert denied.status_code == 422 and balance(bank, a) == "0.00"


def test_demo_credentials_hidden_unless_enabled(tmp_path):
    from tests.conftest import build_app
    assert b"Demo accounts" not in build_app(tmp_path).test_client().get("/login").data
    shown = build_app(tmp_path / "x", SHOW_DEMO_CREDENTIALS=True).test_client().get("/login")
    assert b"Demo accounts" in shown.data


def test_no_page_depends_on_an_external_host(app, bank):
    """Assets are self-hosted: no CDN links, and the CSP only allows 'self'."""
    a = make_account(bank, amount="9000")["account_number"]
    bank.deposit(a, "5")
    c = app.test_client()
    pages = ["/", "/login", "/register", "/about", "/interest-calculator"]
    login(c, a)
    pages += ["/dashboard", f"/account/{a}/transactions", f"/account/{a}/transfer"]
    for path in pages:
        r = c.get(path)
        html = r.get_data(as_text=True)
        assert not re.search(r'(?:src|href)="https?://', html), path
        csp = r.headers["Content-Security-Policy"]
        assert "http" not in csp and "script-src 'self'" in csp and "style-src 'self'" in csp
    assert c.get("/static/vendor/bootstrap/bootstrap.min.css").status_code == 200
    assert c.get("/static/vendor/bootstrap/bootstrap.bundle.min.js").status_code == 200


def test_dashboard_shows_indian_formatted_balance_and_chart(app, bank):
    a = make_account(bank, "CURRENT", amount="1000000.00")["account_number"]
    bank.deposit(a, "234567.50")
    bank.withdraw(a, "34567.50")
    c = app.test_client()
    login(c, a)
    html = c.get("/dashboard").get_data(as_text=True)
    assert "12,00,000.00" in html
    assert '<svg class="chart"' in html and "Balance history" in html


def test_dashboard_without_history_shows_empty_chart_message(app, bank):
    a = make_account(bank)["account_number"]
    c = app.test_client()
    login(c, a)
    html = c.get("/dashboard").get_data(as_text=True)
    assert '<svg class="chart"' not in html and "balance trend will appear here" in html


def test_funds_info_reflects_minimum_balance_and_fd_lock(bank):
    from datetime import timedelta
    from tests.helpers import NOW
    sav = bank.get_account(make_account(bank, "SAVINGS", amount="2000")["account_number"])
    info = bank.funds_info(sav)
    assert str(info["available"]) == "1500.00" and not info["locked"]
    fd = bank.get_account(make_account(bank, "FIXED_DEPOSIT", name="Kabir Singh", pin="905137", now=NOW)["account_number"])
    assert bank.funds_info(fd, NOW)["locked"] and str(bank.funds_info(fd, NOW)["available"]) == "0.00"
    later = bank.funds_info(fd, NOW + timedelta(days=400))
    assert not later["locked"] and str(later["available"]) == "50000.00"
