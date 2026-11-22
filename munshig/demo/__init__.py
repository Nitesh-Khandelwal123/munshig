"""`munshig demo`: one business day, end to end, with dummy data.

Runs the real pipeline (intake, checks, digest, owner commands, Tally XML)
with a fake WhatsApp and, by default, a mock Tally and a scripted AI, so it
works on any PC. Options:

    --tally HOST:PORT   post to a real TallyPrime instead (use a TEST company)
    --company NAME      the Tally company to post to
    --edu               the Tally is in free educational mode
    --ai real           use the local Ollama models instead of the scripted AI
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from ..config import Settings
from ..db import DB
from ..llm import ScriptedLLM, from_settings
from ..parties import refresh_ledger_cache
from ..service import Service
from ..tally.client import TallyClient
from ..tally.mock import MockTally
from ..whatsapp import FakeChannel

SAMPLES = Path(__file__).parent / "samples"
OWNER = "919800000001"

KNOWLEDGE = """# Demo Traders: what the assistant may tell customers

## About us
Demo Traders sells MS pipes, GI sheets and steel hardware.
Address: Shop 4, Market Yard, Pune 411037.

## Timings
Open Monday to Saturday, 9:30 am to 7:30 pm. Closed on Sundays.

## Prices (per piece, GST extra)
- MS pipe 1 inch, 6 m: Rs 480
- GI sheet 0.5 mm: Rs 530

## Payments
UPI: demotraders@okbank. Cash and bank transfer accepted.

## Delivery
Within Pune city in 2 working days.
"""


class DemoAI:
    """Answers like a well-behaved local model would, from the sample truth file."""

    def __init__(self):
        self.truth = json.loads((SAMPLES / "truth.json").read_text(encoding="utf-8"))
        self.current: str | None = None
        self.llm = ScriptedLLM(self.answer)

    def answer(self, system: str, prompt: str, vision: bool) -> str:
        if system.startswith("You read Indian business documents"):
            return json.dumps(self.truth[self.current])
        incoming = prompt.split("New message(s) from the customer:", 1)[-1].lower()
        if "timing" in incoming or "open" in incoming:
            return json.dumps({"can_answer": True, "question": "shop timings", "holding_reply": "",
                               "reply": "We're open Monday to Saturday, 9:30 am to 7:30 pm, and closed on Sundays."})
        if "?" not in incoming:  # sharing a bill or a payment note: acknowledge, confirm nothing
            return json.dumps({"can_answer": True, "question": "shared a document or payment", "holding_reply": "",
                               "reply": "Thank you, we've got it. Our team will check and confirm."})
        return json.dumps({"can_answer": False, "reply": "", "question": "Bulk price for 50 MS pipes",
                           "holding_reply": "Thanks! Let me check the bulk price and get back to you shortly."})


def _print(title: str, body: str = "") -> None:
    print(f"\n=== {title} ===")
    if body:
        print(body)


def run_demo(tally_addr: str | None = None, company: str = "Demo Traders", edu: bool = True,
             ai: str = "scripted", keep: bool = False) -> int:
    home = Path(tempfile.mkdtemp(prefix="munshig-demo-"))
    settings = Settings(home=home)
    settings.business.name, settings.business.gstin = "Demo Traders", "27AABCD1234E1Z8"
    settings.owner.numbers = [OWNER]
    settings.tally.company, settings.tally.educational_mode = company, edu
    settings.ledgers.default_asset = "Office Equipment"
    settings.knowledge_path.write_text(KNOWLEDGE, encoding="utf-8")

    mock = None
    if tally_addr:
        host, _, port = tally_addr.partition(":")
        tally = TallyClient(host, int(port or 9000), company)
        where = f"real Tally at {tally_addr}, company '{company}'"
    else:
        mock = MockTally(company=company, educational_mode=edu)
        tally = TallyClient(company=company, transport=mock.transport())
        where = f"mock Tally (educational mode {'on' if edu else 'off'})"

    demo_ai = DemoAI()
    llm = demo_ai.llm if ai == "scripted" else from_settings(settings.ai)

    db = DB(settings.db_path)
    clock = {"now": datetime(2026, 11, 21, 11, 0)}
    channel = FakeChannel()
    svc = Service(settings, db, channel, tally, llm, clock=lambda: clock["now"],
                  sleep=lambda s: None, background_extraction=False)
    svc.me = "919800000099@s.whatsapp.net"

    _print("MunshiG demo", f"Business: Demo Traders (GSTIN 27AABCD1234E1Z8)\nPosting to: {where}\nAI: {ai}")
    try:
        count = refresh_ledger_cache(db, tally.ledgers())
        print(f"Read {count} ledgers from Tally.")
    except Exception as exc:  # noqa: BLE001 - shown to the user as is
        print(f"Could not read ledgers from Tally: {exc}")
        return 1

    seq = iter(range(1, 100))

    def incoming(jid, name, kind="text", text="", sample=None, from_me=False):
        clock["now"] += timedelta(minutes=7)
        media = None
        if sample:
            media = str(home / "media" / sample)
            (home / "media").mkdir(exist_ok=True)
            shutil.copy(SAMPLES / sample, media)
            demo_ai.current = sample
        who = "Business phone" if from_me else name
        print(f"[{clock['now']:%H:%M}] {who}: " + (f"<{sample}> " if sample else "") + text)
        svc.handle_event({
            "type": "message", "id": f"IN{next(seq)}", "jid": jid, "fromMe": from_me, "pushName": name,
            "timestamp": int(clock["now"].timestamp()), "kind": kind, "text": text, "mediaPath": media,
            "mimeType": "application/pdf" if sample and sample.endswith(".pdf") else "image/png" if sample else None,
            "fileName": sample})

    priya, sharma, kumar, rahul, xyz = (f"9198{n}@s.whatsapp.net" for n in
                                         ("11111111", "22222222", "33333333", "44444444", "55555555"))
    _print("Messages during the day")
    incoming(priya, "Priya", text="Hi, what are your shop timings?")
    clock["now"] += timedelta(seconds=30)
    svc.tick()
    incoming(sharma, "Ravi (Sharma Steel)", kind="document", sample="sharma_steel_invoice.pdf", text="Bill for the pipes")
    incoming(kumar, "Kumar Electricals", kind="document", sample="sales_invoice_kumar.pdf", from_me=True)
    incoming(priya, "Priya", text="Can I get a bulk price for 50 MS pipes?")
    clock["now"] += timedelta(seconds=30)
    svc.tick()
    incoming(sharma, "Ravi (Sharma Steel)", kind="document", sample="sharma_steel_invoice.pdf",
             text="Sending again in case you missed it")
    incoming(kumar, "Kumar Electricals", kind="image", sample="upi_payment_kumar.png")
    incoming(rahul, "Rahul", text="Sent 5000 advance for the order, UTR 412398765432")
    incoming(xyz, "XYZ Packaging", kind="document", sample="quotation_xyz.pdf")
    incoming(OWNER + "@s.whatsapp.net", "Owner", kind="image", sample="office_chair_bill.png")
    clock["now"] += timedelta(seconds=30)
    svc.tick()

    owner_jid = f"{OWNER}@s.whatsapp.net"

    def owner_inbox(since: int) -> list[str]:
        return [m["text"] for m in channel.sent[since:] if m["jid"] == owner_jid]

    _print("What MunshiG sent so far")
    for m in channel.sent:
        print(f"-> {'Owner' if m['jid'] == owner_jid else m['jid'].split('@')[0]}: {m['text']}\n")

    clock["now"] = clock["now"].replace(hour=19, minute=0)
    before = len(channel.sent)
    svc.tick()
    _print("7 pm digest to the owner", "\n".join(owner_inbox(before)))

    # Build the owner's reply from the digest, the way the owner would read it.
    rows = db.all("SELECT label, ref FROM digest_items WHERE kind = 'entry' AND digest_id = (SELECT max(id) FROM digests)")
    reply_tags = []
    for r in rows:
        e = db.one("SELECT party_name, doc_type, suggested_tag FROM entries WHERE id = ?", (r["ref"],))
        if e["doc_type"] == "not_a_bill":
            reply_tags.append(f"{r['label']}X")
        elif (e["party_name"] or "").startswith("Comfort"):
            reply_tags.append(f"{r['label']} A Office Equipment")
        else:
            reply_tags.append(f"{r['label']}{e['suggested_tag']}")
    followup = db.one("SELECT id FROM followups WHERE status = 'open' ORDER BY id LIMIT 1")
    draft_ids = [d["id"] for d in db.all("SELECT id FROM drafts WHERE status = 'waiting' ORDER BY id")]
    lines = []
    plain = [t for t in reply_tags if " " not in t]
    lines.append(" ".join(plain))
    lines += [t for t in reply_tags if " " in t]
    if followup:
        lines.append(f"F{followup['id']} Rs 450 per piece for 50 pieces, delivery free in Pune")
    lines += [f"D{d}" for d in draft_ids]
    owner_reply = "\n".join(lines)
    _print("Owner replies", owner_reply)
    before = len(channel.sent)
    svc.handle_event({"type": "message", "id": "OWNER1", "jid": owner_jid, "fromMe": False,
                      "timestamp": int(clock["now"].timestamp()), "kind": "text", "text": owner_reply})
    _print("MunshiG answers")
    for m in channel.sent[before:]:
        print(f"-> {'Owner' if m['jid'] == owner_jid else m['jid'].split('@')[0]}: {m['text']}\n")

    if mock:
        _print("Vouchers now in (mock) Tally")
        for v in mock.vouchers:
            print(f"{v.vtype:<9} {v.date}  party={v.party or '-'}  ref={v.reference or '-'}")
            for ledger, amount in v.lines:
                side = "Dr" if amount < 0 else "Cr"
                print(f"    {side} {ledger:<24} {abs(amount):>12,.2f}")
            print(f"    {v.narration}")

    _print("Done", f"Demo data kept in {home}" if keep else "")
    db.close()
    if not keep:
        shutil.rmtree(home, ignore_errors=True)
    return 0
