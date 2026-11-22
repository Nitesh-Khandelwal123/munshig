"""Build the end-of-day digest the owner gets on WhatsApp."""

from __future__ import annotations

from decimal import Decimal

from .db import DB, now_iso
from .models import TAGS, Entry, open_entries
from .parties import resolve_party


def inr(amount: Decimal | None) -> str:
    """Format with Indian digit grouping: 1,23,456.50 (paise only when present)."""
    if amount is None:
        return "₹?"
    sign = "-" if amount < 0 else ""
    amount = abs(amount).quantize(Decimal("0.01"))
    rupees, paise = f"{amount:.2f}".split(".")
    if len(rupees) > 3:
        head, tail = rupees[:-3], rupees[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        rupees = ",".join(groups + [tail])
    return f"{sign}₹{rupees}" + (f".{paise}" if paise != "00" else "")


DOC_LABEL = {
    "invoice": "Inv",
    "credit_note": "Credit note",
    "debit_note": "Debit note",
    "payment": "UPI",
    "amount": "Chat",
    "not_a_bill": "Not a bill",
}


def entry_line(db: DB, number: int, e: Entry) -> str:
    who = e.party_name or "Unknown party"
    bits = [f"{number}. {who}"]
    label = DOC_LABEL.get(e.doc_type, e.doc_type)
    if e.invoice_no:
        label += f" for bill {e.invoice_no}" if e.doc_type in ("payment", "amount") else f" {e.invoice_no}"
    bits.append(label)
    amount = inr(e.total)
    if e.tax_total:
        amount += f" (GST {inr(e.tax_total)})"
    bits.append(amount)
    if e.invoice_date:
        bits.append(f"{e.invoice_date:%d %b}")
    if e.suggested_tag:
        bits.append(f"suggest {e.suggested_tag}")
    line = "  ".join(bits)
    notes = []
    if e.status == "failed" and e.tally_error:
        notes.append(f"NOT POSTED: {e.tally_error}")
    if e.review_reasons:
        notes.append("check: " + "; ".join(e.review_reasons))
    if e.suggested_tag in ("S", "P", "R", "PY") and e.party_name:
        match = resolve_party(db, e.party_name, e.party_gstin)
        if match.how == "new":
            notes.append("new party, will be created in Tally")
        elif match.how == "close_name":
            notes.append(f"will post to Tally ledger '{match.ledger}'")
    if notes:
        line += "\n   " + "\n   ".join(notes)
    return line


def build_digest(db: DB) -> tuple[str, int | None]:
    """Build and store a digest. Returns (text, digest id); id None if nothing to report."""
    entries = open_entries(db)
    followups = db.all("SELECT * FROM followups WHERE status = 'open' ORDER BY id")
    drafts = db.all("SELECT * FROM drafts WHERE status = 'waiting' ORDER BY id")
    posted_today = db.one("SELECT count(*) AS n FROM entries WHERE status = 'posted' AND posted_at >= date('now')")
    if not entries and not followups and not drafts:
        return "Nothing waiting for you today.", None

    lines = [f"MunshiG: {len(entries)} entries, {len(followups)} follow-ups"
             + (f", {len(drafts)} drafts" if drafts else "")]
    if posted_today and posted_today["n"]:
        lines.append(f"({posted_today['n']} posted to Tally today)")
    last = db.one("SELECT created_at FROM digests ORDER BY id DESC LIMIT 1")
    dups = db.one("SELECT count(*) AS n FROM entries WHERE status = 'duplicate' AND created_at > ?",
                  (last["created_at"] if last else "",))
    if dups and dups["n"]:
        lines.append(f"({dups['n']} duplicate{'s' if dups['n'] > 1 else ''} skipped: same invoice sent again)")
    items: list[tuple[str, str, str]] = []
    if entries:
        lines.append("")
        for i, e in enumerate(entries, 1):
            lines.append(entry_line(db, i, e))
            items.append((str(i), "entry", e.id))
    if followups:
        lines.append("")
        lines.append("Follow-ups")
        for f in followups:
            lines.append(f"F{f['id']}. {f['question']}")
    if drafts:
        lines.append("")
        lines.append("Drafts waiting")
        for d in drafts:
            lines.append(f"D{d['id']}. {d['text']}")
    lines.append("")
    if entries:
        example = " ".join(f"{i}{e.suggested_tag or 'P'}" for i, e in list(enumerate(entries, 1))[:4])
        lines.append(f"Reply like: {example}   or ALL to accept suggestions")
    if followups:
        lines.append(f"Answer like: F{followups[0]['id']} <your answer>")
    lines.append("Tags: " + ", ".join(f"{k} {v.lower()}" for k, v in TAGS.items()))
    text = "\n".join(lines)

    with db.transaction():
        cur = db.execute("INSERT INTO digests (created_at, text) VALUES (?, ?)", (now_iso(), text))
        digest_id = cur.lastrowid
        for label, kind, ref in items:
            db.execute("INSERT INTO digest_items (digest_id, label, kind, ref) VALUES (?, ?, ?, ?)",
                       (digest_id, label, kind, ref))
    return text, digest_id


def latest_entry_for(db: DB, number: int) -> str | None:
    row = db.one(
        "SELECT ref FROM digest_items WHERE label = ? AND kind = 'entry' "
        "AND digest_id = (SELECT max(id) FROM digests)", (str(number),))
    return row["ref"] if row else None
