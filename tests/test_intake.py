"""Reading documents into pending entries, checks and duplicates."""

import json
from datetime import date
from decimal import Decimal as D

from munshig.extract import Extraction, extract_document, from_model_json
from munshig.intake import Sender, entries_from_text, entry_from_document
from munshig.llm import ScriptedLLM
from munshig.money import find_money
from munshig.reply import HOLDING_REPLY, decide_reply

from .conftest import OUR_GSTIN, SAMPLES

TODAY = date(2026, 11, 21)
CUSTOMER = Sender("919811111111@s.whatsapp.net", "Ravi", from_me=False)


def truth_llm(record: dict) -> ScriptedLLM:
    return ScriptedLLM(lambda system, prompt, vision: json.dumps(record))


def test_pdf_with_text_uses_text_route_and_qr(truth):
    llm = truth_llm(truth["sharma_steel_invoice.pdf"])
    ex = extract_document(SAMPLES / "sharma_steel_invoice.pdf", llm)
    assert llm.calls[0][1] is False  # text route, no vision model needed
    assert ex.source == "qr+pdf_text" and ex.total == D("11800.00") and ex.warnings == []


def test_photo_uses_vision_route(truth):
    llm = truth_llm(truth["office_chair_bill.png"])
    ex = extract_document(SAMPLES / "office_chair_bill.png", llm)
    assert llm.calls[0][1] is True and ex.source == "vision" and ex.invoice_no == "CF-889"


def test_qr_wins_when_ai_misreads(truth):
    misread = dict(truth["sharma_steel_invoice.pdf"], total=11880, invoice_no="SS/2026/0418")
    ex = extract_document(SAMPLES / "sharma_steel_invoice.pdf", truth_llm(misread))
    assert ex.total == D("11800.00") and ex.invoice_no == "SS/2026/0412"
    assert any("differs from the e-invoice QR" in w for w in ex.warnings)


def test_ai_off_still_reads_qr():
    ex = extract_document(SAMPLES / "sharma_steel_invoice.pdf", None)
    assert ex.source == "qr" and ex.seller_gstin == "27AAECS5678F1Z0" and ex.total == D("11800.00")


def test_purchase_and_sale_suggestions(db, settings, truth):
    p = entry_from_document(db, settings, from_model_json(truth["sharma_steel_invoice.pdf"], "pdf_text"),
                            CUSTOMER, None, "sha-a", today=TODAY)
    assert (p.suggested_tag, p.party_name, p.status) == ("P", "Sharma Steel Suppliers", "pending")
    s = entry_from_document(db, settings, from_model_json(truth["sales_invoice_kumar.pdf"], "pdf_text"),
                            CUSTOMER, None, "sha-b", today=TODAY)
    assert (s.suggested_tag, s.party_name, s.party_gstin) == ("S", "Kumar Electricals", "29AAHFK4321L1Z4")


def test_payment_screenshot_is_receipt_against_bill(db, settings, truth):
    e = entry_from_document(db, settings, from_model_json(truth["upi_payment_kumar.png"], "vision"),
                            CUSTOMER, None, "sha-c", today=TODAY)
    assert (e.doc_type, e.suggested_tag, e.party_name, e.invoice_no, e.utr) == \
        ("payment", "R", "Kumar Electricals", "DT/26-27/118", "631245789012")


def test_quotation_is_not_a_bill(db, settings, truth):
    e = entry_from_document(db, settings, from_model_json(truth["quotation_xyz.pdf"], "pdf_text"),
                            CUSTOMER, None, "sha-d", today=TODAY)
    assert e.suggested_tag == "X" and e.status == "needs_review"


def test_checks_flag_bad_numbers(db, settings, truth):
    bad = dict(truth["sharma_steel_invoice.pdf"], total=12800, seller_gstin="27AAECS5678F1Z9",
               invoice_date="2027-01-15")
    e = entry_from_document(db, settings, from_model_json(bad, "vision"), CUSTOMER, None, "sha-e", today=TODAY)
    assert e.status == "needs_review"
    text = " | ".join(e.review_reasons)
    assert "total is 12800" in text and "check digit" in text and "in the future" in text


def test_duplicates(db, settings, truth):
    ex = from_model_json(truth["sharma_steel_invoice.pdf"], "pdf_text")
    first = entry_from_document(db, settings, ex, CUSTOMER, None, "sha-1", today=TODAY)
    again = entry_from_document(db, settings, ex, CUSTOMER, None, "sha-2", today=TODAY)
    assert again.status == "duplicate" and again.duplicate_of == first.id
    # Two payments against one bill are not duplicates; the same UPI reference is.
    pay = dict(truth["upi_payment_kumar.png"])
    p1 = entry_from_document(db, settings, from_model_json(pay, "vision"), CUSTOMER, None, "sha-3", today=TODAY)
    p2 = entry_from_document(db, settings, from_model_json(dict(pay, utr="999999999999"), "vision"),
                             CUSTOMER, None, "sha-4", today=TODAY)
    p3 = entry_from_document(db, settings, from_model_json(pay, "vision"), CUSTOMER, None, "sha-5", today=TODAY)
    assert p1.status == p2.status == "pending" and p3.status == "duplicate"


def test_typed_amount_direction_follows_the_writer(db):
    from_customer = entries_from_text(db, find_money("paid 5000 for bill 118"), CUSTOMER, None, "", when=TODAY)
    assert from_customer[0].suggested_tag == "R" and from_customer[0].invoice_date == TODAY
    staff = Sender("919811111111@s.whatsapp.net", "Ravi", from_me=True)
    from_us = entries_from_text(db, find_money("paid 5000 to you"), staff, None, "")
    assert from_us[0].suggested_tag == "PY"
    unclear = entries_from_text(db, find_money("amount 7000"), CUSTOMER, None, "")
    assert unclear[0].suggested_tag is None and unclear[0].status == "needs_review"


def test_unregistered_business_uses_names(db, settings, truth):
    settings.business.gstin = ""
    e = entry_from_document(db, settings, from_model_json(truth["sharma_steel_invoice.pdf"], "pdf_text"),
                            CUSTOMER, None, "sha-x", today=TODAY)
    assert e.suggested_tag == "P"


def test_reply_guard_blocks_invented_figures():
    kb = "Open 9:30 am to 7:30 pm. MS pipe Rs 480."
    def model(answer):
        return ScriptedLLM(lambda s, p, v: json.dumps(answer))
    ok = decide_reply(model({"can_answer": True, "reply": "MS pipe is Rs 480 per piece.", "holding_reply": "",
                             "question": "price"}), "Demo", kb, [], ["price of MS pipe?"])
    assert ok.answered and "480" in ok.text
    invented = decide_reply(model({"can_answer": True, "reply": "For you, Rs 420 only!", "holding_reply": "",
                                   "question": "discount"}), "Demo", kb, [], ["any discount?"])
    assert not invented.answered and "420" in invented.reason
    promise = decide_reply(model({"can_answer": False, "reply": "", "holding_reply": "I'll reply in 2 hours",
                                  "question": "bulk"}), "Demo", kb, [], ["bulk price?"])
    assert promise.text == HOLDING_REPLY
    assert not decide_reply(None, "Demo", kb, [], ["hi"]).answered
    assert not decide_reply(model({}), "Demo", "", [], ["hi"]).answered  # empty knowledge file


def test_extraction_round_trip_keeps_money_exact():
    ex = from_model_json({"doc_type": "invoice", "total": "1,18,000.50", "taxable": 100000.5}, "vision")
    assert ex.total == D("118000.50") and ex.taxable == D("100000.50")
    assert isinstance(Extraction().to_json(), str)


def test_our_gstin_fixture_is_valid():
    from munshig.gst import is_valid_gstin
    assert is_valid_gstin(OUR_GSTIN)
