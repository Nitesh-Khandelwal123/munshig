"""The MunshiG service: routes WhatsApp events and runs the daily routine.

    message from the owner          -> commands (approve, answer follow-ups, send drafts)
    message typed on the business
    phone in a customer chat        -> the bot goes quiet in that chat (human takeover)
    customer text                   -> money capture + queued for a reply
    customer photo / PDF            -> read in the background into a pending entry
    customer voice note             -> follow-up for the owner (not transcribed yet)
    group, broadcast, status        -> stored, nothing else

tick() runs every few seconds: replies (after a short pause so a burst of
messages gets one answer), the digest at the set time, Tally retries, the
morning reminder and the daily backup.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import queue
import random
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import approvals, commands
from .config import Settings
from .db import DB, now_iso
from .digest import build_digest
from .extract import extract_document
from .intake import Sender, entries_from_text, entry_from_document
from .llm import LLM
from .models import Entry, save_entry
from .money import find_money
from .posting import post_entry
from .reply import decide_reply

log = logging.getLogger("munshig")

BURST_WAIT_SECONDS = 15
RETRY_MINUTES = 15
BACKUPS_KEPT = 14
REPLYABLE_KINDS = ("text",)
CAPTURE_MEDIA_KINDS = ("image", "document")


def digits(jid: str | None) -> str:
    if not jid:
        return ""
    return re.sub(r"\D", "", jid.split("@")[0].split(":")[0])


def normalize_number(number: str) -> str:
    d = re.sub(r"\D", "", number)
    return "91" + d if len(d) == 10 else d


def _hhmm(value: str) -> tuple[int, int]:
    h, m = value.strip().split(":")
    return int(h), int(m)


class Service:
    def __init__(self, settings: Settings, db: DB, channel, tally, llm: LLM | None,
                 clock: Callable[[], datetime] = datetime.now,
                 sleep: Callable[[float], None] = time.sleep, background_extraction: bool = True):
        self.settings, self.db, self.channel, self.tally, self.llm = settings, db, channel, tally, llm
        self.clock, self.sleep = clock, sleep
        self.owner_numbers = {normalize_number(n) for n in settings.owner.numbers}
        self.me: str | None = None
        self.background_extraction = background_extraction
        self._jobs: queue.Queue[int] = queue.Queue()
        if background_extraction:
            threading.Thread(target=self._extraction_worker, daemon=True).start()

    # ------------------------------------------------------------------ helpers
    def is_owner(self, jid: str, jid_alt: str | None = None) -> bool:
        # Only phone-number IDs count; WhatsApp's anonymous "@lid" IDs are not phone numbers.
        phones = {digits(j) for j in (jid, jid_alt) if j and j.endswith(("@s.whatsapp.net", "@c.us"))}
        return bool(phones & self.owner_numbers)

    def owner_jids(self) -> list[str]:
        return [f"{n}@s.whatsapp.net" for n in sorted(self.owner_numbers)]

    def tell_owner(self, text: str, media_path: str | None = None) -> None:
        for jid in self.owner_jids():
            self._send(jid, text, media_path)

    def _send(self, jid: str, text: str, media_path: str | None = None) -> None:
        try:
            mime = mimetypes.guess_type(media_path)[0] if media_path else None
            msg_id = self.channel.send(jid, text, media_path, mime)
        except Exception as exc:  # the bridge being down must not crash the service
            log.warning("Send to %s failed: %s", jid, exc)
            self.db.audit("system", "send_failed", jid=jid, error=str(exc))
            return
        self.db.execute(
            "INSERT OR IGNORE INTO messages (wa_id, jid, from_me, ts, kind, text, processed) "
            "VALUES (?, ?, 1, ?, 'text', ?, 1)", (msg_id, jid, int(self.clock().timestamp()), text))

    def _msg_date(self, ev: dict):
        ts = ev.get("timestamp")
        return datetime.fromtimestamp(int(ts)).date() if ts else self.clock().date()

    def knowledge(self) -> str:
        path = self.settings.knowledge_path
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def in_working_hours(self, now: datetime) -> bool:
        start_s, end_s = self.settings.assistant.working_hours.split("-")
        start, end = _hhmm(start_s), _hhmm(end_s)
        return start <= (now.hour, now.minute) < end

    def _contact(self, jid: str, name: str | None = None):
        self.db.execute("INSERT OR IGNORE INTO contacts (jid, name) VALUES (?, ?)", (jid, name))
        if name:
            self.db.execute("UPDATE contacts SET name = ? WHERE jid = ? AND (name IS NULL OR name = '')", (name, jid))
        return self.db.one("SELECT * FROM contacts WHERE jid = ?", (jid,))

    def _followup(self, jid: str, message_id: int | None, question: str) -> int:
        cur = self.db.execute("INSERT INTO followups (jid, message_id, question, created_at) VALUES (?, ?, ?, ?)",
                              (jid, message_id, question, now_iso()))
        return cur.lastrowid

    # ------------------------------------------------------------------- events
    def handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "message":
            self._on_message(event)
        elif kind == "edit":
            self._on_edit(event)
        elif kind == "revoke":
            self._on_revoke(event)
        elif kind == "connected":
            self.me = event.get("me")
            self.db.set("whatsapp_status", "connected")
            log.info("WhatsApp connected as %s", digits(self.me))
        elif kind == "disconnected":
            self.db.set("whatsapp_status", "logged_out" if event.get("loggedOut") else "reconnecting")
            if event.get("loggedOut"):
                log.error("WhatsApp logged this PC out. Run 'munshig link' and scan the QR again.")
        elif kind == "qr":
            self.db.set("whatsapp_status", "waiting_for_qr")
            log.warning("Scan the QR code above with WhatsApp > Linked devices on the business phone.")
        elif kind == "log":
            log.log(logging.ERROR if event.get("level") == "error" else logging.INFO, "bridge: %s", event.get("message"))
        elif kind == "bridge_exit":
            self.db.set("whatsapp_status", "stopped")
            log.error("WhatsApp bridge stopped (exit code %s)", event.get("code"))

    def _on_message(self, ev: dict) -> None:
        if ev.get("sentByBridge"):
            return
        jid = ev["jid"]
        cur = self.db.execute(
            "INSERT OR IGNORE INTO messages (wa_id, jid, jid_alt, from_me, is_group, push_name, ts, kind, text, "
            "media_path, mime, file_name, media_sha256, processed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (ev.get("id"), jid, ev.get("jidAlt"), int(bool(ev.get("fromMe"))), int(bool(ev.get("isGroup"))),
             ev.get("pushName"), int(ev.get("timestamp") or self.clock().timestamp()), ev.get("kind", "text"),
             ev.get("text") or "", ev.get("mediaPath"), ev.get("mimeType"), ev.get("fileName"), ev.get("sha256")))
        if cur.rowcount == 0:
            return  # already seen (WhatsApp redelivers after a reconnect)
        msg_id = cur.lastrowid

        def done():
            self.db.execute("UPDATE messages SET processed = 1 WHERE id = ?", (msg_id,))

        if ev.get("isGroup") or ev.get("isBroadcast"):
            return done()

        from_me = bool(ev.get("fromMe"))
        self_chat = from_me and self.me and digits(jid) == digits(self.me)
        if (self.is_owner(jid, ev.get("jidAlt")) and not from_me) or (self_chat and self.is_owner(self.me)):
            done()
            owner_jid = jid if not self_chat else self.owner_jids()[0]
            if ev.get("kind") in CAPTURE_MEDIA_KINDS and ev.get("mediaPath"):
                # The owner forwarding a bill to be recorded.
                self._contact(jid, "Owner")
                if self.background_extraction:
                    self._jobs.put(msg_id)
                else:
                    self.process_media(msg_id)
                self._send(owner_jid, "Got it. It's in today's list for your approval.")
            return self._on_owner(owner_jid, ev.get("text") or "")

        name = ev.get("pushName") if not from_me else None
        contact = self._contact(jid, name)
        sender = Sender(jid=jid, name=contact["name"] or digits(jid), from_me=from_me)

        if from_me:
            # Someone typed on the business phone: stay out of this chat for a while.
            until = self.clock() + timedelta(hours=self.settings.assistant.takeover_hours)
            self.db.execute("UPDATE contacts SET paused_until = ? WHERE jid = ?", (until.isoformat(), jid))
            self.db.execute("UPDATE messages SET processed = 1 WHERE jid = ? AND from_me = 0 AND processed = 0", (jid,))
            if ev.get("kind") == "text":
                entries_from_text(self.db, find_money(ev.get("text") or ""), sender, msg_id, ev.get("text") or "",
                                  when=self._msg_date(ev))
            elif ev.get("kind") in CAPTURE_MEDIA_KINDS and ev.get("mediaPath"):
                # e.g. the owner sending a sales invoice to a customer
                if self.background_extraction:
                    self._jobs.put(msg_id)
                else:
                    self.process_media(msg_id)
            return done()

        kind = ev.get("kind")
        if kind in CAPTURE_MEDIA_KINDS:
            if ev.get("viewOnce") or not ev.get("mediaPath"):
                self._followup(jid, msg_id, f"{sender.name} sent a {'view-once ' if ev.get('viewOnce') else ''}"
                               f"{kind} MunshiG couldn't save; please check it on the phone")
            elif self.background_extraction:
                self._jobs.put(msg_id)
            else:
                self.process_media(msg_id)
            if not (ev.get("text") or "").strip():
                return done()  # no caption: nothing to reply to
        elif kind == "audio":
            self._followup(jid, msg_id, f"Voice note from {sender.name}; please listen on the phone")
            return done()
        elif kind != "text":
            return done()

        text = ev.get("text") or ""
        if kind == "text":
            entries_from_text(self.db, find_money(text), sender, msg_id, text, when=self._msg_date(ev))
        if contact["do_not_reply"]:
            return done()
        # Left unprocessed: tick() replies once the customer pauses.

    def _on_owner(self, owner_jid: str, text: str) -> None:
        self.db.set("last_owner_message", now_iso())
        parsed = commands.parse(text)
        if parsed.empty and not parsed.unknown:
            return
        outcome = approvals.apply(self.db, self.settings, self.tally, owner_jid, parsed)
        for m in outcome.messages:
            self._send(m.jid, m.text, m.media_path)
            if m.kind == "customer":
                self.db.execute("UPDATE contacts SET greeted = 1 WHERE jid = ?", (m.jid,))

    def _entries_for_message(self, wa_id: str | None, jid: str) -> list[Entry]:
        row = self.db.one("SELECT id FROM messages WHERE wa_id = ? AND jid = ?", (wa_id, jid))
        if not row:
            return []
        from .models import OPEN_STATUSES
        marks = ", ".join("?" for _ in OPEN_STATUSES)
        rows = self.db.all(f"SELECT * FROM entries WHERE message_id = ? AND status IN ({marks})",
                           (row["id"], *OPEN_STATUSES))
        return [Entry.from_row(r) for r in rows]

    def _on_edit(self, ev: dict) -> None:
        self.db.execute("UPDATE messages SET text = ? WHERE wa_id = ? AND jid = ?", (ev.get("text"), ev.get("id"), ev.get("jid")))
        for e in self._entries_for_message(ev.get("id"), ev.get("jid")):
            e.review_reasons.append(f"sender edited the message to: {ev.get('text', '')[:80]}")
            e.status = "needs_review"
            save_entry(self.db, e)

    def _on_revoke(self, ev: dict) -> None:
        self.db.execute("UPDATE messages SET deleted = 1, processed = 1 WHERE wa_id = ? AND jid = ?", (ev.get("id"), ev.get("jid")))
        for e in self._entries_for_message(ev.get("id"), ev.get("jid")):
            e.review_reasons.append("deleted by the sender after it was received")
            e.status = "needs_review"
            save_entry(self.db, e)

    # --------------------------------------------------------------- extraction
    def _extraction_worker(self) -> None:
        while True:
            msg_id = self._jobs.get()
            try:
                self.process_media(msg_id)
            except Exception:
                log.exception("Reading document for message %s failed", msg_id)

    def process_media(self, msg_id: int) -> Entry | None:
        row = self.db.one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        if not row or not row["media_path"] or not Path(row["media_path"]).exists():
            return None
        sha = row["media_sha256"] or hashlib.sha256(Path(row["media_path"]).read_bytes()).hexdigest()
        if not row["media_sha256"]:
            self.db.execute("UPDATE messages SET media_sha256 = ? WHERE id = ?", (sha, msg_id))
        contact = self._contact(row["jid"])
        sender = Sender(jid=row["jid"], name=contact["name"] or digits(row["jid"]), from_me=bool(row["from_me"]))
        ex = extract_document(Path(row["media_path"]), self.llm, row["mime"])
        return entry_from_document(self.db, self.settings, ex, sender, msg_id, sha, today=self.clock().date(),
                                   when=datetime.fromtimestamp(row["ts"]).date())

    # --------------------------------------------------------------------- tick
    def tick(self) -> None:
        now = self.clock()
        self._reply_pass(now)
        self._digest_pass(now)
        self._retry_pass(now)
        self._reminder_pass(now)
        self._backup_pass(now)

    def _reply_pass(self, now: datetime) -> None:
        a = self.settings.assistant
        rows = self.db.all(
            "SELECT jid, max(ts) AS last_ts, min(ts) AS first_ts FROM messages "
            "WHERE from_me = 0 AND processed = 0 AND deleted = 0 AND is_group = 0 AND kind IN ('text', 'image', 'document') "
            "GROUP BY jid")
        for r in rows:
            jid = r["jid"]
            if now.timestamp() - r["last_ts"] < BURST_WAIT_SECONDS:
                continue
            pending = self.db.all("SELECT * FROM messages WHERE jid = ? AND from_me = 0 AND processed = 0 "
                                  "AND deleted = 0 ORDER BY ts", (jid,))
            ids = [p["id"] for p in pending]
            contact = self._contact(jid)
            name = contact["name"] or digits(jid)

            def mark():
                self.db.execute(f"UPDATE messages SET processed = 1 WHERE id IN ({','.join('?' * len(ids))})", ids)

            if contact["do_not_reply"] or (contact["paused_until"] and contact["paused_until"] > now.isoformat()):
                mark()
                continue
            texts = [p["text"] for p in pending if (p["text"] or "").strip()]
            if not texts:
                mark()
                continue
            told_closed = contact["after_hours_on"] and \
                (now.date() - datetime.fromisoformat(contact["after_hours_on"]).date()).days <= 1
            if now.timestamp() - r["first_ts"] > a.stale_after_hours * 3600 and not told_closed:
                self._followup(jid, ids[-1], f"{name} wrote while MunshiG was offline: \"{' / '.join(texts)[:150]}\"")
                mark()
                continue
            if not self.in_working_hours(now):
                today = now.date().isoformat()
                if contact["after_hours_on"] != today:
                    self.db.execute("UPDATE contacts SET after_hours_on = ? WHERE jid = ?", (today, jid))
                    # A fixed template, so it goes out even in draft mode.
                    self._deliver(jid, name, f"Thanks for your message. We're closed right now and will reply "
                                  f"during working hours ({a.working_hours}).", ids[-1], answered=True,
                                  force_send=True)
                continue  # keep the messages for the morning
            sent_last_hour = self.db.one(
                "SELECT count(*) AS n FROM messages WHERE from_me = 1 AND ts > ?", (int(now.timestamp()) - 3600,))
            if sent_last_hour["n"] >= a.max_replies_per_hour:
                continue
            history_rows = self.db.all(
                "SELECT from_me, text FROM messages WHERE jid = ? AND processed = 1 AND deleted = 0 "
                "AND text != '' ORDER BY ts DESC LIMIT 10", (jid,))
            history = [("Business" if h["from_me"] else "Customer", h["text"]) for h in reversed(history_rows)]
            decision = decide_reply(self.llm, self.settings.business.name, self.knowledge(), history, texts)
            if not decision.answered:
                fid = self._followup(jid, ids[-1], f"{name}: {decision.question}")
                self.db.audit("bot", "followup_created", followup=fid, reason=decision.reason)
            text = decision.text
            if a.disclose_bot and not contact["greeted"]:
                business = self.settings.business.name or "us"
                text = f"(Automated assistant for {business})\n{text}"
                self.db.execute("UPDATE contacts SET greeted = 1 WHERE jid = ?", (jid,))
            mark()
            self._deliver(jid, name, text, ids[-1], answered=decision.answered)

    def _deliver(self, jid: str, name: str, text: str, message_id: int, answered: bool,
                 force_send: bool = False) -> None:
        if self.settings.assistant.mode == "draft" and not force_send:
            cur = self.db.execute("INSERT INTO drafts (jid, message_id, text, created_at) VALUES (?, ?, ?, ?)",
                                  (jid, message_id, text, now_iso()))
            did = cur.lastrowid
            label = "Reply" if answered else "Holding reply (follow-up added)"
            self.tell_owner(f"D{did} {label} for {name}:\n{text}\n\nSend D{did} to send, D{did} <your text> "
                            f"to change it, D{did} NO to drop it.")
            return
        self.channel.typing(jid, True)
        self.sleep(min(15.0, random.uniform(3, 8) + len(text) / 40))
        self.channel.typing(jid, False)
        self._send(jid, text)
        self.db.execute("UPDATE contacts SET greeted = 1 WHERE jid = ?", (jid,))

    def _digest_pass(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.db.get("last_digest_date") == today or (now.hour, now.minute) < _hhmm(self.settings.assistant.digest_time):
            return
        self.db.set("last_digest_date", today)
        text, digest_id = build_digest(self.db)
        if digest_id is not None:
            self.tell_owner(text)

    def send_digest_now(self) -> str:
        text, _ = build_digest(self.db)
        self.tell_owner(text)
        return text

    def _retry_pass(self, now: datetime) -> None:
        last = self.db.get("last_retry")
        if last and now - datetime.fromisoformat(last) < timedelta(minutes=RETRY_MINUTES):
            return
        self.db.set("last_retry", now.isoformat())
        rows = self.db.all("SELECT * FROM entries WHERE status = 'approved' AND tally_error IS NOT NULL")
        results = []
        for r in rows:
            e = post_entry(self.db, self.settings, self.tally, Entry.from_row(r), actor="retry")
            if e.status in ("posted", "failed", "duplicate"):
                results.append(f"{e.party_name or 'Entry'} {e.status}" + (f": {e.tally_error}" if e.tally_error else ""))
        if results:
            self.tell_owner("Tally is reachable again:\n" + "\n".join(results))

    def _reminder_pass(self, now: datetime) -> None:
        start = _hhmm(self.settings.assistant.working_hours.split("-")[0])
        if (now.hour, now.minute) < (start[0] + 1, start[1]) or self.db.get("last_reminder_date") == now.date().isoformat():
            return
        last_digest = self.db.one("SELECT created_at FROM digests ORDER BY id DESC LIMIT 1")
        if not last_digest:
            return
        last_owner = self.db.get("last_owner_message", "")
        waiting = self.db.one("SELECT count(*) AS n FROM entries WHERE status IN ('pending', 'needs_review', 'failed')")
        self.db.set("last_reminder_date", now.date().isoformat())
        if waiting["n"] and last_owner < last_digest["created_at"]:
            self.tell_owner(f"Reminder: {waiting['n']} entries are waiting for your tags. Send DIGEST to see them.")

    def _backup_pass(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.db.get("last_backup_date") == today:
            return
        self.db.set("last_backup_date", today)
        folder = self.settings.home / "backups"
        try:
            self.db.backup_to(folder / f"munshig-{today}.db")
            for old in sorted(folder.glob("munshig-*.db"))[:-BACKUPS_KEPT]:
                old.unlink()
        except OSError as exc:
            log.warning("Backup failed: %s", exc)

    # ---------------------------------------------------------------------- run
    def run_forever(self, tick_seconds: float = 3.0) -> None:
        events = self.channel.events()
        next_tick = 0.0
        while True:
            timeout = max(0.1, next_tick - time.monotonic())
            try:
                self.handle_event(events.get(timeout=timeout))
            except queue.Empty:
                pass
            except Exception:
                log.exception("Handling a WhatsApp event failed")
            if time.monotonic() >= next_tick:
                try:
                    self.tick()
                except Exception:
                    log.exception("Scheduled work failed")
                next_tick = time.monotonic() + tick_seconds
