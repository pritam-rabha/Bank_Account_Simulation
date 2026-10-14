"""Exception hierarchy used by BankSim.

Every error raised deliberately by the business layer derives from
:class:`BankError`, so the web layer can turn them into friendly form
messages without ever leaking a stack trace.
"""


class BankError(Exception):
    """Base class for expected, user-facing business errors."""

    status_code = 400

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field  # optional form-field name for inline display


class ValidationError(BankError):
    """Malformed or out-of-range user input."""

    status_code = 400


class NotFoundError(BankError):
    status_code = 404


class RuleError(BankError):
    """A well-formed request that violates an account rule."""

    status_code = 422


class InsufficientFundsError(RuleError):
    pass


class AccountStatusError(RuleError):
    """Operation not allowed because the account is closed."""


class AuthError(BankError):
    status_code = 401


class LockedError(AuthError):
    """Authentication temporarily locked after repeated failures."""

    status_code = 429

    def __init__(self, retry_after_seconds, field=None):
        minutes = max(1, (int(retry_after_seconds) + 59) // 60)
        plural = "" if minutes == 1 else "s"
        super().__init__(
            "Too many failed attempts. Authentication is temporarily locked. "
            f"Please try again in about {minutes} minute{plural}.",
            field,
        )
        self.retry_after = int(retry_after_seconds)


class StorageError(Exception):
    """The JSON data file could not be read or written safely."""
