"""CSV/PDF export: authorisation, filters, masking, formula-injection protection."""
import csv
import io
from datetime import timedelta

import pytest

from app import exporter
from app.utils import get_display_tz
from tests.helpers import NOW, PIN_B, login, make_account


def parse_csv(response):
    text = response.data.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


@pytest.fixture
def logged_in(app, bank):
    a = make_account(bank, "CURRENT", amount="1000", now=NOW)["account_number"]
    bank.deposit(a, "50", "salary", now=NOW + timedelta(days=1))
    bank.withdraw(a, "20", "snacks", now=NOW + timedelta(days=2))
    c = app.test_client()
    login(c, a)
    return c, a


def test_csv_has_required_columns_masked_account_and_all_rows(logged_in):
    c, a = logged_in
    r = c.get(f"/account/{a}/export?format=csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert r.data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM for Excel
    rows = parse_csv(r)
    assert len(rows) == 3
    for col in ("Account Number (masked)", "Transaction ID", "Transaction Type", "Date and Time",
                "Amount", "Previous Balance", "New Balance", "Reference"):
        assert col in rows[0]
    assert all(row["Account Number (masked)"] == "XXXXXXXX" + a[-4:] for row in rows)
    assert a not in r.data.decode("utf-8-sig")  # full number never appears
    newest = rows[0]
    assert newest["Transaction Type"] == "Withdrawal" and newest["Amount"] == "20.00"
    assert (newest["Previous Balance"], newest["New Balance"]) == ("1050.00", "1030.00")
    assert newest["Reference"].startswith("WDL-")


def test_csv_filename_is_meaningful_and_ascii(logged_in):
    c, a = logged_in
    r = c.get(f"/account/{a}/export?format=csv&date_from=2026-06-01&date_to=2026-06-30")
    disp = r.headers["Content-Disposition"]
    assert disp.startswith("attachment;") and f"acct-{a[-4:]}" in disp
    assert "from-2026-06-01_to-2026-06-30.csv" in disp and a not in disp


def test_csv_respects_type_date_and_sort_filters(logged_in):
    c, a = logged_in
    deposits = parse_csv(c.get(f"/account/{a}/export?format=csv&type=DEPOSIT"))
    assert [r["Transaction Type"] for r in deposits] == ["Deposit"]
    ranged = parse_csv(c.get(f"/account/{a}/export?format=csv&date_from=2026-06-16&date_to=2026-06-16"))
    assert [r["Transaction Type"] for r in ranged] == ["Deposit"]
    oldest = parse_csv(c.get(f"/account/{a}/export?format=csv&sort=oldest"))
    assert oldest[0]["Transaction Type"] == "Opening deposit"


@pytest.mark.parametrize("query", [
    "format=xlsx", "format=csv&type=HACK", "format=csv&date_from=2026-02-30", "format=csv&date_from=junk",
    "format=csv&date_from=2026-06-30&date_to=2026-06-01", "format=csv&sort=zigzag",
])
def test_invalid_export_filters_are_rejected(logged_in, query):
    c, a = logged_in
    r = c.get(f"/account/{a}/export?{query}")
    assert r.status_code == 400 and r.mimetype == "text/html"


def test_empty_export_shows_helpful_message_instead_of_file(logged_in):
    c, a = logged_in
    r = c.get(f"/account/{a}/export?format=csv&date_from=2030-01-01")
    assert r.status_code == 200 and r.mimetype == "text/html"
    assert b"Nothing to export" in r.data and "Content-Disposition" not in r.headers


def test_export_form_page_renders_without_download(logged_in):
    c, a = logged_in
    r = c.get(f"/account/{a}/export")
    assert r.status_code == 200 and b"Download statement" in r.data


def test_export_contains_only_own_records_and_blocks_other_accounts(app, bank):
    a = make_account(bank, name="Asha Rao")["account_number"]
    b = make_account(bank, "CURRENT", name="Ravi Kumar", pin=PIN_B)["account_number"]
    bank.deposit(b, "77.77", "ravi-only-note")
    c = app.test_client()
    login(c, a)
    body = c.get(f"/account/{a}/export?format=csv").data.decode("utf-8-sig")
    assert "ravi-only-note" not in body and "77.77" not in body
    assert c.get(f"/account/{b}/export?format=csv").status_code == 403
    assert app.test_client().get(f"/account/{a}/export?format=csv").status_code == 302


@pytest.mark.parametrize("payload", [
    "=HYPERLINK(\"http://evil.test\",\"x\")", "+1+1", "-2+3", "@SUM(1,1)", "\t=1+1", "\r=1+1", " =1+1",
])
def test_sanitize_cell_neutralises_formula_triggers(payload):
    cleaned = exporter.sanitize_cell(payload)
    assert cleaned.startswith("'") and cleaned == "'" + payload


@pytest.mark.parametrize("safe", ["salary", "Rent 2026", "", "12.50", "O'Neil", "a=b"])
def test_sanitize_cell_leaves_normal_text_alone(safe):
    assert exporter.sanitize_cell(safe) == safe


def test_formula_injection_is_blocked_end_to_end(app, bank):
    a = make_account(bank, "CURRENT", amount="1000")["account_number"]
    bank.deposit(a, "10", "=cmd|'/C calc'!A0")
    bank.deposit(a, "11", "@SUM(A1:A9)")
    bank.deposit(a, "12", "+1+1")
    c = app.test_client()
    login(c, a)
    rows = parse_csv(c.get(f"/account/{a}/export?format=csv"))
    descriptions = {r["Description"] for r in rows}
    assert {"'=cmd|'/C calc'!A0", "'@SUM(A1:A9)", "'+1+1"} <= descriptions
    for row in rows:
        for cell in row.values():
            assert not cell.startswith(("=", "+", "@")), cell
    assert [r["Amount"] for r in rows if r["Amount"].startswith("-")] == []


def test_build_csv_uses_crlf_and_quotes_commas(bank):
    a = make_account(bank, "CURRENT")["account_number"]
    bank.deposit(a, "5", 'note, with "quotes"')
    from app.services import TXN_TYPES
    data = exporter.build_csv(bank.query_transactions(a), get_display_tz("Asia/Kolkata"), TXN_TYPES).decode("utf-8-sig")
    assert "\r\n" in data
    assert 'note, with ""quotes""' in data
    assert next(csv.DictReader(io.StringIO(data)))["Description"] == 'note, with "quotes"'


def test_pdf_export_when_reportlab_is_installed(logged_in):
    if not exporter.pdf_available():
        pytest.skip("reportlab not installed")
    c, a = logged_in
    r = c.get(f"/account/{a}/export?format=pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data.startswith(b"%PDF")
    assert ".pdf" in r.headers["Content-Disposition"]
