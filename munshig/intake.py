"""Turn what arrived on WhatsApp into pending entries with a suggested tag.

Nothing here talks to Tally. Every entry waits for the owner's tag in the
daily digest.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .config import Settings
from .db import DB
from .extract import Extraction
from .gst import check_tax_split, is_interstate, is_valid_gstin, normalize_gstin
from .models import Entry, save_entry
from .money import MoneyMention
from .parties import name_key


@dataclass
class Sender:
    jid: str
    name: str | None
    from_me: bool  # sent from the business phone itself (owner or staff)


def _same_business(name: str | None, business: str) -> bool:
    a, b = name_key(name), name_key(business)
    if not a or not b:
        return False
    return a == b or a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _check_common(entry: Entry, today: date) -> None:
    if entry.total is None:
        entry.review_reasons.append("total amount not found")
    if entry.party_gstin and not is_valid_gstin(entry.party_gstin):
        entry.review_reasons.append(f"GSTIN {entry.party_gstin} fails the GSTIN check digit")
    if entry.doc_type == "invoice":
        if not entry.invoice_date:
            entry.review_reasons.append("invoice date not found")
        elif entry.invoice_date > today + timedelta(days=1):
            entry.review_reasons.append(f"invoice date {entry.invoice_date:%d %b %Y} is in the future")
        elif entry.invoice_date < today - timedelta(days=550):
            entry.review_reasons.append(f"invoice date {entry.invoice_date:%d %b %Y} is over 18 months old")
        if not entry.invoice_no:
            entry.review_reasons.append("invoice number not found")


def _find_duplicate(db: DB, entry: Entry, media_sha256: str | None) -> str | None:
    if entry.doc_type in ("payment", "amount"):
        # Several payments can settle one bill, so payments match on the UPI reference only.
        if media_sha256:
            row = db.one("SELECT e.id FROM entries e JOIN messages m ON m.id = e.message_id "
                         "WHERE m.media_sha256 = ? AND e.id != ? AND e.status != 'ignored' LIMIT 1",
                         (media_sha256, entry.id))
            if row:
                return row["id"]
        if entry.utr:
            row = db.one("SELECT id FROM entries WHERE utr = ? AND id != ? AND status != 'ignored' "
                         "AND doc_type IN ('payment', 'amount') LIMIT 1", (entry.utr, entry.id))
            if row:
                return row["id"]
        return None
    if media_sha256:
        row = db.one(
            "SELECT e.id FROM entries e JOIN messages m ON m.id = e.message_id "
            "WHERE m.media_sha256 = ? AND e.id != ? AND e.status NOT IN ('ignored') LIMIT 1",
            (media_sha256, entry.id))
        if row:
            return row["id"]
    if entry.invoice_no and (entry.party_gstin or entry.party_name):
        inv = entry.invoice_no.strip().upper()
        if entry.party_gstin:
            row = db.one("SELECT id FROM entries WHERE upper(trim(invoice_no)) = ? AND party_gstin = ? "
                         "AND doc_type = ? AND id != ? AND status NOT IN ('ignored') LIMIT 1",
                         (inv, entry.party_gstin, entry.doc_type, entry.id))
        else:
            row = db.one("SELECT id FROM entries WHERE upper(trim(invoice_no)) = ? AND party_name = ? "
                         "AND doc_type = ? AND id != ? AND status NOT IN ('ignored') LIMIT 1",
                         (inv, entry.party_name, entry.doc_type, entry.id))
        if row:
            return row["id"]
    return None


def _finish(db: DB, entry: Entry, media_sha256: str | None) -> Entry:
    dup = _find_duplicate(db, entry, media_sha256)
    if dup:
        entry.duplicate_of = dup
        entry.status = "duplicate"
    elif entry.review_reasons:
        entry.status = "needs_review"
    else:
        entry.status = "pending"
    return save_entry(db, entry)


def entry_from_document(db: DB, settings: Settings, ex: Extraction, sender: Sender,
                        message_id: int | None, media_sha256: str | None,
                        today: date | None = None, when: date | None = None) -> Entry:
    """`when` is the date the message arrived, used for payments that show no date."""
    today = today or date.today()
    ours = normalize_gstin(settings.business.gstin)
    business = settings.business.name
    entry = Entry(doc_type=ex.doc_type if ex.doc_type != "unknown" else "invoice",
                  message_id=message_id, party_jid=sender.jid, source=ex.source,
                  extraction_json=ex.to_json(), invoice_no=ex.invoice_no, invoice_date=ex.invoice_date,
                  taxable=ex.taxable, cgst=ex.cgst, sgst=ex.sgst, igst=ex.igst, cess=ex.cess,
                  total=ex.total, utr=ex.utr)
    entry.review_reasons.extend(ex.warnings)

    if ex.doc_type == "not_a_bill":
        entry.suggested_tag = "X"
        entry.party_name = ex.seller_name or sender.name
        kind = ex.not_a_bill_kind or "quotation, estimate or challan"
        entry.review_reasons.append(f"looks like a {kind}, not a bill")
        return _finish(db, entry, media_sha256)

    if ex.doc_type == "payment":
        entry.doc_type = "payment"
        status = (ex.payment_status or "success").lower()
        if status != "success":
            entry.suggested_tag = "X"
            entry.review_reasons.append(f"payment shows as {status}")
        elif _same_business(ex.payee_name, business):
            entry.suggested_tag, entry.direction, entry.party_name = "R", "in", ex.payer_name or sender.name
        elif _same_business(ex.payer_name, business):
            entry.suggested_tag, entry.direction, entry.party_name = "PY", "out", ex.payee_name or sender.name
        elif sender.from_me:
            entry.suggested_tag, entry.direction, entry.party_name = "PY", "out", ex.payee_name or sender.name
        else:
            entry.suggested_tag, entry.direction, entry.party_name = "R", "in", sender.name or ex.payer_name
        entry.invoice_no = ex.against_bill  # the bill this payment settles, if the note says
        entry.invoice_date = entry.invoice_date or when
        _check_common(entry, today)
        return _finish(db, entry, media_sha256)

    if ex.doc_type in ("credit_note", "debit_note"):
        entry.party_name = ex.seller_name if ex.buyer_gstin == ours else ex.buyer_name or ex.seller_name
        entry.party_gstin = ex.seller_gstin if ex.buyer_gstin == ours else ex.buyer_gstin
        entry.review_reasons.append(f"{ex.doc_type.replace('_', ' ')}s aren't supported yet; post this one by hand")
        return _finish(db, entry, media_sha256)

    # Tax invoice: work out whether it's our sale or our purchase.
    if ours and ex.seller_gstin == ours:
        sale = True
    elif ours and ex.buyer_gstin == ours:
        sale = False
    elif _same_business(ex.seller_name, business):
        sale = True
    elif _same_business(ex.buyer_name, business):
        sale = False
    else:
        sale = sender.from_me
        entry.review_reasons.append("couldn't tell from the GSTINs or names whether this is a sale or a purchase")
    if sale:
        entry.suggested_tag, entry.direction = "S", "out"
        entry.party_name, entry.party_gstin = ex.buyer_name or sender.name, ex.buyer_gstin
    else:
        entry.suggested_tag, entry.direction = "P", "in"
        entry.party_name, entry.party_gstin = ex.seller_name or sender.name, ex.seller_gstin

    check = check_tax_split(ex.taxable, ex.cgst, ex.sgst, ex.igst, ex.cess, ex.total,
                            is_interstate(ex.seller_gstin, ex.buyer_gstin))
    entry.review_reasons.extend(r for r in check.reasons if r != "total amount not found")
    entry.round_off = check.round_off if check.round_off else None
    _check_common(entry, today)
    return _finish(db, entry, media_sha256)


def entries_from_text(db: DB, mentions: list[MoneyMention], sender: Sender,
                      message_id: int | None, text: str, when: date | None = None) -> list[Entry]:
    """A typed amount becomes one entry (the largest amount in the message)."""
    if not mentions:
        return []
    top = mentions[0]
    entry = Entry(doc_type="amount", message_id=message_id, party_jid=sender.jid,
                  party_name=sender.name, total=top.amount, utr=top.utr, source="text", invoice_date=when,
                  extraction_json=json.dumps({"text": text[:500], "snippet": top.snippet}))
    # Direction is stated from the writer's side; flip it to the business's side.
    if top.direction == "paid":
        entry.suggested_tag = "PY" if sender.from_me else "R"
    elif top.direction == "received":
        entry.suggested_tag = "R" if sender.from_me else "PY"
    else:
        entry.review_reasons.append("not clear whether this money came in or went out")
    if len(mentions) > 1:
        others = ", ".join(f"₹{m.amount:,.2f}" for m in mentions[1:4])
        entry.review_reasons.append(f"message also mentions {others}")
    entry.direction = {"R": "in", "PY": "out"}.get(entry.suggested_tag or "")
    return [_finish(db, entry, None)]


def money_total(entry: Entry) -> Decimal:
    return entry.total or Decimal("0")
