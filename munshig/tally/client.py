"""Talk to TallyPrime's HTTP server.

Tally must be open, with the chosen company loaded, and its server switched
on (TallyPrime: F1 Help > Settings > Connectivity > Client/Server
configuration, "TallyPrime acts as" Server or Both, port 9000). The exact menu
path varies a little between Tally versions; the setup wizard shows it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

from . import xml as tx


class TallyUnavailable(Exception):
    """Tally is closed, its server is off, or the port is blocked."""


@dataclass
class TallyLedger:
    name: str
    parent: str
    gstin: str | None


class TallyClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 9000, company: str = "",
                 timeout: float = 20.0, transport: httpx.BaseTransport | None = None):
        self.url = f"http://{host}:{port}"
        self.company = company
        # Tally is on this PC or the office network: never route it through a proxy.
        self._client = httpx.Client(timeout=timeout, transport=transport, trust_env=False)

    def _post(self, body: str) -> str:
        try:
            resp = self._client.post(self.url, content=body.encode("utf-8"),
                                     headers={"Content-Type": "text/xml; charset=utf-8"})
        except httpx.HTTPError as exc:
            raise TallyUnavailable(
                f"Could not reach Tally at {self.url}. Is Tally open with its server on port "
                f"{self.url.rsplit(':', 1)[-1]}? ({exc.__class__.__name__})"
            ) from exc
        if resp.status_code != 200:
            raise TallyUnavailable(f"Tally answered with HTTP {resp.status_code}")
        return resp.text

    def ping(self) -> bool:
        try:
            self._post(tx.companies_request())
            return True
        except TallyUnavailable:
            return False

    def companies(self) -> list[str]:
        reply = self._post(tx.companies_request())
        return [c["NAME"] for c in tx.parse_collection(reply, "COMPANY") if c.get("NAME")]

    def ledgers(self) -> list[TallyLedger]:
        reply = self._post(tx.ledgers_request(self.company))
        out = []
        for item in tx.parse_collection(reply, "LEDGER"):
            if item.get("NAME"):
                out.append(TallyLedger(item["NAME"], item.get("PARENT", ""), item.get("PARTYGSTIN")))
        return out

    def post_voucher(self, voucher: tx.Voucher) -> tx.ImportResult:
        return tx.parse_import_response(self._post(tx.voucher_xml(voucher, self.company)))

    def delete_voucher(self, remote_id: str, vtype: str, vdate: date) -> tx.ImportResult:
        return tx.parse_import_response(self._post(tx.delete_voucher_xml(remote_id, vtype, vdate, self.company)))

    def create_ledger(self, name: str, parent: str, gstin: str | None = None,
                      state: str | None = None) -> tx.ImportResult:
        return tx.parse_import_response(self._post(tx.ledger_xml(name, parent, self.company, gstin, state)))

    def find_voucher(self, party: str, reference: str, around: date) -> list[dict[str, str]]:
        """Vouchers for this party with this reference in the invoice's financial year."""
        start_year = around.year if around.month >= 4 else around.year - 1
        reply = self._post(tx.find_voucher_request(
            self.company, party, reference, date(start_year, 4, 1), date(start_year + 1, 3, 31)))
        return tx.parse_collection(reply, "VOUCHER")

    def close(self) -> None:
        self._client.close()
