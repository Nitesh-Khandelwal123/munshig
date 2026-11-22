"""Turn an approved entry into a Tally voucher and post it.

Voucher rules (accounting-invoice mode, no stock items):

    S  Sales     Dr customer (total)          Cr sales (taxable), output GST, round-off
    P  Purchase  Dr purchase (taxable), input GST, round-off   Cr supplier (total)
    E  Expense   with a bill:   Purchase voucher, Dr expense ledger + input GST, Cr supplier
                 without a bill: Payment voucher, Dr expense ledger, Cr cash/bank
    A  Asset     same as Expense, with a fixed-asset ledger
    L  Liability Receipt voucher, Dr bank, Cr the liability ledger the owner names
    R  Receipt   Receipt voucher, Dr bank, Cr customer (against the bill when known)
    PY Payment   Payment voucher, Dr supplier, Cr bank (against the bill when known)

Expenses and assets with a GST bill go through a Purchase voucher so they show
up in Tally's GST reports with their input tax.
"""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .config import Settings
from .db import DB, now_iso
from .gst import state_of
from .models import Entry, save_entry
from .parties import ledger_exists, remember_ledger, remembered_ledger, resolve_party
from .tally.client import TallyUnavailable
from .tally.xml import BillRef, Line, Voucher

EDU_ALLOWED_DAYS = (1, 2, 31)
ZERO = Decimal("0")


class CannotPost(Exception):
    """The entry can't be posted as tagged; the message is shown to the owner."""


def edu_mode_date(d: date, policy: str) -> tuple[date, str | None]:
    """Educational mode only accepts the 1st, 2nd or 31st. Returns (date, note)."""
    if d.day in EDU_ALLOWED_DAYS:
        return d, None
    if policy == "refuse":
        raise CannotPost(
            f"Tally's free educational mode only accepts dates on the 1st, 2nd or 31st; "
            f"this entry is dated {d:%d %b %Y}."
        )
    last = calendar.monthrange(d.year, d.month)[1]
    allowed = [day for day in EDU_ALLOWED_DAYS if day <= last]
    best = min(allowed, key=lambda day: abs(day - d.day))
    moved = d.replace(day=best)
    return moved, f"Actual date {d:%d-%m-%Y} (moved for Tally educational mode)"


@dataclass
class Resolved:
    party_ledger: str | None
    new_party: str | None  # a party ledger that has to be created first
    main_ledger: str | None  # expense / asset / liability / sales / purchase ledger


def _main_ledger(db: DB, settings: Settings, entry: Entry, tag: str) -> str | None:
    lg = settings.ledgers
    if entry.ledger:
        return entry.ledger
    remembered = remembered_ledger(db, entry.party_name, tag)
    if remembered:
        return remembered
    return {
        "S": lg.sales,
        "P": lg.purchase,
        "E": lg.default_expense,
        "A": lg.default_asset or None,
        "L": None,
    }.get(tag)


def resolve(db: DB, settings: Settings, entry: Entry, tag: str) -> Resolved:
    needs_party = tag in ("S", "P", "R", "PY") or (tag in ("E", "A") and entry.party_name and entry.invoice_no)
    party_ledger = new_party = None
    if needs_party:
        match = resolve_party(db, entry.party_name, entry.party_gstin)
        if match.how == "missing":
            raise CannotPost("No party name on this entry. Open it in the dashboard to add one.")
        party_ledger, new_party = match.ledger, match.proposed_name
    main = _main_ledger(db, settings, entry, tag)
    if tag in ("A", "L") and not main:
        what = "fixed-asset" if tag == "A" else "loan or liability"
        raise CannotPost(f"Which {what} ledger should this go to? Reply with the entry number, "
                         f"{tag} and the ledger name, e.g. '3 {tag} Furniture'.")
    return Resolved(party_ledger, new_party, main)


def build_voucher(settings: Settings, entry: Entry, tag: str, res: Resolved) -> Voucher:
    lg = settings.ledgers
    total = entry.total
    if total is None or total <= 0:
        raise CannotPost("No amount on this entry.")
    party = res.party_ledger or res.new_party
    vdate = entry.invoice_date or date.today()
    notes: list[str] = []
    if settings.tally.educational_mode:
        vdate, note = edu_mode_date(vdate, settings.tally.edu_date_policy)
        if note:
            notes.append(note)

    taxable = entry.taxable if entry.has_tax_breakup else total
    round_off = entry.round_off or ZERO
    bank = lg.bank
    paid_from = lg.cash if lg.paid_from == "cash" else lg.bank
    lines: list[Line] = []

    def taxes(sign: int, prefix: str) -> None:
        names = {
            "cgst": getattr(lg, f"{prefix}_cgst"),
            "sgst": getattr(lg, f"{prefix}_sgst"),
            "igst": getattr(lg, f"{prefix}_igst"),
        }
        for key, ledger in names.items():
            value = getattr(entry, key)
            if value:
                lines.append(Line(ledger, sign * value))
        if entry.cess:
            raise CannotPost("Invoices with GST cess aren't supported yet; post this one by hand.")

    # For S/P/E/A the invoice number is the voucher's reference and opens a new bill.
    # For R/PY it names the bill being settled; the UPI reference becomes the voucher's reference.
    bill_no = entry.invoice_no
    ref = bill_no if tag in ("S", "P", "E", "A") else entry.utr
    new_bill = BillRef(bill_no, "New Ref") if bill_no else None
    against_bill = BillRef(bill_no, "Agst Ref") if bill_no else None

    if tag == "S":
        vtype = "Sales"
        lines.append(Line(party, total, is_party=True, bill=new_bill))
        lines.append(Line(res.main_ledger, -taxable))
        taxes(-1, "output")
        if round_off:
            lines.append(Line(lg.round_off, -round_off))
    elif tag in ("P", "E", "A") and party:
        vtype = "Purchase"
        lines.append(Line(res.main_ledger, taxable))
        taxes(+1, "input")
        if round_off:
            lines.append(Line(lg.round_off, round_off))
        lines.append(Line(party, -total, is_party=True, bill=new_bill))
    elif tag in ("E", "A"):
        vtype = "Payment"
        lines.append(Line(res.main_ledger, total))
        lines.append(Line(paid_from, -total))
    elif tag == "L":
        vtype = "Receipt"
        lines.append(Line(bank, total))
        lines.append(Line(res.main_ledger, -total))
    elif tag == "R":
        vtype = "Receipt"
        lines.append(Line(bank, total))
        lines.append(Line(party, -total, is_party=True, bill=against_bill or BillRef("On Account", "On Account")))
    elif tag == "PY":
        vtype = "Payment"
        lines.append(Line(party, total, is_party=True, bill=against_bill or BillRef("On Account", "On Account")))
        lines.append(Line(bank, -total))
    else:
        raise CannotPost(f"Unknown tag {tag!r}.")

    narration_bits = [f"Recorded by MunshiG from WhatsApp, entry {entry.id}."]
    if entry.utr:
        narration_bits.append(f"UPI ref {entry.utr}.")
    narration_bits.extend(notes)
    voucher = Voucher(
        vtype=vtype,
        vdate=vdate,
        lines=lines,
        remote_id=f"munshig-{entry.id}",
        party=party,
        reference=ref,
        reference_date=entry.invoice_date if ref and tag in ("S", "P", "E", "A") else None,
        narration=" ".join(narration_bits),
        notes=notes,
    )
    voucher.check_balanced()
    return voucher


def undo_entry(db: DB, tally, entry: Entry, actor: str = "owner") -> str:
    """Delete a voucher MunshiG posted today. Returns a message for the owner."""
    if entry.status != "posted" or not entry.tally_result or not entry.tally_remote_id:
        return "it isn't posted to Tally, so there is nothing to undo"
    if (entry.posted_at or "")[:10] != now_iso()[:10]:
        return "UNDO only works on the day of posting; correct it in Tally instead"
    info = json.loads(entry.tally_result)
    try:
        result = tally.delete_voucher(entry.tally_remote_id, info["vtype"], date.fromisoformat(info["date"]))
    except TallyUnavailable as exc:
        return str(exc)
    if not result.ok:
        return ("Tally didn't delete it (" + ("; ".join(result.messages) or "no reason given")
                + "). Please delete it in Tally by hand.")
    entry.status, entry.tag, entry.tally_result, entry.posted_at = "pending", None, None, None
    entry.tally_remote_id = entry.approved_at = entry.approved_by = None
    save_entry(db, entry)
    db.audit(actor, "undone", entry=entry.id, vtype=info["vtype"])
    return "removed from Tally; it's back in the list"


def post_entry(db: DB, settings: Settings, tally, entry: Entry, actor: str = "owner") -> Entry:
    """Post one approved entry. Never raises for business errors: the entry ends
    up 'posted' or 'failed' with a plain-language reason."""
    tag = entry.tag
    if entry.status == "posted":
        return entry
    if not tag or tag == "X":
        raise ValueError("post_entry called for an entry that isn't approved for posting")

    try:
        res = resolve(db, settings, entry, tag)
        voucher = build_voucher(settings, entry, tag, res)

        if res.main_ledger and tag in ("E", "A", "L") and db.one("SELECT 1 FROM ledgers") \
                and not ledger_exists(db, res.main_ledger):
            raise CannotPost(f"Ledger '{res.main_ledger}' isn't in Tally. Reply with the entry number, "
                             f"{tag} and an existing ledger name, or create it in Tally first.")

        if tag in ("S", "P", "E", "A") and res.party_ledger and entry.invoice_no and entry.invoice_date:
            existing = tally.find_voucher(res.party_ledger, entry.invoice_no, entry.invoice_date)
            if existing:
                found = existing[0]
                entry.status = "duplicate"
                entry.tally_error = (f"Already in Tally: {found.get('VOUCHERTYPENAME', 'voucher')} "
                                     f"no. {found.get('VOUCHERNUMBER', '?')} for {res.party_ledger}, "
                                     f"ref {entry.invoice_no}. Not posted again.")
                save_entry(db, entry)
                db.audit(actor, "duplicate_in_tally", entry=entry.id)
                return entry

        if res.new_party:
            parent = settings.ledgers.sundry_debtors if tag in ("S", "R") else settings.ledgers.sundry_creditors
            created = tally.create_ledger(res.new_party, parent, entry.party_gstin, state_of(entry.party_gstin))
            if not created.ok:
                raise CannotPost(f"Couldn't create party '{res.new_party}' in Tally: "
                                 + ("; ".join(created.messages) or "no reason given"))
            db.execute("INSERT OR REPLACE INTO ledgers (name, parent, gstin) VALUES (?, ?, ?)",
                       (res.new_party, parent, entry.party_gstin))
            db.audit(actor, "ledger_created", name=res.new_party, parent=parent)

        result = tally.post_voucher(voucher)
        entry.tally_remote_id = voucher.remote_id
        if result.ok:
            entry.status = "posted"
            entry.posted_at = now_iso()
            entry.tally_result = json.dumps({"vtype": voucher.vtype, "date": voucher.vdate.isoformat(),
                                             "tally_id": result.last_vch_id, "notes": voucher.notes})
            entry.tally_error = None
            if res.main_ledger and tag in ("E", "A", "L", "S", "P"):
                remember_ledger(db, entry.party_name, tag, res.main_ledger)
            db.audit(actor, "posted", entry=entry.id, vtype=voucher.vtype, tally_id=result.last_vch_id)
        else:
            entry.status = "failed"
            entry.tally_error = "Tally refused it: " + ("; ".join(result.messages) or "no reason given")
            db.audit(actor, "post_failed", entry=entry.id, errors=result.messages)
    except CannotPost as exc:
        entry.status = "failed"
        entry.tally_error = str(exc)
        db.audit(actor, "post_blocked", entry=entry.id, reason=str(exc))
    except TallyUnavailable as exc:
        # Stay approved: it is retried when Tally is reachable again.
        entry.tally_error = str(exc)
        db.audit("system", "tally_unavailable", entry=entry.id)
    save_entry(db, entry)
    return entry
