"""Match a party on an invoice to a ledger in Tally.

Order: same GSTIN, then the same name ignoring case and punctuation, then a
close name match. If nothing matches, the party is proposed as a new ledger
and the owner sees that in the digest before approving.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .db import DB
from .gst import normalize_gstin

_SUFFIXES = re.compile(r"\b(m/s|messrs|pvt|private|ltd|limited|llp|co|company|and|&)\b", re.IGNORECASE)


def name_key(name: str | None) -> str:
    if not name:
        return ""
    cleaned = _SUFFIXES.sub(" ", name.lower())
    return re.sub(r"[^a-z0-9]", "", cleaned)


@dataclass
class PartyMatch:
    ledger: str | None  # existing Tally ledger, if found
    proposed_name: str | None  # name to create when no ledger exists
    how: str  # gstin, name, close_name, new, missing


def resolve_party(db: DB, name: str | None, gstin: str | None) -> PartyMatch:
    gstin = normalize_gstin(gstin)
    if gstin:
        row = db.one("SELECT name FROM ledgers WHERE upper(gstin) = ?", (gstin,))
        if row:
            return PartyMatch(row["name"], None, "gstin")
    if not name:
        return PartyMatch(None, None, "missing")

    key = name_key(name)
    rows = db.all("SELECT name FROM ledgers")
    by_key = {name_key(r["name"]): r["name"] for r in rows}
    if key and key in by_key:
        return PartyMatch(by_key[key], None, "name")
    close = difflib.get_close_matches(key, list(by_key), n=1, cutoff=0.88) if key else []
    if close:
        return PartyMatch(by_key[close[0]], None, "close_name")
    return PartyMatch(None, name.strip(), "new")


def refresh_ledger_cache(db: DB, ledgers) -> int:
    with db.transaction():
        db.execute("DELETE FROM ledgers")
        for ld in ledgers:
            db.execute("INSERT OR REPLACE INTO ledgers (name, parent, gstin) VALUES (?, ?, ?)",
                       (ld.name, ld.parent, normalize_gstin(ld.gstin)))
    return len(ledgers)


def remember_ledger(db: DB, party: str | None, tag: str, ledger: str) -> None:
    if party:
        db.execute("INSERT OR REPLACE INTO ledger_memory (party_key, tag, ledger) VALUES (?, ?, ?)",
                   (name_key(party), tag, ledger))


def remembered_ledger(db: DB, party: str | None, tag: str) -> str | None:
    if not party:
        return None
    row = db.one("SELECT ledger FROM ledger_memory WHERE party_key = ? AND tag = ?", (name_key(party), tag))
    return row["ledger"] if row else None


def ledger_exists(db: DB, name: str) -> bool:
    return db.one("SELECT 1 FROM ledgers WHERE lower(name) = lower(?)", (name,)) is not None
