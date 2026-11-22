"""Tally XML and every voucher rule, posted to the mock Tally."""

from datetime import date
from decimal import Decimal as D

import httpx
import pytest

from munshig.models import Entry, get_entry, save_entry
from munshig.posting import CannotPost, edu_mode_date, post_entry, undo_entry
from munshig.tally import xml as tx
from munshig.tally.client import TallyClient

SHARMA = dict(party_name="Sharma Steel Suppliers", party_gstin="27AAECS5678F1Z0")
KUMAR = dict(party_name="Kumar Electricals", party_gstin="29AAHFK4321L1Z4")


def approved(db, tag, **kw) -> Entry:
    e = Entry(doc_type=kw.pop("doc_type", "invoice"), tag=tag, status="approved", **kw)
    return save_entry(db, e)


def lines(mock, i=-1):
    return {name: amount for name, amount in mock.vouchers[i].lines}


def test_voucher_xml_signs_and_balance():
    v = tx.Voucher("Sales", date(2026, 11, 2), [tx.Line("Kumar", D("118"), True, tx.BillRef("A1", "New Ref")),
                                                 tx.Line("Sales", D("-100")), tx.Line("Output IGST", D("-18"))],
                   remote_id="munshig-x", party="Kumar", reference="A1")
    out = tx.voucher_xml(v, "Demo & Co")
    assert "<SVCURRENTCOMPANY>Demo &amp; Co</SVCURRENTCOMPANY>" in out
    assert "<LEDGERNAME>Kumar</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><ISPARTYLEDGER>Yes</ISPARTYLEDGER><AMOUNT>-118.00</AMOUNT>" in out
    assert "<LEDGERNAME>Sales</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><ISPARTYLEDGER>No</ISPARTYLEDGER><AMOUNT>100.00</AMOUNT>" in out
    assert "<BILLTYPE>New Ref</BILLTYPE>" in out and 'REMOTEID="munshig-x"' in out
    v.lines.append(tx.Line("Round Off", D("1")))
    with pytest.raises(ValueError):
        tx.voucher_xml(v, "Demo")


def test_parse_responses():
    ok = tx.parse_import_response("<RESPONSE><CREATED>1</CREATED><ERRORS>0</ERRORS><LASTVCHID>7</LASTVCHID></RESPONSE>")
    assert ok.ok and ok.last_vch_id == "7"
    bad = tx.parse_import_response("<RESPONSE><LINEERROR>Ledger 'X' does not exist!</LINEERROR><CREATED>0</CREATED>"
                                   "<ERRORS>1</ERRORS></RESPONSE>")
    assert not bad.ok and bad.messages == ["Ledger 'X' does not exist!"]
    assert not tx.parse_import_response("<RESPONSE>Unknown Request, cannot be processed</RESPONSE>").ok
    assert not tx.parse_import_response("garbage").ok
    # Tally sometimes sends control-character references that strict XML parsers reject.
    assert tx.parse_import_response("<RESPONSE><CREATED>1</CREATED><X>&#4;</X></RESPONSE>").ok


def test_purchase_with_gst_creates_party_and_posts(db, settings, tally, mock):
    e = approved(db, "P", **SHARMA, invoice_no="SS/1", invoice_date=date(2026, 11, 2),
                 taxable=D("10000"), cgst=D("900"), sgst=D("900"), total=D("11800"))
    e = post_entry(db, settings, tally, e)
    assert e.status == "posted", e.tally_error
    assert mock.ledgers["Sharma Steel Suppliers"]["parent"] == "Sundry Creditors"
    assert mock.vouchers[0].vtype == "Purchase" and mock.vouchers[0].reference == "SS/1"
    # Tally's sign: debit negative.
    assert lines(mock) == {"Purchase": D("-10000.00"), "Input CGST": D("-900.00"), "Input SGST": D("-900.00"),
                           "Sharma Steel Suppliers": D("11800.00")}


def test_sale_interstate_with_round_off(db, settings, tally, mock):
    e = approved(db, "S", **KUMAR, invoice_no="DT/9", invoice_date=date(2026, 11, 2),
                 taxable=D("847.00"), igst=D("152.46"), round_off=D("0.54"), total=D("1000"))
    e = post_entry(db, settings, tally, e)
    assert e.status == "posted", e.tally_error
    assert lines(mock) == {"Kumar Electricals": D("-1000.00"), "Sales": D("847.00"), "Output IGST": D("152.46"),
                           "Round Off": D("0.54")}
    assert mock.ledgers["Kumar Electricals"]["parent"] == "Sundry Debtors"


def test_expense_with_bill_is_purchase_voucher_and_without_bill_is_payment(db, settings, tally, mock):
    e = approved(db, "E", party_name="MSEB", invoice_no="EB-77", invoice_date=date(2026, 11, 2),
                 total=D("2300"), ledger="Rent")
    assert post_entry(db, settings, tally, e).status == "posted"
    assert mock.vouchers[-1].vtype == "Purchase" and lines(mock) == {"Rent": D("-2300.00"), "MSEB": D("2300.00")}
    cash = approved(db, "E", doc_type="amount", party_name="Tea stall", total=D("150"),
                    invoice_date=date(2026, 11, 2))
    assert post_entry(db, settings, tally, cash).status == "posted"
    assert mock.vouchers[-1].vtype == "Payment"
    assert lines(mock) == {"General Expenses": D("-150.00"), "Cash": D("150.00")}


def test_asset_and_liability_need_a_ledger(db, settings, tally, mock):
    a = approved(db, "A", party_name="Comfort Furniture House", invoice_no="CF-1", invoice_date=date(2026, 11, 2),
                 taxable=D("5500"), cgst=D("495"), sgst=D("495"), total=D("6490"))
    a = post_entry(db, settings, tally, a)
    assert a.status == "failed" and "fixed-asset ledger" in a.tally_error
    a.ledger, a.status = "Office Equipment", "approved"
    assert post_entry(db, settings, tally, a).status == "posted"
    assert lines(mock)["Office Equipment"] == D("-5500.00")

    loan = approved(db, "L", doc_type="amount", party_name="Uncle", total=D("100000"), invoice_date=date(2026, 11, 2),
                    ledger="Unsecured Loans")
    assert post_entry(db, settings, tally, loan).status == "posted"
    assert mock.vouchers[-1].vtype == "Receipt"
    assert lines(mock) == {"Bank": D("-100000.00"), "Unsecured Loans": D("100000.00")}


def test_receipt_against_bill_and_payment(db, settings, tally, mock):
    r = approved(db, "R", doc_type="payment", **KUMAR, invoice_no="DT/9", utr="631245789012",
                 total=D("25000"), invoice_date=date(2026, 11, 2))
    assert post_entry(db, settings, tally, r).status == "posted"
    v = mock.vouchers[-1]
    assert v.vtype == "Receipt" and v.reference == "631245789012"
    assert "<BILLTYPE>Agst Ref</BILLTYPE>" in mock.requests[-1] and "<NAME>DT/9</NAME>" in mock.requests[-1]
    p = approved(db, "PY", doc_type="amount", **SHARMA, total=D("5000"), invoice_date=date(2026, 11, 2))
    assert post_entry(db, settings, tally, p).status == "posted"
    assert lines(mock) == {"Sharma Steel Suppliers": D("-5000.00"), "Bank": D("5000.00")}
    assert "<BILLTYPE>On Account</BILLTYPE>" in mock.requests[-1]


def test_already_in_tally_is_not_posted_twice(db, settings, tally, mock):
    kw = dict(**SHARMA, invoice_no="SS/1", invoice_date=date(2026, 11, 2), taxable=D("100"), total=D("100"))
    assert post_entry(db, settings, tally, approved(db, "P", **kw)).status == "posted"
    second = post_entry(db, settings, tally, approved(db, "P", **kw))
    assert second.status == "duplicate" and "Already in Tally" in second.tally_error
    assert len(mock.vouchers) == 1


def test_educational_mode_dates(db, settings, tally, mock):
    assert edu_mode_date(date(2026, 11, 14), "nearest_allowed")[0] == date(2026, 11, 2)
    assert edu_mode_date(date(2026, 10, 29), "nearest_allowed")[0] == date(2026, 10, 31)
    assert edu_mode_date(date(2026, 2, 27), "nearest_allowed")[0] == date(2026, 2, 2)  # no 31st in February
    assert edu_mode_date(date(2026, 11, 1), "refuse") == (date(2026, 11, 1), None)
    with pytest.raises(CannotPost):
        edu_mode_date(date(2026, 11, 14), "refuse")

    mock.educational_mode = settings.tally.educational_mode = True
    e = approved(db, "P", **SHARMA, invoice_no="SS/5", invoice_date=date(2026, 11, 14), total=D("100"))
    e = post_entry(db, settings, tally, e)
    assert e.status == "posted" and mock.vouchers[-1].date == "20261102"
    assert "Actual date 14-11-2026" in mock.vouchers[-1].narration


def test_tally_errors_reach_the_owner_in_plain_words(db, settings, tally, mock):
    del mock.ledgers["Input CGST"]
    e = approved(db, "P", **SHARMA, invoice_no="SS/1", invoice_date=date(2026, 11, 2),
                 taxable=D("100"), cgst=D("9"), sgst=D("9"), total=D("118"))
    e = post_entry(db, settings, tally, e)
    assert e.status == "failed" and "Ledger 'Input CGST' does not exist" in e.tally_error


def test_tally_closed_keeps_entry_approved_for_retry(db, settings):
    def refuse(request):
        raise httpx.ConnectError("connection refused")
    closed = TallyClient(company="Demo Traders", transport=httpx.MockTransport(refuse))
    e = approved(db, "P", **SHARMA, invoice_no="SS/1", invoice_date=date(2026, 11, 2), total=D("100"))
    e = post_entry(db, settings, closed, e)
    assert e.status == "approved" and "Could not reach Tally" in e.tally_error


def test_undo_same_day(db, settings, tally, mock):
    e = post_entry(db, settings, tally, approved(db, "P", **SHARMA, invoice_no="SS/1",
                                                 invoice_date=date(2026, 11, 2), total=D("100")))
    assert len(mock.vouchers) == 1
    assert "removed from Tally" in undo_entry(db, tally, e)
    assert mock.vouchers == [] and get_entry(db, e.id).status == "pending"


def test_parse_collection_ignores_cmpinfo_counts():
    # Real TallyPrime replies start with CMPINFO object counts named like the objects.
    reply = """<ENVELOPE><BODY><DESC><CMPINFO><LEDGER>50</LEDGER><VOUCHER>8</VOUCHER></CMPINFO></DESC>
    <DATA><COLLECTION>   </COLLECTION></DATA></BODY></ENVELOPE>"""
    assert tx.parse_collection(reply, "VOUCHER") == []
    assert tx.parse_collection(reply, "LEDGER") == []
