"""The pending-entry record and its life cycle.

Status flow:
    pending / needs_review  -> approved -> posted
                                        -> failed (back into the next digest)
    any open status         -> ignored  (owner tagged X)
    any open status         -> duplicate (same invoice seen before)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal

from .db import DB, now_iso

OPEN_STATUSES = ("pending", "needs_review", "failed")

TAGS = {
    "S": "Sales",
    "P": "Purchase",
    "E": "Expense",
    "A": "Asset",
    "L": "Liability",
    "R": "Receipt",
    "PY": "Payment",
    "X": "Ignore",
}

_MONEY_FIELDS = ("taxable", "cgst", "sgst", "igst", "cess", "round_off", "total")


@dataclass
class Entry:
    doc_type: str  # invoice, credit_note, debit_note, payment, amount, not_a_bill
    status: str = "pending"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    message_id: int | None = None
    created_at: str = field(default_factory=now_iso)
    suggested_tag: str | None = None
    tag: str | None = None
    ledger: str | None = None
    party_name: str | None = None
    party_gstin: str | None = None
    party_jid: str | None = None
    invoice_no: str | None = None
    invoice_date: date | None = None
    taxable: Decimal | None = None
    cgst: Decimal | None = None
    sgst: Decimal | None = None
    igst: Decimal | None = None
    cess: Decimal | None = None
    round_off: Decimal | None = None
    total: Decimal | None = None
    utr: str | None = None
    direction: str | None = None  # "in" (money/bill to the business) or "out"
    source: str | None = None  # qr, pdf_text, vision, text
    review_reasons: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    extraction_json: str | None = None
    tally_remote_id: str | None = None
    tally_result: str | None = None
    tally_error: str | None = None
    approved_at: str | None = None
    approved_by: str | None = None
    posted_at: str | None = None

    @property
    def tax_total(self) -> Decimal:
        return sum((v or Decimal("0") for v in (self.cgst, self.sgst, self.igst, self.cess)), Decimal("0"))

    @property
    def has_tax_breakup(self) -> bool:
        return self.taxable is not None

    def to_row(self) -> dict:
        row = asdict(self)
        for k in _MONEY_FIELDS:
            row[k] = None if row[k] is None else str(row[k])
        row["invoice_date"] = self.invoice_date.isoformat() if self.invoice_date else None
        row["review_reasons"] = json.dumps(self.review_reasons)
        return row

    @classmethod
    def from_row(cls, row) -> "Entry":
        data = dict(row)
        for k in _MONEY_FIELDS:
            data[k] = None if data.get(k) is None else Decimal(data[k])
        data["invoice_date"] = date.fromisoformat(data["invoice_date"]) if data.get("invoice_date") else None
        data["review_reasons"] = json.loads(data.get("review_reasons") or "[]")
        return cls(**data)


def save_entry(db: DB, entry: Entry) -> Entry:
    row = entry.to_row()
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "id")
    db.execute(f"INSERT INTO entries ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}",
               row.values())
    return entry


def get_entry(db: DB, entry_id: str) -> Entry | None:
    row = db.one("SELECT * FROM entries WHERE id = ?", (entry_id,))
    return Entry.from_row(row) if row else None


def open_entries(db: DB) -> list[Entry]:
    marks = ", ".join("?" for _ in OPEN_STATUSES)
    rows = db.all(f"SELECT * FROM entries WHERE status IN ({marks}) ORDER BY rowid", OPEN_STATUSES)
    return [Entry.from_row(r) for r in rows]
