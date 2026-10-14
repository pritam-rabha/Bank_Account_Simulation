"""Account types and their documented educational rules."""
from dataclasses import dataclass
from decimal import Decimal

from .utils import indian

SAVINGS = "SAVINGS"
CURRENT = "CURRENT"
FIXED_DEPOSIT = "FIXED_DEPOSIT"

NUMBER_PREFIX = {SAVINGS: "10", CURRENT: "20", FIXED_DEPOSIT: "30"}


@dataclass(frozen=True)
class AccountTypeRule:
    code: str
    label: str
    min_opening_deposit: Decimal
    min_balance: Decimal
    accepts_deposits: bool
    default_rate: Decimal
    summary: str
    rules: tuple


def get_rules(cfg):
    """Build the rule table from configuration (so rates/limits are tunable)."""
    sav_rate = cfg["SAVINGS_INTEREST_RATE"]
    cur_rate = cfg["CURRENT_INTEREST_RATE"]
    sav_min = cfg["SAVINGS_MIN_BALANCE"]
    fd_terms = ", ".join(f"{m} mo @ {r}%" for m, r in cfg["FD_RATES"].items())
    cur_interest_rule = (
        "Earns no interest (configured rate is 0%)."
        if cur_rate == 0
        else f"Earns simple interest at {cur_rate}% p.a. (configured)."
    )
    return {
        SAVINGS: AccountTypeRule(
            SAVINGS,
            "Savings Account",
            cfg["SAVINGS_MIN_OPENING"],
            sav_min,
            True,
            sav_rate,
            "Everyday account that earns simple interest.",
            (
                f"Minimum opening deposit: {indian(cfg['SAVINGS_MIN_OPENING'])}.",
                f"Minimum balance of {indian(sav_min)} must always remain (no overdraft).",
                f"Configurable annual interest, currently {sav_rate}% p.a. (simple interest).",
                "Interest is credited on request for 1-12 months (a simulated time step).",
                "Deposits, withdrawals and transfers allowed while the account is active.",
            ),
        ),
        CURRENT: AccountTypeRule(
            CURRENT,
            "Current Account",
            cfg["CURRENT_MIN_OPENING"],
            Decimal("0.00"),
            True,
            cur_rate,
            "Transactional account for frequent payments.",
            (
                f"Minimum opening deposit: {indian(cfg['CURRENT_MIN_OPENING'])}.",
                "No minimum balance, but no overdraft: the balance can never go below zero.",
                cur_interest_rule,
                "Deposits, withdrawals and transfers allowed while the account is active.",
            ),
        ),
        FIXED_DEPOSIT: AccountTypeRule(
            FIXED_DEPOSIT,
            "Fixed Deposit Account",
            cfg["FD_MIN_OPENING"],
            Decimal("0.00"),
            False,
            Decimal("0.00"),
            "Lump sum locked for a fixed term at a fixed rate.",
            (
                f"Minimum opening deposit: {indian(cfg['FD_MIN_OPENING'])} (the principal).",
                f"Choose a term at opening. Rates by term: {fd_terms}.",
                "Simplified interest: simple interest for the full term = Principal x Rate x Months / 1200.",
                "Interest is credited once, only after the maturity date has passed.",
                "No further deposits or incoming transfers; no withdrawals or outgoing transfers before maturity.",
            ),
        ),
    }
