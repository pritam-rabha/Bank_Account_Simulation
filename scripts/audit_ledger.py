"""Check the ledger for inconsistencies:  python scripts/audit_ledger.py [path/to/bank_data.json]

Verifies that every account balance equals its ledger history, that previous/new
balances chain correctly, that no balance is negative and that every transfer has
a matching debit and credit.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BASE_DIR  # noqa: E402
from app.services import verify_ledger  # noqa: E402
from app.storage import JsonStore  # noqa: E402

path = sys.argv[1] if len(sys.argv) > 1 else str(BASE_DIR / "data" / "bank_data.json")
state = JsonStore(path).snapshot()
problems = verify_ledger(state)
print(f"{len(state['accounts'])} accounts, {len(state['transactions'])} transactions")
if problems:
    print("PROBLEMS FOUND:")
    for p in problems:
        print(" -", p)
    sys.exit(1)
print("Ledger is consistent.")
