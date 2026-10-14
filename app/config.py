"""Configuration loading.

All tunables come from environment variables (see ``.env.example``) so the
same code runs locally, in tests and on Render.  Money-like settings are
parsed into :class:`decimal.Decimal`, never ``float``.
"""
import logging
import os
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


def _dec(env, name, default):
    raw = env.get(name)
    if raw is None or raw == "":
        return Decimal(default)
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise RuntimeError(f"{name} must be a decimal number, got {raw!r}") from exc
    if not value.is_finite() or value < 0:
        raise RuntimeError(f"{name} must be a non-negative number, got {raw!r}")
    return value


def _int(env, name, default):
    raw = env.get(name)
    if raw is None or raw == "":
        return int(default)
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(env, name, default=False):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _fd_rates(env):
    """Fixed-deposit annual rates by term in months, e.g. ``12:6.50,24:6.75``."""
    default = "6:6.00,12:6.50,24:6.75,36:7.00,60:7.25"
    raw = env.get("FD_RATES") or default
    rates = {}
    try:
        for part in raw.split(","):
            months, rate = part.split(":")
            months_i = int(months.strip())
            rate_d = Decimal(rate.strip())
            if months_i <= 0 or rate_d < 0 or rate_d > 100:
                raise ValueError
            rates[months_i] = rate_d
    except (ValueError, InvalidOperation) as exc:
        raise RuntimeError(
            "FD_RATES must look like '12:6.50,24:6.75' (months:percent)"
        ) from exc
    return dict(sorted(rates.items()))


def load_config(env=None):
    """Build the Flask config mapping from an environment mapping."""
    env = os.environ if env is None else env
    production = env.get("BANKSIM_ENV", "development").lower() == "production"

    secret = env.get("SECRET_KEY")
    if not secret:
        if production:
            raise RuntimeError("SECRET_KEY must be set when BANKSIM_ENV=production")
        secret = secrets.token_hex(32)
        log.warning("SECRET_KEY not set; using a random one (sessions reset on restart).")

    data_file = env.get("DATA_FILE") or str(BASE_DIR / "data" / "bank_data.json")

    return {
        "SECRET_KEY": secret,
        "BANKSIM_ENV": "production" if production else "development",
        "DATA_FILE": data_file,
        "SAMPLE_DATA_FILE": str(BASE_DIR / "data" / "sample_accounts.json"),
        "SEED_SAMPLE_DATA": _bool(env, "SEED_SAMPLE_DATA", False),
        "SHOW_DEMO_CREDENTIALS": _bool(env, "SHOW_DEMO_CREDENTIALS", False),
        "TRUST_PROXY": _bool(env, "TRUST_PROXY", False),
        # --- sessions / cookies -------------------------------------------
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": _bool(env, "SESSION_COOKIE_SECURE", production),
        "SESSION_COOKIE_NAME": "banksim_session",
        "PERMANENT_SESSION_LIFETIME": timedelta(minutes=_int(env, "SESSION_MINUTES", 15)),
        "MAX_CONTENT_LENGTH": 64 * 1024,
        "CSRF_ENABLED": True,
        # --- PIN policy and throttling -------------------------------------
        "PIN_MIN_LENGTH": _int(env, "PIN_MIN_LENGTH", 4),
        "PIN_MAX_LENGTH": _int(env, "PIN_MAX_LENGTH", 6),
        "PIN_PEPPER": env.get("PIN_PEPPER", ""),
        "MAX_FAILED_ATTEMPTS": _int(env, "MAX_FAILED_ATTEMPTS", 5),
        "IP_MAX_FAILED_ATTEMPTS": _int(env, "IP_MAX_FAILED_ATTEMPTS", 20),
        "FAILURE_WINDOW_MINUTES": _int(env, "FAILURE_WINDOW_MINUTES", 15),
        "LOCKOUT_MINUTES": _int(env, "LOCKOUT_MINUTES", 15),
        # --- money rules ----------------------------------------------------
        "CURRENCY_SYMBOL": "\u20b9",
        "MAX_TRANSACTION_AMOUNT": _dec(env, "MAX_TRANSACTION_AMOUNT", "1000000.00"),
        "MAX_BALANCE": Decimal("999999999999.99"),
        "SAVINGS_INTEREST_RATE": _dec(env, "SAVINGS_INTEREST_RATE", "4.00"),
        "CURRENT_INTEREST_RATE": _dec(env, "CURRENT_INTEREST_RATE", "0.00"),
        "SAVINGS_MIN_BALANCE": _dec(env, "SAVINGS_MIN_BALANCE", "500.00"),
        "SAVINGS_MIN_OPENING": _dec(env, "SAVINGS_MIN_OPENING", "500.00"),
        "CURRENT_MIN_OPENING": _dec(env, "CURRENT_MIN_OPENING", "1000.00"),
        "FD_MIN_OPENING": _dec(env, "FD_MIN_OPENING", "1000.00"),
        "FD_RATES": _fd_rates(env),
        # --- display ----------------------------------------------------------
        "DISPLAY_TIMEZONE": env.get("DISPLAY_TIMEZONE", "Asia/Kolkata"),
        "PER_PAGE": _int(env, "PER_PAGE", 10),
    }
