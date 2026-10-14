"""JSON file storage with an all-or-nothing transaction boundary.

How atomicity is achieved
-------------------------
``JsonStore.transaction()`` is a context manager that

1. takes an in-process ``threading.Lock`` (serialises threads), then
2. takes an OS-level exclusive file lock (serialises *processes*, e.g. several
   Gunicorn workers),
3. loads the *entire* state fresh from disk,
4. lets the caller mutate the in-memory dictionary, and
5. on a clean exit writes the whole new state to a temporary file in the same
   directory, ``fsync``s it and swaps it in with ``os.replace`` (atomic on
   POSIX and Windows).

If the body raises, nothing is written - every mutation made inside the block
(for example the debit half of a transfer) is simply discarded.  Because a
transfer changes both accounts and appends both ledger entries in ONE
in-memory state and commits ONE file replacement, the debit and the credit
either both reach the disk or neither does.

Known limits (documented in the README): a single host/filesystem is assumed
(no NFS), the whole file is rewritten on every commit (fine for a classroom
demo, not for scale), and there is no write-ahead log or point-in-time
recovery beyond the single ``.bak`` copy kept of the previous version.
"""
import contextlib
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from .errors import StorageError

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows
    import msvcrt
except ImportError:
    msvcrt = None

SCHEMA_VERSION = 1


def empty_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {"next_seq": 1},
        "accounts": {},
        "transactions": [],
        "throttle": {},
        "idempotency": {},
    }


class _FileLock:
    """Exclusive advisory lock on a sidecar ``.lock`` file."""

    def __init__(self, path):
        self.path = path
        self._fh = None

    def acquire(self):
        self._fh = open(self.path, "a+b")
        if fcntl is not None:
            fcntl.flock(self._fh, fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows only, untested here
            self._fh.seek(0)
            while True:
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.02)

    def release(self):
        try:
            if fcntl is not None:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._fh.close()
            self._fh = None


class JsonStore:
    def __init__(self, path):
        self.path = Path(path)
        self.backup_path = self.path.with_name(self.path.name + ".bak")
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.Lock()
        self._local = threading.local()

    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def transaction(self):
        """Read-modify-write block; commits only if the body finishes cleanly."""
        if getattr(self._local, "active", False):
            raise StorageError("Nested storage transactions are not supported.")
        with self._thread_lock:
            flock = _FileLock(self.lock_path)
            flock.acquire()
            self._local.active = True
            try:
                state = self._load()
                before = json.dumps(state, sort_keys=True)
                yield state  # an exception here skips the commit below
                if json.dumps(state, sort_keys=True) != before:
                    self._commit(state)
            finally:
                self._local.active = False
                flock.release()

    def snapshot(self):
        """Return a consistent copy of the current state (never persisted)."""
        with self._thread_lock:
            flock = _FileLock(self.lock_path)
            flock.acquire()
            try:
                return self._load()
            finally:
                flock.release()

    # ------------------------------------------------------------------
    def _load(self):
        if not self.path.exists():
            return empty_state()
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except json.JSONDecodeError as exc:
            raise StorageError(
                f"Data file {self.path.name} is corrupted ({exc}). "
                f"A previous version may be available as {self.backup_path.name}."
            ) from exc
        except OSError as exc:
            raise StorageError(f"Cannot read data file: {exc}") from exc
        if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
            raise StorageError("Data file has an unsupported format or schema version.")
        for key, default in empty_state().items():
            state.setdefault(key, default)
        return state

    def _commit(self, state):
        payload = json.dumps(state, indent=2, ensure_ascii=False)
        tmp_name = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            if self.path.exists():
                shutil.copy2(self.path, self.backup_path)
            os.replace(tmp_name, self.path)
            tmp_name = None
        except OSError as exc:
            raise StorageError(f"Could not save data safely: {exc}") from exc
        finally:
            if tmp_name and os.path.exists(tmp_name):
                with contextlib.suppress(OSError):
                    os.remove(tmp_name)
