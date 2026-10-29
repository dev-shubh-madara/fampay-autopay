"""Tiny thread-safe SQLite wrapper. One file, no server needed."""

import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id            TEXT PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    customer_ref  TEXT,
    product       TEXT,
    description   TEXT,
    amount_paise  INTEGER NOT NULL,
    status        TEXT NOT NULL,          -- pending | paid | cancelled
    claimed_utr   TEXT,
    payment_id    INTEGER,
    metadata      TEXT,
    created_at    INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    paid_at       INTEGER,
    webhook_state TEXT                    -- null | sent | failed
);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_ref, status);
CREATE INDEX IF NOT EXISTS idx_orders_utr ON orders(claimed_utr);

CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    TEXT NOT NULL UNIQUE,
    utr           TEXT,
    amount_paise  INTEGER,
    code          TEXT,
    payer         TEXT,
    received_at   INTEGER NOT NULL,
    status        TEXT NOT NULL,          -- unmatched | matched | underpaid
    order_id      TEXT,
    note          TEXT
);
-- A UTR can exist only once, ever. This is what stops re-use scams.
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_utr ON payments(utr) WHERE utr IS NOT NULL;

CREATE TABLE IF NOT EXISTS processed_emails (
    message_id    TEXT PRIMARY KEY,
    from_addr     TEXT,
    subject       TEXT,
    result        TEXT,
    processed_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    @contextmanager
    def tx(self):
        """BEGIN IMMEDIATE ... COMMIT. Everything inside is atomic."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def get_kv(self, k: str) -> str | None:
        row = self.fetchone("SELECT v FROM kv WHERE k=?", (k,))
        return row["v"] if row else None

    def set_kv(self, k: str, v: str) -> None:
        self.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, v))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
