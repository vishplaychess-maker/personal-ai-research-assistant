"""MP-4C - the durable REPLAY GUARD for executor grants.

A single broker key plus a persistent SQLite volume. Consuming a jti is an atomic
INSERT: exactly one caller wins, and the record survives a process restart on the
same volume, so the SAME signed request is refused after a restart too.

This is a replay guard ONLY. It is NOT an approval system and holds no authority:
the application-side DispatchRecord remains the execution authority.

Content: a SHA-256 of the jti is stored, never the jti verbatim, and never the
code, a token, or any tool output. An unavailable or corrupt store raises
NonceStoreError so the caller fails CLOSED with zero starts.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from typing import List, Optional

#: Named-volume mount point; must be writable by the service user (the image
#: pre-creates it with the right ownership so a fresh volume inherits it).
DEFAULT_PATH = "/var/lib/executor/nonces.db"
PATH_ENV = "EXECUTOR_SERVICE_NONCE_DB"

_SCHEMA = """CREATE TABLE IF NOT EXISTS nonces (
    jti_hash   TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
)"""


class NonceStoreError(Exception):
    """The replay guard could not be used. Callers MUST fail closed."""


def _hash(jti: str) -> str:
    return hashlib.sha256(("mp4c-nonce/v1\x00" + str(jti)).encode("utf-8")).hexdigest()


class NonceStore:
    """Atomic, durable, single-broker nonce consumption."""

    def __init__(self, path: Optional[str] = None, *, timeout_s: float = 10.0) -> None:
        self.path = path or (os.environ.get(PATH_ENV) or DEFAULT_PATH)
        self._lock = threading.Lock()
        self._conn = None
        try:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent and not os.path.isdir(parent):
                raise NonceStoreError("nonce store directory is unavailable")
            self._conn = sqlite3.connect(self.path, timeout=timeout_s,
                                         check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute(_SCHEMA)
            self._conn.commit()
            self._verify_schema()
        except NonceStoreError:
            self._discard()
            raise
        except Exception:  # noqa: BLE001 - any storage failure is fail-closed
            self._discard()
            raise NonceStoreError("nonce store unavailable") from None

    def _discard(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _verify_schema(self) -> None:
        rows = self._conn.execute("PRAGMA table_info(nonces)").fetchall()
        names = {row[1] for row in rows}
        if names != {"jti_hash", "expires_at", "created_at"}:
            raise NonceStoreError("nonce store schema is not the expected shape")

    def _require(self):
        if self._conn is None:
            raise NonceStoreError("nonce store is not open")
        return self._conn

    def columns(self) -> List[str]:
        with self._lock:
            self._require()
            return [row[1] for row in
                    self._conn.execute("PRAGMA table_info(nonces)").fetchall()]

    def consume(self, jti: str, *, expires_at: int, now: int) -> bool:
        """Atomically consume jti. True = first use; False = REPLAY."""
        if not isinstance(jti, str) or not jti.strip():
            raise NonceStoreError("nonce store received an empty jti")
        with self._lock:
            conn = self._require()
            try:
                conn.execute(
                    "INSERT INTO nonces (jti_hash, expires_at, created_at)"
                    " VALUES (?, ?, ?)",
                    (_hash(jti), int(expires_at), int(now)))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False
            except Exception:  # noqa: BLE001
                raise NonceStoreError("nonce store write failed") from None

    def seen(self, jti: str) -> bool:
        """NON-DESTRUCTIVE: has this jti already been consumed?

        Used ONLY to CLASSIFY a refusal that has already decided not to consume
        anything. With no live reservation, an already-consumed jti is a REPLAY,
        while an unseen jti is a genuine capacity miss whose grant must stay
        unburned and retryable. This never consumes, never inserts and never
        deletes.
        """
        if not isinstance(jti, str) or not jti.strip():
            raise NonceStoreError("nonce store received an empty jti")
        with self._lock:
            conn = self._require()
            try:
                row = conn.execute(
                    "SELECT 1 FROM nonces WHERE jti_hash = ? LIMIT 1",
                    (_hash(jti),)).fetchone()
                return row is not None
            except Exception:  # noqa: BLE001 - a failed read is fail-closed
                raise NonceStoreError("nonce store read failed") from None

    def prune(self, *, now: int, skew: int) -> int:
        """Delete records past expiry + skew. Returns the number removed."""
        with self._lock:
            conn = self._require()
            try:
                cur = conn.execute("DELETE FROM nonces WHERE expires_at < ?",
                                   (int(now) - int(skew),))
                conn.commit()
                return int(cur.rowcount or 0)
            except Exception:  # noqa: BLE001
                raise NonceStoreError("nonce store prune failed") from None

    def count(self) -> int:
        with self._lock:
            conn = self._require()
            try:
                return int(conn.execute("SELECT COUNT(*) FROM nonces").fetchone()[0])
            except Exception:  # noqa: BLE001
                raise NonceStoreError("nonce store read failed") from None

    def close(self) -> None:
        with self._lock:
            self._discard()


def store_from_env(env=None) -> NonceStore:
    """Build the store from EXECUTOR_SERVICE_NONCE_DB (default: the mount point)."""
    e = env if env is not None else os.environ
    return NonceStore(str(e.get(PATH_ENV) or DEFAULT_PATH))


__all__ = ["NonceStore", "NonceStoreError", "DEFAULT_PATH", "PATH_ENV",
           "store_from_env"]
