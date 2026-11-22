"""Deterministic rules: GST checks, amounts in chat, e-invoice QR, owner commands, digest money format."""

from decimal import Decimal as D

from munshig.commands import parse
from munshig.digest import inr
from munshig.einvoice import find_einvoice, parse_qr_text
from munshig.extract import load_pages
from munshig.gst import check_tax_split, is_interstate, is_valid_gstin, state_of
from munshig.money import find_money

from .conftest import SAMPLES


def test_gstin_check_digit():
    assert is_valid_gstin("27AAPFU0939F1ZV")  # a widely published valid example
    assert is_valid_gstin("27aapfu0939f1zv")
    assert not is_valid_gstin("27AAPFU0939F1ZX")  # wrong check digit
    assert not is_valid_gstin("00AAPFU0939F1ZV")  # no such state
    assert not is_valid_gstin("27AAPFU0939F1Z")  # too short
    assert state_of("29AAHFK4321L1Z4") == "Karnataka"
    assert is_interstate("27AAPFU0939F1ZV", "29AAHFK4321L1Z4") is True
    assert is_interstate("27AAPFU0939F1ZV", None) is None


def test_tax_split_checks():
    ok = check_tax_split(D("10000"), D("900"), D("900"), None, None, D("11800"), interstate=False)
    assert ok.ok and ok.round_off == 0
    rounded = check_tax_split(D("847.00"), D("76.23"), D("76.23"), None, None, D("1000"), interstate=False)
    assert rounded.ok and rounded.round_off == D("0.54")
    off = check_tax_split(D("10000"), D("900"), D("900"), None, None, D("12800"), interstate=False)
    assert not off.ok and "total is 12800" in off.reasons[0]
    wrong_kind = check_tax_split(D("10000"), D("900"), D("900"), None, None, D("11800"), interstate=True)
    assert not wrong_kind.ok and "expected IGST" in wrong_kind.reasons[0]
    no_breakup = check_tax_split(None, None, None, None, None, D("500"), interstate=None)
    assert no_breakup.ok


def test_money_in_chat():
    m = find_money("Sent 5000 advance for the order, UTR 412398765432")
    assert m[0].amount == D("5000") and m[0].direction == "paid" and m[0].utr == "412398765432"
    assert find_money("bhai 25k bhej diya")[0].amount == D("25000")
    assert find_money("Rs. 1,25,000/- received")[0].direction == "received"
    assert find_money("₹2.5 lakh")[0].amount == D("250000")
    # Quantities, phone numbers and dates are not money.
    assert find_money("send 10 pcs, call me on 9812345678") == []
    assert find_money("meeting on 12/11 at 5") == []


def test_einvoice_qr_from_sample_pdf():
    images, text = load_pages(SAMPLES / "sharma_steel_invoice.pdf")
    qr = find_einvoice(images)
    assert qr and qr.seller_gstin == "27AAECS5678F1Z0" and qr.total == D("11800") and qr.doc_no == "SS/2026/0412"
    assert "Sharma Steel Suppliers" in text
    assert parse_qr_text("not a jwt") is None


def test_commands():
    p = parse("1P 2r 3 E Rent\n4PY\nall\n5?\nF2 Rs 420 each above 40 units\nD3\nD4 We'll call you\nD5 no\nundo 6\nwhat?")
    assert [(t.number, t.tag, t.ledger) for t in p.tags] == [(1, "P", None), (2, "R", None), (3, "E", "Rent"),
                                                             (4, "PY", None)]
    assert p.accept_all and p.details == [5] and p.undo == [6]
    assert p.followup_answers == [(2, "Rs 420 each above 40 units")]
    assert p.draft_send == [(3, None), (4, "We'll call you")] and p.draft_discard == [5]
    assert p.unknown == ["what?"]
    assert parse("hello there").empty
    assert parse("3 A Furniture 2nd floor").tags[0].ledger == "Furniture 2nd floor"


def test_inr_grouping():
    assert inr(D("1234567.5")) == "₹12,34,567.50"
    assert inr(D("11800")) == "₹11,800"
    assert inr(D("999")) == "₹999"
    assert inr(None) == "₹?"
