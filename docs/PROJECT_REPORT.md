# BankSim — Bank Account Simulation
## MCA Project Report (short)

| | |
|---|---|
| Author | Pritam Rabha |
| Programme / Semester | MCA, 3rd Semester |
| Course | Python Programming |
| Project guide | Dr. Pangambam Sendash Singh |
| Institution | Dr. Hari Singh Gour Vishwavidyalaya, Sagar, Madhya Pradesh |

Copyright © 2026 Pritam Rabha. All rights reserved.

> BankSim is a fictional educational simulation. It does not model any real bank, product or regulation and is not connected to real accounts or payment services.

## 1. Introduction and objectives

Banking software is a good vehicle for practising correctness-critical programming: money must never be created or lost, history must be trustworthy, and access must be controlled. BankSim is a Flask web application that simulates a small bank in order to practise these ideas. Its objectives are the six assignment requirements: multiple account types, timestamped transaction history, interest calculation, secure PIN-based login, atomic fund transfers and statement export — plus automated verification.

## 2. Design

**Layers.** `routes.py` (HTTP, forms, templates) → `services.Bank` (all business rules) → `storage.JsonStore` (persistence and transactions). Helper modules provide validation (`utils`), account rules (`account_types`), interest maths (`interest`), security primitives (`security`) and export (`exporter`). Templates use Jinja2 with a self-hosted Bootstrap 5.3.2 and a custom navy/indigo design system (app-shell layout with sidebar, ledger tables, SVG balance chart, split-screen sign-in). All assets are served locally and the CSP allows only `'self'`.

**Data model (JSON).** One file holds `accounts`, an append-only `transactions` list, a `throttle` table for failed logins, an `idempotency` table and a sequence counter. Money is stored as two-decimal strings and handled as `Decimal`. A ledger entry records ID, type, account, amount, previous balance, new balance, UTC ISO timestamp, status, description, reference and counterparty. Each account's last `new_balance` must equal its stored balance — `Bank.audit()` checks this.

**Key design decisions.**

1. *Atomicity by transaction boundary.* `JsonStore.transaction()` locks (thread + OS file lock), loads fresh state, lets the service mutate it and commits only on a clean exit with a temp-file + `fsync` + `os.replace`. A transfer posts both entries inside one block, so they commit together or not at all. The limits of JSON storage (single host, whole-file rewrite, no WAL) are documented, and PostgreSQL is named as the production alternative.
2. *Decimal everywhere.* Strict parsing rejects signs, exponents, separators, NaN and more than two decimals instead of rounding silently.
3. *Authentication.* scrypt-hashed PINs (optional pepper), failure counters persisted per account and per IP with lockout, signed sessions, CSRF tokens, step-up PIN checks for transfers and settings, and URL-level authorisation (`403` unless the URL's account is the signed-in one).
4. *Estimated vs credited interest.* The calculator and previews never write; only the explicit confirmation posts an `INTEREST_CREDIT` entry.
5. *Duplicate protection.* Per-form idempotency keys scoped to account + operation, plus a client-side double-click guard.
6. *Safe export.* Only the signed-in account's records, masked numbers, validated filters, formula-injection prefixing.

## 3. Implementation summary

* **Account types:** Savings (min opening 500, min balance 500, 4% simple interest), Current (min opening 1,000, no overdraft, no interest by default), Fixed Deposit (term-based rate, locked until maturity, interest credited once after maturity, no deposits).
* **History:** filter by type and date, newest/oldest first, pagination (10 per page); times displayed in Asia/Kolkata.
* **Interest:** simple `I = PRT/100` and compound `A = P(1+R/100n)^(nT)`; validated inputs; year-by-year schedule; documented assumptions.
* **Transfer:** both accounts validated (12 digits + Luhn check digit), same-account/closed/FD/insufficient-balance rules, shared `TRF-` reference, confirmation page.
* **Export:** CSV (UTF-8 with BOM) and optional PDF (ReportLab).
* **Operations:** Gunicorn config, Render blueprint, health check, ledger audit script.

## 4. Testing

The pytest suite (`tests/`, 141 test functions, 256 parametrised cases) uses an isolated temporary JSON file for each test. It covers: all account types; PIN creation, hashing and verification; failed-login throttling and its persistence; unauthorised URL access; deposits, withdrawals and insufficient balance; interest values (simple and compound), validation and crediting; transfers, same-account rejection, closed-account restrictions and fixed-deposit rules; **atomicity** (a failure injected between debit and credit, and a failed disk write — balances and ledger unchanged); concurrent deposits/withdrawals/transfers (no lost updates, no overdraft, money conserved); history filters and pagination; CSV content, masking, filters, empty export and formula injection; CSRF on every state-changing route; JSON persistence, backups and corrupt-file handling; and invalid-input handling.

## 5. Results

* Test run recorded during development: **256 passed, 0 failed.** Honest caveat: the build environment had no network access, so the real `pytest` could not be installed and a small stand-in runner (supporting fixtures, `tmp_path`, `monkeypatch`, `raises`, `parametrize`) executed the suite. The suite should be re-run with real `pytest` (`pytest` in the project root).
* The application was started with `python run.py` and exercised over HTTP: PIN login, deposit, PIN-confirmed transfer with confirmation page, `403` when requesting another account, `400` for a POST without a CSRF token, and a CSV download with masked account numbers.
* Seeded demo data passes the ledger audit; a hand check of interest (e.g. 26,499.50 × 4% × 3/12 = 265.00; 100,000 × 6.5% for 12 months = 6,500.00) matched the application.
* The interface was rendered in headless Chromium at desktop (1440 px) and phone (390 px) widths for every page: no console errors, no horizontal overflow, mobile drawer working (screenshots in `docs/screenshots/`).
* Not verified in the build environment: running under Gunicorn, the Render deployment itself, the Windows file-lock branch, other browsers or real devices, and screen-reader behaviour.

## 6. Limitations

JSON storage is single-host and rewrites the whole file; the free Render tier loses data on restart; a numeric PIN is weak authentication by nature; savings interest can be credited repeatedly because time is simulated; there is no registration rate limiting or multi-factor login.

## 7. Conclusion

BankSim meets the six assignment requirements with a clear separation of concerns, correct decimal arithmetic, an all-or-nothing transfer boundary and a broad automated test suite. It demonstrates how bank-style correctness and security concerns can be handled in Python, while being explicit that it is a teaching tool. Natural next steps are a relational database, multi-factor authentication and richer account features.
