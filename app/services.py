"""Business logic: accounts, ledger, transfers, interest, authentication.

Design rules
------------
* Every public method that changes money does ALL of its reads, checks and
  writes inside a single ``store.transaction()`` block.  Raising inside the
  block discards every in-memory change, so a failed operation cannot change a
  balance and a transfer cannot be half-applied.
* Money is always ``Decimal``; it is stored as a two-place string.
* Only *successful* financial operations append a ledger entry.
* ``now`` can be injected so tests (and seeding) are deterministic.
"""
import re
import secrets
from datetime import timezone
from decimal import Decimal

from . import account_types as at
from . import interest as interest_mod
from . import security
from .errors import (
    AccountStatusError,
    AuthError,
    InsufficientFundsError,
    LockedError,
    NotFoundError,
    RuleError,
    ValidationError,
)
from .utils import (
    D,
    add_months,
    clean_description,
    clean_idempotency_key,
    day_bounds_utc,
    get_display_tz,
    indian,
    is_valid_account_number,
    luhn_check_digit,
    mask_account,
    money_str,
    normalize_name,
    now_utc,
    parse_amount,
    parse_date,
    parse_iso,
    validate_new_pin,
)

TXN_TYPES = {
    "OPENING_DEPOSIT": "Opening deposit",
    "DEPOSIT": "Deposit",
    "WITHDRAWAL": "Withdrawal",
    "TRANSFER_OUT": "Transfer sent",
    "TRANSFER_IN": "Transfer received",
    "INTEREST_CREDIT": "Interest credited",
}
CREDIT_TYPES = {"OPENING_DEPOSIT", "DEPOSIT", "TRANSFER_IN", "INTEREST_CREDIT"}

PUBLIC_ACCOUNT_FIELDS = (
    "account_number", "holder_name", "account_type", "opened_at", "balance", "status",
    "interest_rate", "closed_at", "fd_term_months", "fd_principal", "fd_maturity_date",
    "fd_interest_credited",
)
IDEMPOTENCY_LIMIT = 2000


def public_account(record):
    """Whitelist copy of an account - the PIN hash can never leak from here."""
    return {k: record[k] for k in PUBLIC_ACCOUNT_FIELDS if k in record}


class Bank:
    def __init__(self, store, config):
        self.store = store
        self.cfg = config
        self.rules = at.get_rules(config)

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------
    @property
    def tz(self):
        return get_display_tz(self.cfg["DISPLAY_TIMEZONE"])

    def _pepper(self):
        return self.cfg["PIN_PEPPER"]

    @staticmethod
    def _reference(prefix, now):
        return f"{prefix}-{now:%Y%m%d}-{secrets.token_hex(4).upper()}"

    def _new_number(self, st, account_type):
        for _ in range(100):
            payload = at.NUMBER_PREFIX[account_type] + "".join(
                str(secrets.randbelow(10)) for _ in range(9)
            )
            number = payload + str(luhn_check_digit(payload))
            if number not in st["accounts"]:
                return number
        raise RuleError("Could not allocate an account number. Please try again.")

    def _clean_number(self, raw, field):
        number = (raw or "").strip()
        if not number:
            raise ValidationError("Account number is required.", field)
        if not is_valid_account_number(number):
            raise ValidationError(
                "Enter a valid 12-digit account number (check for typing mistakes).", field
            )
        return number

    # ------------------------------------------------------------------
    # idempotency (duplicate-submission protection)
    # ------------------------------------------------------------------
    @staticmethod
    def _idem_lookup(st, account_number, key, op):
        """Keys are scoped to (account, operation) so one key can never replay a different action."""
        if key:
            prior = st["idempotency"].get(f"{account_number}:{op}:{key}")
            if prior:
                return {**prior, "duplicate": True}
        return None

    @staticmethod
    def _idem_remember(st, account_number, key, op, result):
        if not key:
            return
        table = st["idempotency"]
        table[f"{account_number}:{op}:{key}"] = {
            "reference": result["reference"],
            "transactions": result["transactions"],
        }
        while len(table) > IDEMPOTENCY_LIMIT:
            table.pop(next(iter(table)))

    # ------------------------------------------------------------------
    # ledger primitive
    # ------------------------------------------------------------------
    def _post(self, st, acct, tx_type, amount, *, description, reference, now, counterparty=None):
        """Apply one ledger entry to ``acct`` inside an open transaction."""
        credit = tx_type in CREDIT_TYPES
        previous = D(acct["balance"])
        new = previous + amount if credit else previous - amount
        if new < 0:
            raise InsufficientFundsError("Insufficient balance.", "amount")
        if new > self.cfg["MAX_BALANCE"]:
            raise RuleError("This operation would exceed the maximum balance allowed.", "amount")
        acct["balance"] = money_str(new)
        seq = st["meta"]["next_seq"]
        st["meta"]["next_seq"] = seq + 1
        entry = {
            "transaction_id": f"TXN-{seq:08d}-{secrets.token_hex(2).upper()}",
            "seq": seq,
            "type": tx_type,
            "direction": "CREDIT" if credit else "DEBIT",
            "account_number": acct["account_number"],
            "amount": money_str(amount),
            "previous_balance": money_str(previous),
            "new_balance": money_str(new),
            "timestamp": now.astimezone(timezone.utc).isoformat(),
            "status": "COMPLETED",
            "description": description,
            "reference": reference,
            "counterparty": counterparty,
        }
        st["transactions"].append(entry)
        return entry

    # ------------------------------------------------------------------
    # rule checks
    # ------------------------------------------------------------------
    @staticmethod
    def _is_matured(acct, now):
        return now >= parse_iso(acct["fd_maturity_date"])

    def funds_info(self, account, now=None):
        """What can actually be taken out right now (for the UI)."""
        now = now or now_utc()
        rule = self.rules[account["account_type"]]
        locked = account["account_type"] == at.FIXED_DEPOSIT and not self._is_matured(account, now)
        available = Decimal("0.00")
        if not locked and account["status"] == "ACTIVE":
            available = max(D(account["balance"]) - rule.min_balance, Decimal("0.00"))
        return {"available": available, "locked": locked, "min_balance": rule.min_balance, "rule": rule}

    def _ensure_active(self, acct, who):
        if acct["status"] != "ACTIVE":
            raise AccountStatusError(f"{who} account is closed.")

    def _ensure_can_debit(self, acct, amount, now, who="Your"):
        self._ensure_active(acct, who)
        rule = self.rules[acct["account_type"]]
        if acct["account_type"] == at.FIXED_DEPOSIT and not self._is_matured(acct, now):
            matures = parse_iso(acct["fd_maturity_date"]).astimezone(self.tz)
            raise RuleError(
                f"Fixed deposit funds are locked until maturity ({matures:%d %b %Y}).", "amount"
            )
        available = D(acct["balance"]) - rule.min_balance
        if amount > available:
            shown = max(available, Decimal("0.00"))
            raise InsufficientFundsError(
                f"Insufficient balance. Available to withdraw: {self.cfg['CURRENCY_SYMBOL']}{indian(shown)}"
                + (f" (minimum balance {indian(rule.min_balance)} must remain)." if rule.min_balance else "."),
                "amount",
            )

    def _ensure_can_credit(self, acct, who="Destination"):
        self._ensure_active(acct, who)
        if not self.rules[acct["account_type"]].accepts_deposits:
            raise RuleError(
                f"{who} account is a fixed deposit and does not accept deposits or transfers in.",
                "amount" if who == "Your" else "destination_account",
            )

    # ------------------------------------------------------------------
    # accounts
    # ------------------------------------------------------------------
    def create_account(self, holder_name, account_type, pin, confirm_pin, opening_deposit,
                       fd_term_months=None, now=None, _fixed_number=None):
        now = now or now_utc()
        name = normalize_name(holder_name)
        rule = self.rules.get(account_type)
        if rule is None:
            raise ValidationError("Choose a valid account type.", "account_type")
        pin = validate_new_pin(pin, confirm_pin, self.cfg["PIN_MIN_LENGTH"], self.cfg["PIN_MAX_LENGTH"])
        amount = parse_amount(
            opening_deposit, field="Opening deposit", minimum=rule.min_opening_deposit,
            maximum=self.cfg["MAX_TRANSACTION_AMOUNT"], field_name="opening_deposit",
        )
        rate = rule.default_rate
        term = None
        if account_type == at.FIXED_DEPOSIT:
            try:
                term = int(str(fd_term_months).strip())
            except (TypeError, ValueError):
                term = None
            if term not in self.cfg["FD_RATES"]:
                raise ValidationError("Choose one of the available fixed deposit terms.", "fd_term_months")
            rate = self.cfg["FD_RATES"][term]
        pin_hash = security.hash_pin(pin, self._pepper())  # slow; done outside the lock

        with self.store.transaction() as st:
            number = _fixed_number or self._new_number(st, account_type)
            if number in st["accounts"]:
                raise RuleError("Account number already exists.")
            record = {
                "account_number": number,
                "holder_name": name,
                "account_type": account_type,
                "opened_at": now.astimezone(timezone.utc).isoformat(),
                "balance": "0.00",
                "status": "ACTIVE",
                "interest_rate": money_str(rate),
                "closed_at": None,
                "pin_hash": pin_hash,
                "pin_changed_at": None,
            }
            if account_type == at.FIXED_DEPOSIT:
                record.update(
                    fd_term_months=term,
                    fd_principal=money_str(amount),
                    fd_maturity_date=add_months(now.astimezone(timezone.utc), term).isoformat(),
                    fd_interest_credited=False,
                )
            st["accounts"][number] = record
            self._post(
                st, record, "OPENING_DEPOSIT", amount, description="Opening deposit",
                reference=self._reference("OPN", now), now=now,
            )
            return public_account(record)

    def get_account(self, account_number):
        st = self.store.snapshot()
        record = st["accounts"].get(account_number)
        return public_account(record) if record else None

    def require_account(self, account_number):
        acct = self.get_account(account_number)
        if acct is None:
            raise NotFoundError("Account not found.")
        return acct

    def account_session_info(self, account_number):
        """(public account, pin_changed_at) for session validation."""
        st = self.store.snapshot()
        record = st["accounts"].get(account_number)
        if not record:
            return None, None
        return public_account(record), record.get("pin_changed_at")

    # ------------------------------------------------------------------
    # authentication with throttling
    # ------------------------------------------------------------------
    def _limits(self, number, ip):
        return [
            (f"acct:{number}", self.cfg["MAX_FAILED_ATTEMPTS"]),
            (f"ip:{ip}", self.cfg["IP_MAX_FAILED_ATTEMPTS"]),
        ]

    def _fail(self, st, number, ip, now):
        limits = self._limits(number, ip) if number else [(f"ip:{ip}", self.cfg["IP_MAX_FAILED_ATTEMPTS"])]
        security.record_failure(
            st["throttle"], limits, now, self.cfg["FAILURE_WINDOW_MINUTES"], self.cfg["LOCKOUT_MINUTES"]
        )
        locked = security.lock_remaining(st["throttle"], [k for k, _ in limits], now)
        return LockedError(locked) if locked else AuthError("Invalid account number or PIN.")

    def _check_pin(self, st, record, pin, ip, now):
        """Verify a PIN inside an open transaction.

        Returns ``None`` on success or an exception *instance* that the caller
        raises AFTER the transaction commits (so the failure counter persists).
        """
        number = record["account_number"]
        keys = [k for k, _ in self._limits(number, ip)]
        locked = security.lock_remaining(st["throttle"], keys, now)
        if locked:
            return LockedError(locked)
        if security.verify_pin(pin, record["pin_hash"], self._pepper()):
            security.clear_failures(st["throttle"], [f"acct:{number}"])
            return None
        return self._fail(st, number, ip, now)

    def authenticate(self, account_number, pin, ip="unknown", now=None):
        """Return the public account on success; raise AuthError/LockedError."""
        now = now or now_utc()
        number = (account_number or "").strip()
        pin = pin or ""
        pending = None
        result = None
        with self.store.transaction() as st:
            record = st["accounts"].get(number) if is_valid_account_number(number) else None
            if record is None:
                locked = security.lock_remaining(st["throttle"], [f"ip:{ip}"], now)
                if locked:
                    pending = LockedError(locked)
                else:
                    security.verify_pin(pin, None, self._pepper())  # equalise timing
                    pending = self._fail(st, None, ip, now)
            else:
                pending = self._check_pin(st, record, pin, ip, now)
                if pending is None:
                    result = public_account(record)
        if pending is not None:
            raise pending
        return result

    # ------------------------------------------------------------------
    # deposits / withdrawals
    # ------------------------------------------------------------------
    def _single_entry(self, account_number, tx_type, amount_raw, description, default_desc,
                      prefix, idempotency_key, now):
        now = now or now_utc()
        amount = parse_amount(amount_raw, maximum=self.cfg["MAX_TRANSACTION_AMOUNT"])
        desc = clean_description(description) or default_desc
        key = clean_idempotency_key(idempotency_key)
        with self.store.transaction() as st:
            dup = self._idem_lookup(st, account_number, key, tx_type)
            if dup:
                return dup
            acct = st["accounts"].get(account_number)
            if acct is None:
                raise NotFoundError("Account not found.")
            if tx_type == "DEPOSIT":
                self._ensure_can_credit(acct, who="Your")
            else:
                self._ensure_can_debit(acct, amount, now)
            ref = self._reference(prefix, now)
            entry = self._post(st, acct, tx_type, amount, description=desc, reference=ref, now=now)
            result = {"reference": ref, "transactions": [entry], "duplicate": False}
            self._idem_remember(st, account_number, key, tx_type, result)
            return result

    def deposit(self, account_number, amount_raw, description="", idempotency_key=None, now=None):
        return self._single_entry(account_number, "DEPOSIT", amount_raw, description,
                                  "Deposit", "DEP", idempotency_key, now)

    def withdraw(self, account_number, amount_raw, description="", idempotency_key=None, now=None):
        return self._single_entry(account_number, "WITHDRAWAL", amount_raw, description,
                                  "Withdrawal", "WDL", idempotency_key, now)

    # ------------------------------------------------------------------
    # transfers  (debit + credit in ONE storage transaction)
    # ------------------------------------------------------------------
    def transfer(self, source_number, destination_number, amount_raw, pin, description="",
                 idempotency_key=None, ip="unknown", now=None):
        now = now or now_utc()
        src_no = self._clean_number(source_number, "source")
        dst_no = self._clean_number(destination_number, "destination_account")
        if src_no == dst_no:
            raise ValidationError("Source and destination accounts must be different.", "destination_account")
        amount = parse_amount(amount_raw, maximum=self.cfg["MAX_TRANSACTION_AMOUNT"])
        desc = clean_description(description)
        key = clean_idempotency_key(idempotency_key)
        pending = None
        result = None
        with self.store.transaction() as st:
            dup = self._idem_lookup(st, src_no, key, "TRANSFER")
            if dup:
                return dup
            src = st["accounts"].get(src_no)
            if src is None:
                raise NotFoundError("Source account not found.")
            pending = self._check_pin(st, src, pin, ip, now)
            if pending is None:
                dst = st["accounts"].get(dst_no)
                if dst is None:
                    raise NotFoundError("Destination account not found.", "destination_account")
                self._ensure_can_debit(src, amount, now, who="Your")
                self._ensure_can_credit(dst, who="Destination")
                ref = self._reference("TRF", now)
                debit = self._post(
                    st, src, "TRANSFER_OUT", amount, reference=ref, now=now, counterparty=dst_no,
                    description=desc or f"Transfer to {mask_account(dst_no)}",
                )
                credit = self._post(
                    st, dst, "TRANSFER_IN", amount, reference=ref, now=now, counterparty=src_no,
                    description=desc or f"Transfer from {mask_account(src_no)}",
                )
                result = {"reference": ref, "transactions": [debit, credit], "duplicate": False}
                self._idem_remember(st, src_no, key, "TRANSFER", result)
            # leaving the block commits BOTH ledger entries and BOTH balances
        if pending is not None:
            raise pending
        return result

    # ------------------------------------------------------------------
    # interest
    # ------------------------------------------------------------------
    def interest_status(self, account, months=1, now=None):
        """Describe whether interest can be credited and the *estimate* for it."""
        now = now or now_utc()
        out = {"eligible": False, "reason": "", "estimate": None, "months": None, "kind": None}
        if account["status"] != "ACTIVE":
            out["reason"] = "This account is closed."
            return out
        rate = D(account["interest_rate"])
        if account["account_type"] == at.FIXED_DEPOSIT:
            out["kind"] = "maturity"
            matured = self._is_matured(account, now)
            term = account["fd_term_months"]
            out["estimate"] = interest_mod.simple_interest_amount(D(account["fd_principal"]), rate, term)
            out["months"] = term
            if account["fd_interest_credited"]:
                out["reason"] = "Maturity interest has already been credited."
            elif not matured:
                when = parse_iso(account["fd_maturity_date"]).astimezone(self.tz)
                out["reason"] = f"Interest can be credited only after maturity on {when:%d %b %Y}."
            else:
                out["eligible"] = True
            return out
        if rate <= 0:
            out["reason"] = "This account type does not earn interest (rate is 0%)."
            return out
        out["kind"] = "periodic"
        out["months"] = months
        out["estimate"] = interest_mod.simple_interest_amount(D(account["balance"]), rate, months)
        out["eligible"] = out["estimate"] > 0
        if not out["eligible"]:
            out["reason"] = "Interest for this period is less than 0.01, so nothing can be credited."
        return out

    def apply_interest(self, account_number, months_raw="1", idempotency_key=None, now=None):
        """CREDIT interest to the account (writes an INTEREST_CREDIT transaction)."""
        now = now or now_utc()
        key = clean_idempotency_key(idempotency_key)
        with self.store.transaction() as st:
            dup = self._idem_lookup(st, account_number, key, "INTEREST")
            if dup:
                return dup
            acct = st["accounts"].get(account_number)
            if acct is None:
                raise NotFoundError("Account not found.")
            self._ensure_active(acct, "Your")
            rate = D(acct["interest_rate"])
            if acct["account_type"] == at.FIXED_DEPOSIT:
                if acct["fd_interest_credited"]:
                    raise RuleError("Maturity interest has already been credited.")
                if not self._is_matured(acct, now):
                    raise RuleError("Fixed deposit interest can be credited only after maturity.")
                months = acct["fd_term_months"]
                base = D(acct["fd_principal"])
            else:
                if rate <= 0:
                    raise RuleError("This account type does not earn interest.")
                try:
                    months = int(str(months_raw).strip())
                except ValueError as exc:
                    raise ValidationError("Months must be a whole number from 1 to 12.", "months") from exc
                if not 1 <= months <= 12:
                    raise ValidationError("Months must be a whole number from 1 to 12.", "months")
                base = D(acct["balance"])
            amount = interest_mod.simple_interest_amount(base, rate, months)
            if amount <= 0:
                raise RuleError("Interest for this period is less than 0.01, so nothing was credited.")
            ref = self._reference("INT", now)
            entry = self._post(
                st, acct, "INTEREST_CREDIT", amount, reference=ref, now=now,
                description=f"Simple interest {rate}% p.a. for {months} month(s) on {indian(base)}",
            )
            if acct["account_type"] == at.FIXED_DEPOSIT:
                acct["fd_interest_credited"] = True
            result = {"reference": ref, "transactions": [entry], "duplicate": False}
            self._idem_remember(st, account_number, key, "INTEREST", result)
            return result

    # ------------------------------------------------------------------
    # settings: change PIN / close account
    # ------------------------------------------------------------------
    def change_pin(self, account_number, current_pin, new_pin, confirm_pin, ip="unknown", now=None):
        now = now or now_utc()
        new_pin = validate_new_pin(new_pin, confirm_pin, self.cfg["PIN_MIN_LENGTH"], self.cfg["PIN_MAX_LENGTH"])
        if new_pin == (current_pin or ""):
            raise ValidationError("New PIN must be different from the current PIN.", "pin")
        new_hash = security.hash_pin(new_pin, self._pepper())
        pending = None
        with self.store.transaction() as st:
            record = st["accounts"].get(account_number)
            if record is None:
                raise NotFoundError("Account not found.")
            pending = self._check_pin(st, record, current_pin, ip, now)
            if pending is None:
                record["pin_hash"] = new_hash
                record["pin_changed_at"] = now.astimezone(timezone.utc).isoformat()
        if pending is not None:
            raise pending

    def close_account(self, account_number, pin, ip="unknown", now=None):
        """Pay out any remaining balance and mark the account CLOSED."""
        now = now or now_utc()
        pending = None
        result = None
        with self.store.transaction() as st:
            record = st["accounts"].get(account_number)
            if record is None:
                raise NotFoundError("Account not found.")
            pending = self._check_pin(st, record, pin, ip, now)
            if pending is None:
                self._ensure_active(record, "This")
                if record["account_type"] == at.FIXED_DEPOSIT and not self._is_matured(record, now):
                    raise RuleError("A fixed deposit cannot be closed before its maturity date.")
                payout = D(record["balance"])
                entries = []
                if payout > 0:
                    entries.append(self._post(
                        st, record, "WITHDRAWAL", payout, now=now,
                        reference=self._reference("CLS", now),
                        description="Account closure payout",
                    ))
                record["status"] = "CLOSED"
                record["closed_at"] = now.astimezone(timezone.utc).isoformat()
                result = {"payout": payout, "transactions": entries}
        if pending is not None:
            raise pending
        return result

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------
    def query_transactions(self, account_number=None, types=None, date_from=None, date_to=None,
                           newest_first=True):
        """Filtered list.  ``account_number=None`` returns the complete ledger
        (internal/audit use only - the web UI always passes the signed-in account)."""
        st = self.store.snapshot()
        return self._filter(st["transactions"], account_number, types, date_from, date_to, newest_first)

    def _filter(self, transactions, account_number, types, date_from, date_to, newest_first):
        start, end = day_bounds_utc(date_from, date_to, self.tz)
        rows = []
        for t in transactions:
            if account_number is not None and t["account_number"] != account_number:
                continue
            if types and t["type"] not in types:
                continue
            if start or end:
                ts = parse_iso(t["timestamp"])
                if start and ts < start:
                    continue
                if end and ts >= end:
                    continue
            rows.append(t)
        rows.sort(key=lambda t: (t["timestamp"], t["seq"]), reverse=newest_first)
        return rows

    def parse_filters(self, args):
        """Validate history/export filter query values -> (types, d_from, d_to, sort, errors)."""
        errors = {}
        types = None
        raw_type = (args.get("type") or "").strip()
        if raw_type:
            if raw_type in TXN_TYPES:
                types = {raw_type}
            else:
                errors["type"] = "Unknown transaction type."
        d_from = d_to = None
        try:
            d_from = parse_date(args.get("date_from"), "From date")
        except ValidationError as exc:
            errors[exc.field] = exc.message
        try:
            d_to = parse_date(args.get("date_to"), "To date")
        except ValidationError as exc:
            errors[exc.field] = exc.message
        if d_from and d_to and d_from > d_to and "date_from" not in errors:
            errors["date_to"] = "To date cannot be earlier than the From date."
        sort = (args.get("sort") or "newest").strip()
        if sort not in ("newest", "oldest"):
            errors["sort"] = "Sort must be newest or oldest."
            sort = "newest"
        return types, d_from, d_to, sort, errors

    @staticmethod
    def paginate(rows, page, per_page):
        total = len(rows)
        pages = max(1, -(-total // per_page))
        page = min(max(1, page), pages)
        start = (page - 1) * per_page
        return {
            "items": rows[start:start + per_page], "page": page, "pages": pages,
            "total": total, "per_page": per_page,
            "has_prev": page > 1, "has_next": page < pages,
        }

    def get_transaction_by_reference(self, account_number, reference):
        st = self.store.snapshot()
        return [t for t in st["transactions"]
                if t["account_number"] == account_number and t["reference"] == reference]

    # ------------------------------------------------------------------
    # integrity audit
    # ------------------------------------------------------------------
    def audit(self):
        """Return a list of consistency problems (empty list = ledger is consistent)."""
        st = self.store.snapshot()
        return verify_ledger(st)


def verify_ledger(st):
    problems = []
    by_account = {}
    seen_ids = set()
    for t in sorted(st["transactions"], key=lambda t: t["seq"]):
        if t["transaction_id"] in seen_ids:
            problems.append(f"duplicate transaction id {t['transaction_id']}")
        seen_ids.add(t["transaction_id"])
        by_account.setdefault(t["account_number"], []).append(t)
    refs = {}
    for number, acct in st["accounts"].items():
        entries = by_account.get(number, [])
        running = Decimal("0.00")
        for t in entries:
            if D(t["previous_balance"]) != running:
                problems.append(f"{t['transaction_id']}: previous balance does not chain")
            delta = D(t["amount"]) if t["direction"] == "CREDIT" else -D(t["amount"])
            if D(t["new_balance"]) != running + delta:
                problems.append(f"{t['transaction_id']}: new balance arithmetic wrong")
            running = D(t["new_balance"])
        if D(acct["balance"]) != running:
            problems.append(f"account {number}: stored balance != ledger balance")
        if running < 0:
            problems.append(f"account {number}: negative balance")
    for t in st["transactions"]:
        if t["type"] in ("TRANSFER_OUT", "TRANSFER_IN"):
            refs.setdefault(t["reference"], []).append(t)
    for ref, pair in refs.items():
        kinds = sorted(t["type"] for t in pair)
        if kinds != ["TRANSFER_IN", "TRANSFER_OUT"] or pair[0]["amount"] != pair[1]["amount"]:
            problems.append(f"transfer {ref}: unmatched debit/credit")
    return problems
