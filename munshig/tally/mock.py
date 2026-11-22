"""A stand-in for TallyPrime's HTTP server, for tests and the offline demo.

It applies the checks real Tally applies that matter to MunshiG: the company
must be loaded, every ledger must exist, the voucher must balance, and in
educational mode only the 1st, 2nd and 31st are accepted. It is not a full
Tally: nothing here proves the XML is accepted by a real TallyPrime, which is
why the phase 0 checklist posts to a real (test) company.
"""

from __future__ import annotations

import itertools
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape, quoteattr

import httpx

DEFAULT_LEDGERS = {
    "Cash": "Cash-in-Hand",
    "Bank": "Bank Accounts",
    "Sales": "Sales Accounts",
    "Purchase": "Purchase Accounts",
    "Output CGST": "Duties & Taxes",
    "Output SGST": "Duties & Taxes",
    "Output IGST": "Duties & Taxes",
    "Input CGST": "Duties & Taxes",
    "Input SGST": "Duties & Taxes",
    "Input IGST": "Duties & Taxes",
    "Round Off": "Indirect Expenses",
    "General Expenses": "Indirect Expenses",
    "Rent": "Indirect Expenses",
    "Office Equipment": "Fixed Assets",
    "Unsecured Loans": "Loans (Liability)",
}


@dataclass
class MockVoucher:
    vch_id: int
    remote_id: str
    vtype: str
    date: str
    party: str | None
    reference: str | None
    lines: list[tuple[str, Decimal]]
    narration: str


@dataclass
class MockTally:
    company: str = "Demo Traders"
    educational_mode: bool = False
    ledgers: dict[str, dict] = field(default_factory=lambda: {
        name: {"parent": parent, "gstin": None} for name, parent in DEFAULT_LEDGERS.items()})
    vouchers: list[MockVoucher] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1001))

    # -- dispatch -----------------------------------------------------------
    def handle(self, body: str) -> str:
        self.requests.append(body)
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            return self._error(f"Invalid XML: {exc}")
        request = (root.findtext("HEADER/TALLYREQUEST") or "").strip()
        if request == "Import Data":
            company = root.findtext(".//SVCURRENTCOMPANY") or ""
            if company != self.company:
                return self._error(f"Could not find Company '{company}'")
            report = root.findtext(".//REPORTNAME")
            if report == "Vouchers":
                return self._import_vouchers(root)
            if report == "All Masters":
                return self._import_masters(root)
            return self._error(f"Unknown report {report}")
        if request == "Export":
            return self._export(root)
        return "<RESPONSE>Unknown Request, cannot be processed</RESPONSE>"

    # -- imports ------------------------------------------------------------
    def _import_vouchers(self, root: ET.Element) -> str:
        created = deleted = 0
        last_id = None
        for v in root.iter("VOUCHER"):
            action = v.get("ACTION", "Create")
            remote_id = v.get("REMOTEID", "")
            if action == "Delete":
                before = len(self.vouchers)
                self.vouchers = [x for x in self.vouchers if x.remote_id != remote_id]
                if len(self.vouchers) == before:
                    return self._error(f"Voucher with REMOTEID {remote_id} does not exist")
                deleted += 1
                continue
            vdate = v.findtext("DATE") or ""
            if self.educational_mode and vdate[-2:] not in ("01", "02", "31"):
                return self._error("Educational mode allows only the 1st, 2nd and 31st of a month")
            lines = []
            for le in v.iter("ALLLEDGERENTRIES.LIST"):
                name = le.findtext("LEDGERNAME") or ""
                if name not in self.ledgers:
                    return self._error(f"Ledger '{name}' does not exist!")
                amount = Decimal(le.findtext("AMOUNT") or "0")
                deemed = (le.findtext("ISDEEMEDPOSITIVE") or "No") == "Yes"
                if deemed != (amount < 0):
                    return self._error(f"ISDEEMEDPOSITIVE does not match the sign for '{name}'")
                lines.append((name, amount))
            if sum(a for _, a in lines) != 0:
                return self._error("Voucher totals do not match!")
            if any(x.remote_id == remote_id for x in self.vouchers):
                return self._error(f"Voucher with REMOTEID {remote_id} already exists")
            last_id = next(self._ids)
            self.vouchers.append(MockVoucher(
                vch_id=last_id, remote_id=remote_id, vtype=v.get("VCHTYPE", ""), date=vdate,
                party=v.findtext("PARTYLEDGERNAME"), reference=v.findtext("REFERENCE"),
                lines=lines, narration=v.findtext("NARRATION") or ""))
            created += 1
        return (f"<RESPONSE><CREATED>{created}</CREATED><ALTERED>0</ALTERED><DELETED>{deleted}</DELETED>"
                f"<LASTVCHID>{last_id or 0}</LASTVCHID><LASTMID>0</LASTMID><COMBINED>0</COMBINED>"
                "<IGNORED>0</IGNORED><ERRORS>0</ERRORS><CANCELLED>0</CANCELLED></RESPONSE>")

    def _import_masters(self, root: ET.Element) -> str:
        created = 0
        for led in root.iter("LEDGER"):
            name = led.get("NAME") or led.findtext("NAME.LIST/NAME") or ""
            if name in self.ledgers:
                return self._error(f"Ledger '{name}' already exists")
            parent = led.findtext("PARENT") or ""
            self.ledgers[name] = {"parent": parent, "gstin": led.findtext("PARTYGSTIN")}
            created += 1
        return f"<RESPONSE><CREATED>{created}</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS></RESPONSE>"

    # -- exports ------------------------------------------------------------
    def _export(self, root: ET.Element) -> str:
        coll = root.findtext("HEADER/ID")
        if coll == "MunshiGCompanies":
            return (f"<ENVELOPE><BODY><DATA><COLLECTION><COMPANY NAME={quoteattr(self.company)}>"
                    f"<NAME>{escape(self.company)}</NAME></COMPANY></COLLECTION></DATA></BODY></ENVELOPE>")
        if (root.findtext(".//SVCURRENTCOMPANY") or "") != self.company:
            return self._error("Could not find Company")
        if coll == "MunshiGLedgers":
            items = "".join(
                f"<LEDGER NAME={quoteattr(n)}><PARENT>{escape(d['parent'])}</PARENT>"
                + (f"<PARTYGSTIN>{escape(d['gstin'])}</PARTYGSTIN>" if d.get("gstin") else "")
                + "</LEDGER>"
                for n, d in self.ledgers.items())
            return f"<ENVELOPE><BODY><DATA><COLLECTION>{items}</COLLECTION></DATA></BODY></ENVELOPE>"
        if coll == "MunshiGFindVoucher":
            formula = root.findtext(".//SYSTEM") or ""
            party = re.search(r'\$PartyLedgerName = "([^"]*)"', formula)
            ref = re.search(r'\$Reference = "([^"]*)"', formula)
            hits = [x for x in self.vouchers
                    if party and ref and x.party == party.group(1) and x.reference == ref.group(1)]
            items = "".join(
                f"<VOUCHER><DATE>{x.date}</DATE><VOUCHERTYPENAME>{escape(x.vtype)}</VOUCHERTYPENAME>"
                f"<VOUCHERNUMBER>{x.vch_id}</VOUCHERNUMBER><REFERENCE>{escape(x.reference or '')}</REFERENCE>"
                f"<PARTYLEDGERNAME>{escape(x.party or '')}</PARTYLEDGERNAME></VOUCHER>"
                for x in hits)
            return f"<ENVELOPE><BODY><DATA><COLLECTION>{items}</COLLECTION></DATA></BODY></ENVELOPE>"
        return self._error(f"Unknown collection {coll}")

    @staticmethod
    def _error(message: str) -> str:
        return (f"<RESPONSE><LINEERROR>{escape(message)}</LINEERROR><CREATED>0</CREATED>"
                "<ALTERED>0</ALTERED><ERRORS>1</ERRORS></RESPONSE>")

    # -- plumbing -----------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(lambda req: httpx.Response(200, text=self.handle(req.content.decode("utf-8"))))

    def serve(self, host: str = "127.0.0.1", port: int = 9000) -> ThreadingHTTPServer:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                reply = mock.handle(self.rfile.read(length).decode("utf-8")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/xml; charset=utf-8")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
