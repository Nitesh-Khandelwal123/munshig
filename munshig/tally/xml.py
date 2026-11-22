"""Build and read Tally XML messages.

Uses the classic "Import Data" / "Export Data" envelope, which TallyPrime and
Tally.ERP 9 both accept on their HTTP server (port 9000 by default).

Sign convention inside MunshiG: a positive amount is a debit, a negative
amount is a credit, and every voucher must sum to zero. Tally's own XML
convention is the opposite sign with an ISDEEMEDPOSITIVE flag, handled here.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape, quoteattr

TWO = Decimal("0.01")


@dataclass
class BillRef:
    name: str
    kind: str  # "New Ref", "Agst Ref" or "On Account"


@dataclass
class Line:
    ledger: str
    amount: Decimal  # positive = debit, negative = credit
    is_party: bool = False
    bill: BillRef | None = None


@dataclass
class Voucher:
    vtype: str  # Sales, Purchase, Receipt, Payment, Journal
    vdate: date
    lines: list[Line]
    remote_id: str
    party: str | None = None
    reference: str | None = None
    reference_date: date | None = None
    narration: str = ""
    notes: list[str] = field(default_factory=list)

    def check_balanced(self) -> None:
        total = sum((ln.amount for ln in self.lines), Decimal("0"))
        if total.quantize(TWO) != Decimal("0.00"):
            raise ValueError(f"Voucher does not balance: debits minus credits = {total}")
        if not self.lines:
            raise ValueError("Voucher has no ledger lines")
        for ln in self.lines:
            if not ln.ledger:
                raise ValueError("A ledger line has no ledger name")


def tally_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def _amount(debit_positive: Decimal) -> tuple[str, str]:
    """Return (ISDEEMEDPOSITIVE, AMOUNT) in Tally's convention."""
    value = (-debit_positive).quantize(TWO)
    return ("Yes" if debit_positive > 0 else "No", f"{value:.2f}")


def _t(tag: str, value: str | None) -> str:
    return f"<{tag}>{escape(value)}</{tag}>" if value is not None else ""


def _import_envelope(report: str, company: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>"
        "<BODY><IMPORTDATA><REQUESTDESC>"
        f"<REPORTNAME>{escape(report)}</REPORTNAME>"
        f"<STATICVARIABLES><SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY></STATICVARIABLES>"
        "</REQUESTDESC><REQUESTDATA>"
        f'<TALLYMESSAGE xmlns:UDF="TallyUDF">{body}</TALLYMESSAGE>'
        "</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
    )


def voucher_xml(v: Voucher, company: str) -> str:
    v.check_balanced()
    lines_xml = []
    for ln in v.lines:
        deemed, amount = _amount(ln.amount)
        bill = ""
        if ln.bill:
            bill = (
                "<BILLALLOCATIONS.LIST>"
                + _t("NAME", ln.bill.name)
                + _t("BILLTYPE", ln.bill.kind)
                + _t("AMOUNT", amount)
                + "</BILLALLOCATIONS.LIST>"
            )
        lines_xml.append(
            "<ALLLEDGERENTRIES.LIST>"
            + _t("LEDGERNAME", ln.ledger)
            + _t("ISDEEMEDPOSITIVE", deemed)
            + _t("ISPARTYLEDGER", "Yes" if ln.is_party else "No")
            + _t("AMOUNT", amount)
            + bill
            + "</ALLLEDGERENTRIES.LIST>"
        )
    body = (
        f"<VOUCHER REMOTEID={quoteattr(v.remote_id)} VCHTYPE={quoteattr(v.vtype)} "
        'ACTION="Create" OBJVIEW="Accounting Voucher View">'
        + _t("DATE", tally_date(v.vdate))
        + _t("EFFECTIVEDATE", tally_date(v.vdate))
        + _t("VOUCHERTYPENAME", v.vtype)
        + _t("PERSISTEDVIEW", "Accounting Voucher View")
        + _t("PARTYLEDGERNAME", v.party)
        + _t("REFERENCE", v.reference)
        + (_t("REFERENCEDATE", tally_date(v.reference_date)) if v.reference_date else "")
        + _t("NARRATION", v.narration)
        + "".join(lines_xml)
        + "</VOUCHER>"
    )
    return _import_envelope("Vouchers", company, body)


def delete_voucher_xml(remote_id: str, vtype: str, vdate: date, company: str) -> str:
    body = (
        f"<VOUCHER REMOTEID={quoteattr(remote_id)} VCHTYPE={quoteattr(vtype)} ACTION=\"Delete\">"
        + _t("DATE", tally_date(vdate))
        + _t("VOUCHERTYPENAME", vtype)
        + "</VOUCHER>"
    )
    return _import_envelope("Vouchers", company, body)


def ledger_xml(name: str, parent: str, company: str, gstin: str | None = None,
               state: str | None = None, billwise: bool = True) -> str:
    body = (
        f'<LEDGER NAME={quoteattr(name)} ACTION="Create">'
        f"<NAME.LIST>{_t('NAME', name)}</NAME.LIST>"
        + _t("PARENT", parent)
        + _t("ISBILLWISEON", "Yes" if billwise else "No")
        + _t("PARTYGSTIN", gstin)
        + _t("LEDSTATENAME", state)
        + "</LEDGER>"
    )
    return _import_envelope("All Masters", company, body)


def _collection_request(name: str, company: str | None, coll_type: str, fetch: str,
                        filter_formula: str | None = None, from_date: date | None = None,
                        to_date: date | None = None) -> str:
    static = "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
    if company:
        static += f"<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>"
    if from_date and to_date:
        static += f"<SVFROMDATE>{tally_date(from_date)}</SVFROMDATE><SVTODATE>{tally_date(to_date)}</SVTODATE>"
    filters = ""
    if filter_formula:
        filters = (
            "<FILTERS>MunshiGFilter</FILTERS></COLLECTION>"
            f'<SYSTEM TYPE="Formulae" NAME="MunshiGFilter">{escape(filter_formula)}</SYSTEM>'
        )
    else:
        filters = "</COLLECTION>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Collection</TYPE><ID>{escape(name)}</ID></HEADER>"
        f"<BODY><DESC><STATICVARIABLES>{static}</STATICVARIABLES>"
        f'<TDL><TDLMESSAGE><COLLECTION NAME={quoteattr(name)} ISMODIFY="No">'
        f"<TYPE>{escape(coll_type)}</TYPE><FETCH>{escape(fetch)}</FETCH>"
        f"{filters}</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )


def companies_request() -> str:
    return _collection_request("MunshiGCompanies", None, "Company", "Name, BooksFrom, StartingFrom")


def ledgers_request(company: str) -> str:
    return _collection_request("MunshiGLedgers", company, "Ledger", "Name, Parent, PartyGSTIN")


def _formula_str(value: str) -> str:
    return '"' + value.replace('"', "") + '"'


def find_voucher_request(company: str, party: str, reference: str, from_date: date, to_date: date) -> str:
    formula = f"$PartyLedgerName = {_formula_str(party)} AND $Reference = {_formula_str(reference)}"
    return _collection_request("MunshiGFindVoucher", company, "Voucher",
                               "Date, VoucherTypeName, VoucherNumber, Reference, PartyLedgerName",
                               formula, from_date, to_date)


# ---------------------------------------------------------------------------
# Reading responses
# ---------------------------------------------------------------------------

_BAD_CHARREF = re.compile(r"&#(?:x0*[0-8bcef]|x0*1[0-9a-f]|0*[0-8]|0*1[1-9]|0*2[0-9]|0*3[01]);", re.IGNORECASE)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _parse(xml_text: str) -> ET.Element | None:
    cleaned = _CTRL.sub("", _BAD_CHARREF.sub("", xml_text or ""))
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError:
        return None


@dataclass
class ImportResult:
    created: int = 0
    altered: int = 0
    deleted: int = 0
    errors: int = 0
    exceptions: int = 0
    last_vch_id: str | None = None
    messages: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.errors == 0 and self.exceptions == 0 and not self.messages and (
            self.created or self.altered or self.deleted
        )


def parse_import_response(xml_text: str) -> ImportResult:
    result = ImportResult(raw=xml_text)
    root = _parse(xml_text)
    if root is None:
        result.errors = 1
        result.messages.append(f"Tally sent a reply MunshiG could not read: {xml_text[:200]!r}")
        return result

    def num(tag: str) -> int:
        el = root.find(f".//{tag}")
        try:
            return int((el.text or "0").strip()) if el is not None else 0
        except ValueError:
            return 0

    result.created, result.altered, result.deleted = num("CREATED"), num("ALTERED"), num("DELETED")
    result.errors, result.exceptions = num("ERRORS"), num("EXCEPTIONS")
    el = root.find(".//LASTVCHID")
    result.last_vch_id = el.text.strip() if el is not None and el.text else None
    result.messages = [e.text.strip() for e in root.iter("LINEERROR") if e.text and e.text.strip()]
    if root.tag == "RESPONSE" and root.text and root.text.strip() and not len(root):
        result.messages.append(root.text.strip())
    return result


def parse_collection(xml_text: str, element: str) -> list[dict[str, str]]:
    """Return each <element> in an export reply as a dict of its child values."""
    root = _parse(xml_text)
    if root is None:
        return []
    # TallyPrime prefixes replies with <CMPINFO>, whose children are object counts
    # named like the objects themselves (<VOUCHER>8</VOUCHER>); those aren't results.
    counts = {id(child) for info in root.iter("CMPINFO") for child in info}
    items = []
    for el in root.iter(element):
        if id(el) in counts:
            continue
        item: dict[str, str] = {}
        if el.get("NAME"):
            item["NAME"] = el.get("NAME")
        for child in el:
            tag = child.tag.split(".")[0]
            text = (child.text or "").strip()
            if text and tag not in item:
                item[tag] = text
            if tag == "NAME" and not text:
                # TallyPrime nests names in NAME.LIST/NAME
                nested = child.find("NAME")
                if nested is not None and nested.text:
                    item.setdefault("NAME", nested.text.strip())
        if "NAME" not in item:
            name_list = el.find("NAME.LIST/NAME")
            if name_list is not None and name_list.text:
                item["NAME"] = name_list.text.strip()
        items.append(item)
    return items
