"""Apply the owner's reply: tag entries, post them, answer follow-ups, send drafts.

Only called for messages from a verified owner number. The service layer
checks that before anything here runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .commands import HELP_TEXT, Parsed, Tag
from .config import Settings
from .db import DB, now_iso
from .digest import build_digest, entry_line, inr, latest_entry_for
from .models import TAGS, get_entry, save_entry
from .posting import post_entry, undo_entry


@dataclass
class OutMessage:
    jid: str
    text: str
    media_path: str | None = None
    kind: str = "reply"  # reply (to the owner), customer, draft


@dataclass
class Outcome:
    owner_lines: list[str] = field(default_factory=list)
    messages: list[OutMessage] = field(default_factory=list)


def _posted_line(number: int, e) -> str:
    if e.status == "posted":
        info = json.loads(e.tally_result or "{}")
        note = f" ({info['notes'][0]})" if info.get("notes") else ""
        return f"{number}. posted: {info.get('vtype', 'voucher')} {inr(e.total)} for {e.party_name or 'party'}{note}"
    if e.status == "duplicate":
        return f"{number}. not posted: {e.tally_error}"
    if e.status == "failed":
        return f"{number}. NOT posted: {e.tally_error}"
    if e.status == "approved" and e.tally_error:
        return f"{number}. waiting for Tally: {e.tally_error}"
    return f"{number}. {e.status}"


def apply(db: DB, settings: Settings, tally, owner_jid: str, parsed: Parsed) -> Outcome:
    out = Outcome()
    actor = f"owner:{owner_jid}"

    if parsed.help:
        out.owner_lines.append(HELP_TEXT)

    tags = list(parsed.tags)
    if parsed.accept_all:
        rows = db.all("SELECT label, ref FROM digest_items WHERE kind = 'entry' "
                      "AND digest_id = (SELECT max(id) FROM digests)")
        tagged = {t.number for t in tags}
        for row in rows:
            e = get_entry(db, row["ref"])
            if e and e.status == "pending" and e.suggested_tag and int(row["label"]) not in tagged:
                tags.append(Tag(int(row["label"]), e.suggested_tag))
        if not tags:
            out.owner_lines.append("Nothing to accept: every entry needs a check or has no suggestion.")

    to_post = []
    for t in tags:
        entry_id = latest_entry_for(db, t.number)
        e = get_entry(db, entry_id) if entry_id else None
        if not e:
            out.owner_lines.append(f"{t.number}. not in the latest digest")
            continue
        if e.status in ("posted", "ignored", "duplicate"):
            out.owner_lines.append(f"{t.number}. already {e.status}")
            continue
        if t.tag == "X":
            e.status, e.tag = "ignored", "X"
            e.approved_at, e.approved_by = now_iso(), actor
            save_entry(db, e)
            db.audit(actor, "ignored", entry=e.id)
            out.owner_lines.append(f"{t.number}. ignored")
            continue
        e.tag, e.status = t.tag, "approved"
        if t.ledger:
            e.ledger = t.ledger
        e.approved_at, e.approved_by = now_iso(), actor
        e.tally_error = None
        save_entry(db, e)
        db.audit(actor, "approved", entry=e.id, tag=t.tag, ledger=t.ledger)
        to_post.append((t.number, e))

    for number, e in to_post:
        e = post_entry(db, settings, tally, e, actor=actor)
        out.owner_lines.append(_posted_line(number, e))
    # Report entries in the order of the digest, whatever order the owner tagged them in.
    out.owner_lines.sort(key=lambda line: int(line.split(".", 1)[0]) if line.split(".", 1)[0].isdigit() else 0)

    for number in parsed.details:
        entry_id = latest_entry_for(db, number)
        e = get_entry(db, entry_id) if entry_id else None
        if not e:
            out.owner_lines.append(f"{number}. not in the latest digest")
            continue
        detail = [entry_line(db, number, e)]
        fields = [("Party GSTIN", e.party_gstin), ("Taxable", e.taxable and inr(e.taxable)),
                  ("CGST", e.cgst and inr(e.cgst)), ("SGST", e.sgst and inr(e.sgst)),
                  ("IGST", e.igst and inr(e.igst)), ("Round-off", e.round_off and inr(e.round_off)),
                  ("UPI ref", e.utr), ("Read from", e.source)]
        detail += [f"   {k}: {v}" for k, v in fields if v]
        media = db.one("SELECT media_path FROM messages WHERE id = ?", (e.message_id,)) if e.message_id else None
        out.messages.append(OutMessage(owner_jid, "\n".join(detail),
                                       media_path=media["media_path"] if media and media["media_path"] else None))

    # Drafts first: a holding reply must reach the customer before the owner's answer.
    for did, new_text in parsed.draft_send:
        row = db.one("SELECT * FROM drafts WHERE id = ?", (did,))
        if not row or row["status"] != "waiting":
            out.owner_lines.append(f"D{did}: no waiting draft with that number")
            continue
        text = new_text or row["text"]
        db.execute("UPDATE drafts SET status = 'sent', text = ?, decided_at = ? WHERE id = ?", (text, now_iso(), did))
        db.audit(actor, "draft_sent", draft=did, edited=bool(new_text))
        out.messages.append(OutMessage(row["jid"], text, kind="customer"))
        out.owner_lines.append(f"D{did}: sent")

    for did in parsed.draft_discard:
        db.execute("UPDATE drafts SET status = 'discarded', decided_at = ? WHERE id = ? AND status = 'waiting'",
                   (now_iso(), did))
        out.owner_lines.append(f"D{did}: discarded")

    for fid, answer in parsed.followup_answers:
        row = db.one("SELECT * FROM followups WHERE id = ?", (fid,))
        if not row:
            out.owner_lines.append(f"F{fid}: no such follow-up")
            continue
        if row["status"] != "open":
            out.owner_lines.append(f"F{fid}: already answered")
            continue
        db.execute("UPDATE followups SET status = 'answered', answer = ?, answered_at = ? WHERE id = ?",
                   (answer, now_iso(), fid))
        db.audit(actor, "followup_answered", followup=fid)
        out.messages.append(OutMessage(row["jid"], answer, kind="customer"))
        out.owner_lines.append(f"F{fid}: sent to the customer")

    for number in parsed.undo:
        entry_id = latest_entry_for(db, number)
        e = get_entry(db, entry_id) if entry_id else None
        if not e:
            out.owner_lines.append(f"UNDO {number}: not in the latest digest")
            continue
        out.owner_lines.append(f"UNDO {number}: {undo_entry(db, tally, e, actor)}")

    if parsed.digest_now:
        text, _ = build_digest(db)
        out.messages.append(OutMessage(owner_jid, text))

    for line in parsed.unknown:
        out.owner_lines.append(f"Didn't understand \"{line[:60]}\". Send HELP for the commands.")

    if out.owner_lines:
        out.messages.insert(0, OutMessage(owner_jid, "\n".join(out.owner_lines)))
    return out


__all__ = ["apply", "Outcome", "OutMessage", "TAGS"]
