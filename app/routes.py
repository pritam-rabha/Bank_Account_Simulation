"""HTTP routes (a single blueprint)."""
from functools import wraps
from urllib.parse import urlparse

from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)

from datetime import timedelta

from . import account_types as at
from . import exporter
from . import interest as interest_mod
from .errors import AuthError, BankError, LockedError, ValidationError
from .services import TXN_TYPES
from .utils import D, compact_money, now_utc, parse_iso

bp = Blueprint("bank", __name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def bank():
    return current_app.extensions["bank"]


def client_ip():
    return request.remote_addr or "unknown"


def field(name, limit=200):
    return (request.form.get(name) or "")[:limit]


def safe_next(target):
    """Only allow same-site relative redirects (prevents open redirects)."""
    if not target:
        return None
    parts = urlparse(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    if "\\" in target:
        return None
    return target


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.account is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required"), 401
            flash("Please log in to continue.", "warning")
            return redirect(url_for("bank.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapper


def account_required(view):
    """Login + the account number in the URL must be the signed-in account."""

    @wraps(view)
    @login_required
    def wrapper(account_number, *args, **kwargs):
        if account_number != g.account["account_number"]:
            abort(403)  # same answer whether or not the other account exists
        return view(account_number, *args, **kwargs)

    return wrapper


def start_session(account):
    session.clear()
    session.permanent = True
    session["account_number"] = account["account_number"]
    session["auth_at"] = now_utc().isoformat()


def render_error_form(template, exc, status=None, **ctx):
    errors = {exc.field or "_form": exc.message}
    return render_template(template, errors=errors, **ctx), status or exc.status_code


def build_chart(rows_asc, tz, symbol):
    """Step-line balance chart geometry for the dashboard (SVG, no JS).

    Geometry uses floats purely for drawing; no money is calculated with them.
    """
    pts = rows_asc[-40:]
    if len(pts) < 2:
        return None
    w, h, left, right, top, bottom = 720, 200, 62, 18, 14, 30
    values = [D(t["new_balance"]) for t in pts]
    times = [parse_iso(t["timestamp"]).timestamp() for t in pts]
    lo, hi = min(values), max(values)
    span = hi - lo if hi != lo else max(hi * D("0.2"), D("1"))
    lo, hi = max(D("0"), lo - span * D("0.1")), hi + span * D("0.1")
    t0, t1 = times[0], max(times[-1], times[0] + 1)

    def px(t):
        return left + (t - t0) / (t1 - t0) * (w - left - right)

    def py(v):
        return top + float((hi - v) / (hi - lo)) * (h - top - bottom)

    xs = [px(t) for t in times]
    ys = [py(v) for v in values]
    path = f"M{xs[0]:.1f} {ys[0]:.1f}"
    for i in range(1, len(xs)):
        path += f" L{xs[i]:.1f} {ys[i - 1]:.1f} L{xs[i]:.1f} {ys[i]:.1f}"
    base = h - bottom
    area = f"{path} L{xs[-1]:.1f} {base} L{xs[0]:.1f} {base} Z"
    grid = [{"y": round(py(lo + (hi - lo) * i / 3), 1), "label": compact_money(lo + (hi - lo) * i / 3, symbol)}
            for i in range(4)]
    dots = [{"x": round(x, 1), "y": round(y, 1)} for x, y in zip(xs, ys)] if len(xs) <= 24 else \
        [{"x": round(xs[-1], 1), "y": round(ys[-1], 1)}]
    first = parse_iso(pts[0]["timestamp"]).astimezone(tz).strftime("%d %b %Y")
    last = parse_iso(pts[-1]["timestamp"]).astimezone(tz).strftime("%d %b %Y")
    return {"w": w, "h": h, "left": left, "right": right, "path": path, "area": area, "grid": grid,
            "dots": dots, "x_first": first, "x_last": last, "count": len(pts),
            "aria": f"Balance after each of the last {len(pts)} transactions, from {first} to {last}."}


def flash_receipt(result, verb):
    t = result["transactions"][0]
    flash(
        f"{verb} successful. Transaction {t['transaction_id']} \u2022 Reference {t['reference']} "
        f"\u2022 New balance {current_app.jinja_env.filters['money'](t['new_balance'])}.",
        "success",
    )


# ---------------------------------------------------------------------------
# public pages
# ---------------------------------------------------------------------------
@bp.route("/")
def home():
    return render_template("home.html")


@bp.route("/about")
def about():
    cfg = current_app.config
    return render_template("about.html", pdf_ok=exporter.pdf_available(), fd_rates=cfg["FD_RATES"])


@bp.route("/healthz")
def healthz():
    bank().store.snapshot()  # proves the data file is readable
    return jsonify(status="ok")


# ---------------------------------------------------------------------------
# registration / login / logout
# ---------------------------------------------------------------------------
@bp.route("/register", methods=["GET", "POST"])
def register():
    cfg = current_app.config
    form = {"account_type": at.SAVINGS}
    if request.method == "POST":
        form = {k: field(k) for k in ("holder_name", "account_type", "opening_deposit", "fd_term_months")}
        try:
            account = bank().create_account(
                field("holder_name"), field("account_type"), field("pin", 20), field("confirm_pin", 20),
                field("opening_deposit"), fd_term_months=field("fd_term_months"),
            )
        except BankError as exc:
            return render_error_form("register.html", exc, form=form)
        start_session(account)
        flash(
            f"Account created. Your account number is {account['account_number']} \u2014 "
            "note it down, you need it (and your PIN) to log in.",
            "success",
        )
        return redirect(url_for("bank.dashboard"))
    return render_template("register.html", form=form, errors={})


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.account is not None:
        return redirect(url_for("bank.dashboard"))
    nxt = safe_next(request.values.get("next"))
    if request.method == "POST":
        number = field("account_number", 30).strip()
        try:
            account = bank().authenticate(number, field("pin", 20), ip=client_ip())
        except LockedError as exc:
            resp = Response(render_template("login.html", errors={"_form": exc.message},
                                            form={"account_number": number}, next=nxt), 429)
            resp.headers["Retry-After"] = str(exc.retry_after)
            return resp
        except AuthError as exc:
            return render_template("login.html", errors={"_form": exc.message},
                                   form={"account_number": number}, next=nxt), 401
        start_session(account)
        flash("Logged in. Welcome back!", "success")
        return redirect(nxt or url_for("bank.dashboard"))
    return render_template("login.html", errors={}, form={}, next=nxt)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("bank.home"))


# ---------------------------------------------------------------------------
# dashboard & overview
# ---------------------------------------------------------------------------
@bp.route("/dashboard")
@login_required
def dashboard():
    number = g.account["account_number"]
    account = bank().require_account(number)
    rows = bank().query_transactions(number)
    recent = rows[:5]
    cutoff = now_utc() - timedelta(days=30)
    money_in = sum((D(t["amount"]) for t in rows if t["direction"] == "CREDIT" and parse_iso(t["timestamp"]) >= cutoff), D("0"))
    money_out = sum((D(t["amount"]) for t in rows if t["direction"] == "DEBIT" and parse_iso(t["timestamp"]) >= cutoff), D("0"))
    rule = bank().rules[account["account_type"]]
    funds = bank().funds_info(account)
    chart = build_chart(list(reversed(rows)), bank().tz, current_app.config["CURRENCY_SYMBOL"])
    return render_template("dashboard.html", account=account, recent=recent, count=len(rows),
                           money_in=money_in, money_out=money_out, rule=rule, chart=chart,
                           funds=funds)


@bp.route("/account/<account_number>")
@account_required
def overview(account_number):
    account = bank().require_account(account_number)
    status = bank().interest_status(account)
    return render_template("overview.html", account=account, rule=bank().rules[account["account_type"]],
                           istatus=status)


# ---------------------------------------------------------------------------
# deposit / withdraw
# ---------------------------------------------------------------------------
def _money_form(account_number, template, action, verb):
    account = bank().require_account(account_number)
    key = request.form.get("idempotency_key") if request.method == "POST" else None
    form = {"amount": "", "description": "", "idempotency_key": key}
    if request.method == "POST":
        form.update(amount=field("amount", 30), description=field("description", 100))
        try:
            result = action(account_number, form["amount"], form["description"], key)
        except BankError as exc:
            return render_error_form(template, exc, account=account, form=form)
        if result["duplicate"]:
            flash("This request was already processed - no second transaction was created.", "info")
        else:
            flash_receipt(result, verb)
        return redirect(url_for("bank.dashboard"))
    return render_template(template, account=account, form=form, errors={})


@bp.route("/account/<account_number>/deposit", methods=["GET", "POST"])
@account_required
def deposit(account_number):
    return _money_form(account_number, "deposit.html",
                       lambda a, amt, d, k: bank().deposit(a, amt, d, k), "Deposit")


@bp.route("/account/<account_number>/withdraw", methods=["GET", "POST"])
@account_required
def withdraw(account_number):
    return _money_form(account_number, "withdraw.html",
                       lambda a, amt, d, k: bank().withdraw(a, amt, d, k), "Withdrawal")


# ---------------------------------------------------------------------------
# transfer
# ---------------------------------------------------------------------------
@bp.route("/account/<account_number>/transfer", methods=["GET", "POST"])
@account_required
def transfer(account_number):
    account = bank().require_account(account_number)
    form = {"destination_account": "", "amount": "", "description": "", "idempotency_key": None}
    if request.method == "POST":
        form.update(destination_account=field("destination_account", 30).strip(),
                    amount=field("amount", 30), description=field("description", 100),
                    idempotency_key=request.form.get("idempotency_key"))
        try:
            result = bank().transfer(account_number, form["destination_account"], form["amount"],
                                     field("pin", 20), form["description"], form["idempotency_key"],
                                     ip=client_ip())
        except LockedError as exc:
            resp = Response(*render_error_form("transfer.html", exc, account=account, form=form))
            resp.headers["Retry-After"] = str(exc.retry_after)
            return resp
        except AuthError as exc:
            return render_template("transfer.html", account=account, form=form,
                                   errors={"pin": "Incorrect PIN. The transfer was not made."}), 401
        except BankError as exc:
            return render_error_form("transfer.html", exc, account=account, form=form)
        if result["duplicate"]:
            flash("This transfer was already processed - no second transfer was made.", "info")
        return redirect(url_for("bank.transfer_receipt", account_number=account_number,
                                reference=result["reference"]))
    return render_template("transfer.html", account=account, form=form, errors={})


@bp.route("/account/<account_number>/transfer/receipt/<reference>")
@account_required
def transfer_receipt(account_number, reference):
    entries = [t for t in bank().get_transaction_by_reference(account_number, reference)
               if t["type"] == "TRANSFER_OUT"]
    if not entries:
        abort(404)
    return render_template("transfer_receipt.html", txn=entries[0],
                           account=bank().require_account(account_number))


# ---------------------------------------------------------------------------
# interest
# ---------------------------------------------------------------------------
@bp.route("/interest-calculator")
def interest_calculator():
    form = {"principal": request.args.get("principal", ""), "rate": request.args.get("rate", ""),
            "time_value": request.args.get("time_value", ""), "time_unit": request.args.get("time_unit", "years"),
            "method": request.args.get("method", "simple"), "compounding": request.args.get("compounding", "4")}
    result, errors = None, {}
    if "calculate" in request.args:
        try:
            p, r, tv, unit, method, n = interest_mod.parse_inputs(
                form["principal"], form["rate"], form["time_value"], form["time_unit"],
                form["method"], form["compounding"])
            result = interest_mod.calculate(p, r, tv, unit, method, n)
        except ValidationError as exc:
            errors[exc.field or "_form"] = exc.message
    return render_template("interest_calculator.html", form=form, result=result, errors=errors,
                           compounding=interest_mod.COMPOUNDING_CHOICES), (400 if errors else 200)


@bp.route("/account/<account_number>/interest", methods=["GET", "POST"])
@account_required
def account_interest(account_number):
    account = bank().require_account(account_number)
    months_raw = (request.form.get("months") if request.method == "POST" else request.args.get("months")) or "1"
    errors = {}
    try:
        months = int(months_raw)
        if not 1 <= months <= 12:
            raise ValueError
    except ValueError:
        errors["months"] = "Months must be a whole number from 1 to 12."
        months = 1
    if request.method == "POST" and not errors:
        try:
            result = bank().apply_interest(account_number, str(months),
                                           request.form.get("idempotency_key"))
        except BankError as exc:
            errors[exc.field or "_form"] = exc.message
        else:
            if result["duplicate"]:
                flash("This interest credit was already processed.", "info")
            else:
                flash_receipt(result, "Interest credit")
            return redirect(url_for("bank.account_interest", account_number=account_number))
    status = bank().interest_status(account, months)
    return render_template("account_interest.html", account=account, istatus=status, months=months,
                           errors=errors, rule=bank().rules[account["account_type"]]), (400 if errors else 200)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------
def _page_number(errors):
    raw = request.args.get("page", "1")
    try:
        return max(1, int(raw))
    except ValueError:
        errors["page"] = "Page must be a number."
        return 1


@bp.route("/account/<account_number>/transactions")
@account_required
def transactions(account_number):
    account = bank().require_account(account_number)
    types, d_from, d_to, sort, errors = bank().parse_filters(request.args)
    page = _page_number(errors)
    if errors:  # show the full history, with the problems listed above the form
        rows = bank().query_transactions(account_number, None, None, None, sort != "oldest")
    else:
        rows = bank().query_transactions(account_number, types, d_from, d_to, sort != "oldest")
    pager = bank().paginate(rows, page, current_app.config["PER_PAGE"])
    args = {k: v for k, v in request.args.items() if k != "page" and v and k not in errors}
    return render_template("transactions.html", account=account, page_data=pager, args=args,
                           errors=errors, filters=request.args, sort=sort), (400 if errors else 200)


@bp.route("/api/account/<account_number>")
@account_required
def api_account(account_number):
    return jsonify(bank().require_account(account_number))


@bp.route("/api/account/<account_number>/transactions")
@account_required
def api_transactions(account_number):
    types, d_from, d_to, sort, errors = bank().parse_filters(request.args)
    if errors:
        return jsonify(errors=errors), 400
    rows = bank().query_transactions(account_number, types, d_from, d_to, sort != "oldest")
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        return jsonify(errors={"page": "Page must be a number."}), 400
    return jsonify(bank().paginate(rows, page, current_app.config["PER_PAGE"]))


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
@bp.route("/account/<account_number>/export")
@account_required
def export(account_number):
    account = bank().require_account(account_number)
    fmt = (request.args.get("format") or "").strip().lower()
    pdf_ok = exporter.pdf_available()
    ctx = dict(account=account, filters=request.args, pdf_ok=pdf_ok, errors={}, empty=False)
    if not fmt:  # first visit: show the form
        return render_template("export.html", **ctx)
    types, d_from, d_to, sort, errors = bank().parse_filters(request.args)
    if fmt not in ("csv", "pdf"):
        errors["format"] = "Choose CSV or PDF."
    elif fmt == "pdf" and not pdf_ok:
        errors["format"] = "PDF export is unavailable on this server (reportlab is not installed). Use CSV."
    if errors:
        ctx["errors"] = errors
        return render_template("export.html", **ctx), 400
    rows = bank().query_transactions(account_number, types, d_from, d_to, sort != "oldest")
    if not rows:
        ctx["empty"] = True
        return render_template("export.html", **ctx)
    name = exporter.statement_filename(account_number, d_from, d_to, fmt)
    if fmt == "csv":
        body, mime = exporter.build_csv(rows, bank().tz, TXN_TYPES), "text/csv; charset=utf-8"
    else:
        period = f"{d_from or 'start'} to {d_to or 'latest'}"
        body = exporter.build_pdf(account, rows, bank().tz, TXN_TYPES, period)
        mime = "application/pdf"
    resp = Response(body, mimetype=mime.split(";")[0])
    resp.headers["Content-Type"] = mime
    resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
@bp.route("/account/<account_number>/settings")
@account_required
def settings(account_number):
    return render_template("settings.html", account=bank().require_account(account_number), errors={})


@bp.route("/account/<account_number>/settings/pin", methods=["POST"])
@account_required
def change_pin(account_number):
    account = bank().require_account(account_number)
    try:
        bank().change_pin(account_number, field("current_pin", 20), field("new_pin", 20),
                          field("confirm_pin", 20), ip=client_ip())
    except LockedError as exc:
        return render_template("settings.html", account=account, errors={"current_pin": exc.message}), 429
    except AuthError:
        return render_template("settings.html", account=account,
                               errors={"current_pin": "Current PIN is incorrect."}), 401
    except BankError as exc:
        key = {"pin": "new_pin", "confirm_pin": "confirm_pin"}.get(exc.field, exc.field or "_form")
        return render_template("settings.html", account=account, errors={key: exc.message}), exc.status_code
    session["auth_at"] = now_utc().isoformat()
    flash("PIN changed. Other signed-in sessions for this account have been ended.", "success")
    return redirect(url_for("bank.settings", account_number=account_number))


@bp.route("/account/<account_number>/settings/close", methods=["POST"])
@account_required
def close_account(account_number):
    account = bank().require_account(account_number)
    if field("confirm_text", 20).strip().upper() != "CLOSE":
        return render_template("settings.html", account=account,
                               errors={"confirm_text": "Type CLOSE to confirm."}), 400
    try:
        result = bank().close_account(account_number, field("pin", 20), ip=client_ip())
    except LockedError as exc:
        return render_template("settings.html", account=account, errors={"pin": exc.message}), 429
    except AuthError:
        return render_template("settings.html", account=account,
                               errors={"pin": "Incorrect PIN. The account was not closed."}), 401
    except BankError as exc:
        return render_template("settings.html", account=account, errors={"_close": exc.message}), exc.status_code
    flash(f"Account closed. Remaining balance paid out: {current_app.jinja_env.filters['money'](result['payout'])}.", "info")
    return redirect(url_for("bank.overview", account_number=account_number))
