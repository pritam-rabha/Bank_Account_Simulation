"""Statement export: CSV (required) and PDF (optional, needs reportlab)."""
import csv
import io
import re
from xml.sax.saxutils import escape

from .utils import format_dt, mask_account

CSV_HEADER = [
    "Account Number (masked)", "Transaction ID", "Transaction Type", "Date and Time",
    "Timestamp (UTC, ISO 8601)", "Direction", "Amount", "Previous Balance", "New Balance",
    "Reference", "Counterparty (masked)", "Description", "Status",
]
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n")


def sanitize_cell(value):
    """Neutralise spreadsheet formula injection.

    Excel / LibreOffice / Sheets treat a cell beginning with ``= + - @`` (or a
    tab / CR) as a formula.  Prefixing a single quote forces text.  The first
    non-blank character is also checked because some apps skip leading spaces.
    """
    text = "" if value is None else str(value)
    stripped = text.lstrip()
    if text.startswith(_FORMULA_TRIGGERS) or (stripped and stripped[0] in _FORMULA_TRIGGERS):
        return "'" + text
    return text


def _row(t, tz, labels):
    return [
        mask_account(t["account_number"]), t["transaction_id"], labels.get(t["type"], t["type"]),
        format_dt(t["timestamp"], tz), t["timestamp"], t["direction"].title(), t["amount"],
        t["previous_balance"], t["new_balance"], t["reference"],
        mask_account(t["counterparty"]) if t.get("counterparty") else "",
        t["description"], t["status"],
    ]


def build_csv(transactions, tz, labels):
    """Return UTF-8 (with BOM, so Excel shows non-ASCII names correctly) CSV bytes."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for t in transactions:
        writer.writerow([sanitize_cell(cell) for cell in _row(t, tz, labels)])
    return buf.getvalue().encode("utf-8-sig")


def pdf_available():
    try:
        import reportlab  # noqa: F401
        return True
    except ImportError:
        return False


def build_pdf(account, transactions, tz, labels, period_text, symbol="INR "):
    """Simple tabular PDF statement (Helvetica has no rupee glyph, so 'INR')."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=14 * mm, title="BankSim Statement",
                            author="BankSim")
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small")
    small.fontSize = 7
    small.leading = 8.5
    story = [
        Paragraph("BankSim \u2014 Account Statement (educational simulation)", styles["Title"]),
        Paragraph(
            f"Account: {escape(mask_account(account['account_number']))} &nbsp;|&nbsp; "
            f"Holder: {escape(account['holder_name'])} &nbsp;|&nbsp; Period: {escape(period_text)}",
            styles["BodyText"],
        ),
        Spacer(1, 4 * mm),
    ]
    head = ["Date and Time", "Transaction ID", "Type", "Amount", "Previous", "New balance",
            "Reference", "Description"]
    data = [head]
    for t in transactions:
        data.append([
            Paragraph(escape(format_dt(t["timestamp"], tz)), small),
            Paragraph(escape(t["transaction_id"]), small),
            Paragraph(escape(labels.get(t["type"], t["type"])), small),
            f"{symbol}{t['amount']}", f"{symbol}{t['previous_balance']}", f"{symbol}{t['new_balance']}",
            Paragraph(escape(t["reference"]), small),
            Paragraph(escape(t["description"]), small),
        ])
    table = Table(data, repeatRows=1,
                  colWidths=[34 * mm, 38 * mm, 28 * mm, 28 * mm, 28 * mm, 30 * mm, 38 * mm, 44 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#14285a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#9aa3c0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f2fb")]),
    ]))
    story += [table, Spacer(1, 5 * mm),
              Paragraph("Fictional educational data. Copyright \u00a9 2026 Pritam Rabha. All rights reserved.", small)]
    doc.build(story)
    return buf.getvalue()


def statement_filename(account_number, date_from, date_to, ext):
    """e.g. BankSim_statement_acct-9012_from-2026-01-01_to-2026-03-31.csv (ASCII only)."""
    last4 = re.sub(r"[^0-9]", "", account_number)[-4:]
    start = date_from.isoformat() if date_from else "start"
    end = date_to.isoformat() if date_to else "latest"
    return f"BankSim_statement_acct-{last4}_from-{start}_to-{end}.{ext}"
