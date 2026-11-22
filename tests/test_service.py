"""Message routing, safety rules and the daily routine, with a fake WhatsApp."""

import shutil
from datetime import timedelta

from munshig.demo import run_demo

from .conftest import OWNER, SAMPLES, reply_llm

PRIYA = "919811111111@s.whatsapp.net"
OWNER_JID = f"{OWNER}@s.whatsapp.net"
TIMINGS = {"timing": {"can_answer": True, "reply": "Open 9:30 am to 7:30 pm.", "holding_reply": "",
                      "question": "timings"}}
KB = "Open 9:30 am to 7:30 pm, Monday to Saturday."
counter = iter(range(1, 10_000))


def msg(svc, clock, jid=PRIYA, text="", kind="text", from_me=False, name="Priya", **extra):
    svc.handle_event({"type": "message", "id": f"M{next(counter)}", "jid": jid, "fromMe": from_me,
                      "pushName": name, "timestamp": int(clock.now.timestamp()), "kind": kind, "text": text, **extra})


def to(channel, jid):
    return [m["text"] for m in channel.sent if m["jid"] == jid]


def test_auto_reply_waits_for_the_burst_then_answers_once(make_service, settings, clock):
    settings.knowledge_path.write_text(KB)
    svc, ch = make_service(reply_llm(TIMINGS), mode="auto")
    msg(svc, clock, text="hi")
    msg(svc, clock, text="what are your timings?")
    svc.tick()
    assert to(ch, PRIYA) == []  # still within the burst window
    clock.now += timedelta(seconds=20)
    svc.tick()
    replies = to(ch, PRIYA)
    assert len(replies) == 1 and replies[0].startswith("(Automated assistant for Demo Traders)")
    assert "9:30 am" in replies[0]


def test_unknown_question_becomes_follow_up_and_draft(make_service, settings, clock, db):
    settings.knowledge_path.write_text(KB)
    svc, ch = make_service(reply_llm({}), mode="draft")
    msg(svc, clock, text="bulk price for 50 pipes?")
    clock.now += timedelta(seconds=20)
    svc.tick()
    assert to(ch, PRIYA) == []  # draft mode: nothing reaches the customer yet
    assert db.one("SELECT count(*) n FROM followups WHERE status = 'open'")["n"] == 1
    assert "D1 Holding reply" in to(ch, OWNER_JID)[0]
    msg(svc, clock, jid=OWNER_JID, text="D1", name="Owner")
    assert len(to(ch, PRIYA)) == 1


def test_only_the_owner_can_approve(make_service, settings, clock, db, truth):
    svc, ch = make_service(reply_llm({}), mode="draft")
    db.execute("INSERT INTO entries (id, created_at, doc_type, status, suggested_tag, party_name, total) "
               "VALUES ('e1', '2026-11-21', 'amount', 'pending', 'R', 'Priya', '500')")
    clock.now = clock.now.replace(hour=19, minute=1)
    svc.tick()
    assert "1. Priya" in to(ch, OWNER_JID)[0]
    msg(svc, clock, text="1R")  # a customer trying to approve
    assert db.one("SELECT status FROM entries WHERE id = 'e1'")["status"] == "pending"
    msg(svc, clock, jid=OWNER_JID, text="1R", name="Owner")
    assert db.one("SELECT status FROM entries WHERE id = 'e1'")["status"] == "posted"


def test_owner_recognised_by_alternate_jid(make_service, clock, db):
    svc, ch = make_service()
    msg(svc, clock, jid="123456789@lid", jidAlt=OWNER_JID, text="HELP", name="Owner")
    assert "MunshiG commands" in to(ch, "123456789@lid")[0]


def test_staff_reply_pauses_the_bot(make_service, settings, clock, db):
    settings.knowledge_path.write_text(KB)
    svc, ch = make_service(reply_llm(TIMINGS), mode="auto")
    msg(svc, clock, text="timings?")
    msg(svc, clock, text="We're open till 7:30", from_me=True)
    clock.now += timedelta(seconds=20)
    svc.tick()
    assert to(ch, PRIYA) == []
    clock.now += timedelta(hours=1)
    msg(svc, clock, text="timings?")
    clock.now += timedelta(seconds=20)
    svc.tick()
    assert to(ch, PRIYA) == []  # still paused (12 hours by default)


def test_groups_and_broadcasts_are_ignored(make_service, settings, clock, db):
    settings.knowledge_path.write_text(KB)
    svc, ch = make_service(reply_llm(TIMINGS), mode="auto")
    msg(svc, clock, jid="12036@g.us", text="paid 5000 timings?", isGroup=True)
    clock.now += timedelta(seconds=20)
    svc.tick()
    assert ch.sent == [] and db.one("SELECT count(*) n FROM entries")["n"] == 0


def test_after_hours_note_once_then_reply_in_the_morning(make_service, settings, clock):
    settings.knowledge_path.write_text(KB)
    svc, ch = make_service(reply_llm(TIMINGS), mode="auto")
    clock.now = clock.now.replace(hour=22)
    msg(svc, clock, text="timings?")
    clock.now += timedelta(seconds=20)
    svc.tick()
    msg(svc, clock, text="hello??")
    clock.now += timedelta(seconds=20)
    svc.tick()
    assert len(to(ch, PRIYA)) == 1 and "closed right now" in to(ch, PRIYA)[0]
    clock.now = (clock.now + timedelta(days=1)).replace(hour=9, minute=30)
    svc.tick()
    assert len(to(ch, PRIYA)) == 2 and "9:30 am" in to(ch, PRIYA)[1]


def test_messages_from_a_long_outage_go_to_the_owner(make_service, settings, clock, db):
    settings.knowledge_path.write_text(KB)
    svc, ch = make_service(reply_llm(TIMINGS), mode="auto")
    msg(svc, clock, text="timings?")
    clock.now += timedelta(hours=7)  # the PC was off
    svc.tick()
    assert to(ch, PRIYA) == []
    assert "while MunshiG was offline" in db.one("SELECT question FROM followups")["question"]


def test_invoice_photo_becomes_entry_and_edit_flags_it(make_service, settings, clock, db, truth, tmp_path):
    from munshig.llm import ScriptedLLM
    import json
    media = tmp_path / "bill.png"
    shutil.copy(SAMPLES / "office_chair_bill.png", media)
    llm = ScriptedLLM(lambda s, p, v: json.dumps(truth["office_chair_bill.png"]) if v else
                      json.dumps({"can_answer": True, "reply": "Thanks", "holding_reply": "", "question": "-"}))
    svc, ch = make_service(llm)
    msg(svc, clock, kind="image", mediaPath=str(media), mimeType="image/png", name="Comfort")
    row = db.one("SELECT * FROM entries")
    assert row["party_name"] == "Comfort Furniture House" and row["status"] == "pending"
    wa_id = db.one("SELECT wa_id FROM messages WHERE id = ?", (row["message_id"],))["wa_id"]
    svc.handle_event({"type": "revoke", "id": wa_id, "jid": PRIYA})
    assert db.one("SELECT status FROM entries")["status"] == "needs_review"


def test_voice_note_and_view_once_become_follow_ups(make_service, clock, db):
    svc, ch = make_service()
    msg(svc, clock, kind="audio")
    msg(svc, clock, kind="image", viewOnce=True)
    questions = [r["question"] for r in db.all("SELECT question FROM followups")]
    assert any("Voice note" in q for q in questions) and any("view-once" in q for q in questions)


def test_redelivered_message_is_processed_once(make_service, clock, db):
    svc, ch = make_service()
    event = {"type": "message", "id": "SAME", "jid": PRIYA, "fromMe": False, "pushName": "Priya",
             "timestamp": int(clock.now.timestamp()), "kind": "text", "text": "paid 5000"}
    svc.handle_event(event)
    svc.handle_event(event)
    assert db.one("SELECT count(*) n FROM entries")["n"] == 1


def test_digest_once_a_day_and_morning_reminder(make_service, clock, db):
    svc, ch = make_service()
    msg(svc, clock, text="paid 5000")
    clock.now = clock.now.replace(hour=19, minute=5)
    svc.tick()
    svc.tick()
    assert len([t for t in to(ch, OWNER_JID) if t.startswith("MunshiG:")]) == 1
    clock.now = (clock.now + timedelta(days=1)).replace(hour=10, minute=5)
    svc.tick()
    assert any(t.startswith("Reminder: 1 entries") for t in to(ch, OWNER_JID))


def test_daily_backup(make_service, settings, clock):
    svc, _ = make_service()
    svc.tick()
    assert list((settings.home / "backups").glob("munshig-*.db"))


def test_demo_runs_end_to_end(capsys):
    assert run_demo() == 0
    out = capsys.readouterr().out
    assert out.count("posted:") == 5 and "6. posted: Purchase ₹6,490" in out
    assert "1 duplicate skipped" in out
