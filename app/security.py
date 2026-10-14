"""PIN hashing, failed-login throttling and CSRF helpers."""
import hashlib
import hmac
import math
import secrets
from datetime import timedelta

from werkzeug.security import check_password_hash, generate_password_hash

from .utils import parse_iso

HASH_METHOD = "scrypt"  # memory-hard, salted; werkzeug embeds salt + parameters


def _material(pin, pepper):
    """Optionally bind the hash to a server-side secret (a 'pepper')."""
    if pepper:
        return hmac.new(pepper.encode(), pin.encode(), hashlib.sha256).hexdigest()
    return pin


def hash_pin(pin, pepper=""):
    return generate_password_hash(_material(pin, pepper), method=HASH_METHOD)


_DUMMY_HASH = None


def verify_pin(pin, pin_hash, pepper=""):
    """Constant-time verify.  ``pin_hash=None`` burns equal time (unknown account)."""
    global _DUMMY_HASH
    if pin_hash is None:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = generate_password_hash("000000-dummy", method=HASH_METHOD)
        check_password_hash(_DUMMY_HASH, _material(pin or "", pepper))
        return False
    try:
        return check_password_hash(pin_hash, _material(pin or "", pepper))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# Throttling.  State lives in the JSON store (key -> entry) so it survives
# restarts and is shared between workers.  Entry shape:
#   {"count": int, "first": iso, "locked_until": iso | None}
# --------------------------------------------------------------------------
def lock_remaining(throttle, keys, now):
    """Seconds left on the longest active lock among ``keys`` (0 = not locked)."""
    remaining = 0
    for key in keys:
        entry = throttle.get(key)
        if entry and entry.get("locked_until"):
            left = (parse_iso(entry["locked_until"]) - now).total_seconds()
            if left > 0:
                remaining = max(remaining, math.ceil(left))
    return remaining


def record_failure(throttle, key_limits, now, window_minutes, lockout_minutes):
    """Count a failure against each ``(key, max_attempts)`` and lock on the limit."""
    window = timedelta(minutes=window_minutes)
    for key, limit in key_limits:
        entry = throttle.get(key)
        if entry:
            still_locked = entry.get("locked_until") and parse_iso(entry["locked_until"]) > now
            if not still_locked and parse_iso(entry["first"]) + window < now:
                entry = None
        if entry is None:
            entry = {"count": 0, "first": now.isoformat(), "locked_until": None}
        entry["count"] += 1
        if entry["count"] >= limit:
            entry["locked_until"] = (now + timedelta(minutes=lockout_minutes)).isoformat()
            entry["count"] = 0
            entry["first"] = now.isoformat()
        throttle[key] = entry
    prune(throttle, now, window_minutes)


def clear_failures(throttle, keys):
    for key in keys:
        throttle.pop(key, None)


def prune(throttle, now, window_minutes):
    """Drop stale entries so the table cannot grow without bound."""
    window = timedelta(minutes=window_minutes)
    for key in list(throttle):
        entry = throttle[key]
        locked = entry.get("locked_until") and parse_iso(entry["locked_until"]) > now
        if not locked and parse_iso(entry["first"]) + window < now:
            del throttle[key]


# --------------------------------------------------------------------------
# CSRF (synchroniser-token pattern; token lives in the signed session cookie)
# --------------------------------------------------------------------------
def new_csrf_token():
    return secrets.token_urlsafe(32)


def tokens_match(expected, sent):
    if not expected or not sent:
        return False
    return hmac.compare_digest(str(expected).encode(), str(sent).encode())
