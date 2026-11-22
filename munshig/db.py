"""Local SQLite store: messages, pending entries, follow-ups, drafts, audit log.

Money is stored as text (Decimal strings) so rupees and paise never pass
through floating point.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    wa_id TEXT UNIQUE,
    jid TEXT NOT NULL,
    jid_alt TEXT,
    from_me INTEGER NOT NULL DEFAULT 0,
    is_group INTEGER NOT NULL DEFAULT 0,
    push_name TEXT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT,
    media_path TEXT,
    mime TEXT,
    file_name TEXT,
    media_sha256 TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_jid ON messages(jid, ts);

CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,
    message_id INTEGER REFERENCES messages(id),
    created_at TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    status TEXT NOT NULL,
    suggested_tag TEXT,
    tag TEXT,
    ledger TEXT,
    party_name TEXT,
    party_gstin TEXT,
    party_jid TEXT,
    invoice_no TEXT,
    invoice_date TEXT,
    taxable TEXT,
    cgst TEXT,
    sgst TEXT,
    igst TEXT,
    cess TEXT,
    round_off TEXT,
    total TEXT,
    utr TEXT,
    direction TEXT,
    source TEXT,
    review_reasons TEXT NOT NULL DEFAULT '[]',
    duplicate_of TEXT,
    extraction_json TEXT,
    tally_remote_id TEXT,
    tally_result TEXT,
    tally_error TEXT,
    approved_at TEXT,
    approved_by TEXT,
    posted_at TEXT
);
CREATE INDEX IF NOT EXISTS entries_status ON entries(status);
CREATE INDEX IF NOT EXISTS entries_invoice ON entries(party_gstin, invoice_no);

CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY,
    jid TEXT NOT NULL,
    message_id INTEGER,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    answer TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    jid TEXT NOT NULL,
    message_id INTEGER,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digest_items (
    digest_id INTEGER NOT NULL REFERENCES digests(id),
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    PRIMARY KEY (digest_id, label)
);

CREATE TABLE IF NOT EXISTS contacts (
    jid TEXT PRIMARY KEY,
    name TEXT,
    do_not_reply INTEGER NOT NULL DEFAULT 0,
    paused_until TEXT,
    greeted INTEGER NOT NULL DEFAULT 0,
    after_hours_on TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ledgers (
    name TEXT PRIMARY KEY,
    parent TEXT,
    gstin TEXT
);

CREATE TABLE IF NOT EXISTS ledger_memory (
    party_key TEXT NOT NULL,
    tag TEXT NOT NULL,
    ledger TEXT NOT NULL,
    PRIMARY KEY (party_key, tag)
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    def backup_to(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(path))
        try:
            with self._lock:
                self.conn.backup(target)
        finally:
            target.close()

    def transaction(self):
        return _Tx(self)

    def audit(self, actor: str, action: str, **detail: Any) -> None:
        self.execute(
            "INSERT INTO audit (ts, actor, action, detail) VALUES (?, ?, ?, ?)",
            (now_iso(), actor, action, json.dumps(detail, default=str)),
        )

    def close(self) -> None:
        self.conn.close()


class _Tx:
    def __init__(self, db: DB):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()
        return False
