"""BankSim - educational Bank Account Simulation (Flask application factory)."""
import logging
import secrets

from flask import Flask, abort, g, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from . import security
from .config import load_config
from .errors import StorageError
from .seed import seed_if_empty
from .services import TXN_TYPES, Bank
from .storage import JsonStore
from .utils import format_dt, format_money, mask_account, parse_iso

__version__ = "1.0.0"

ERROR_TEXT = {
    400: ("Bad request", "The request could not be processed. If you submitted a form, reload the page and try again."),
    403: ("Access denied", "You are not allowed to view or change this resource."),
    404: ("Page not found", "The page you are looking for does not exist."),
    405: ("Method not allowed", "That action is not supported for this address."),
    413: ("Request too large", "The data you sent was too large."),
    429: ("Too many requests", "Please wait a little while before trying again."),
    500: ("Something went wrong", "An unexpected error occurred. No changes were made to your balances."),
}


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.update(load_config())
    if overrides:
        app.config.update(overrides)
    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    if not app.testing:
        logging.basicConfig(level=logging.INFO)

    store = JsonStore(app.config["DATA_FILE"])
    bank = Bank(store, app.config)
    app.extensions["bank"] = bank
    if app.config["SEED_SAMPLE_DATA"]:
        seed_if_empty(bank, app.config["SAMPLE_DATA_FILE"])

    # ------------------------------------------------------------------ CSRF
    def csrf_token():
        if "_csrf" not in session:
            session["_csrf"] = security.new_csrf_token()
        return session["_csrf"]

    @app.before_request
    def csrf_protect():
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and app.config["CSRF_ENABLED"]:
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
            if not security.tokens_match(session.get("_csrf"), sent):
                abort(400, description="csrf")

    # ----------------------------------------- load the signed-in account
    @app.before_request
    def load_account():
        g.account = None
        number = session.get("account_number")
        if not number:
            return
        acct, pin_changed_at = bank.account_session_info(number)
        stale = False
        if acct is None:
            stale = True
        elif pin_changed_at:
            auth_at = session.get("auth_at")
            stale = not auth_at or parse_iso(auth_at) < parse_iso(pin_changed_at)
        if stale:
            session.clear()
            return
        g.account = acct

    # ------------------------------------------------------------ templating
    tz = bank.tz
    app.jinja_env.filters["money"] = lambda v: format_money(v, app.config["CURRENCY_SYMBOL"])
    app.jinja_env.filters["dt"] = lambda v: format_dt(v, tz) if v else "\u2014"
    app.jinja_env.filters["mask"] = mask_account
    app.jinja_env.filters["txn_label"] = lambda v: TXN_TYPES.get(v, v)

    @app.context_processor
    def inject():
        return {
            "csrf_token": csrf_token,
            "current_account": g.get("account"),
            "type_rules": bank.rules,
            "txn_types": TXN_TYPES,
            "new_idem_key": lambda: secrets.token_urlsafe(18),
            "cfg": app.config,
            "funds": bank.funds_info,
        }

    # ---------------------------------------------------------- headers
    @app.after_request
    def secure_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; font-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        if not request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        if app.config["SESSION_COOKIE_SECURE"]:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    # ----------------------------------------------------------- errors
    @app.errorhandler(HTTPException)
    def http_error(exc):
        code = exc.code or 500
        if request.path.startswith("/api/"):
            return jsonify(error=ERROR_TEXT.get(code, ("Error", ""))[0]), code
        title, message = ERROR_TEXT.get(code, ("Error", exc.description or ""))
        if code == 400 and exc.description == "csrf":
            title, message = (
                "Security check failed",
                "Your form session expired or the security token was missing. "
                "Go back, reload the page and submit again.",
            )
        return render_template("errors/error.html", code=code, title=title, message=message), code

    @app.errorhandler(StorageError)
    def storage_error(exc):
        app.logger.error("Storage error: %s", exc)
        title, message = ERROR_TEXT[500]
        return render_template("errors/error.html", code=500, title=title, message=message), 500

    @app.errorhandler(Exception)
    def unexpected(exc):
        app.logger.exception("Unhandled error")
        title, message = ERROR_TEXT[500]
        if request.path.startswith("/api/"):
            return jsonify(error=title), 500
        return render_template("errors/error.html", code=500, title=title, message=message), 500

    from .routes import bp

    app.register_blueprint(bp)
    return app
