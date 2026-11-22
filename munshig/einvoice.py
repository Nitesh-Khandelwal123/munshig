"""Read the signed QR code printed on GST e-invoices.

The QR holds a signed JWT issued by the Invoice Registration Portal. Its
payload carries the seller and buyer GSTIN, document number, type and date,
total invoice value, item count, main HSN code and the IRN. Reading it gives
exact values with no AI involved, so it is always tried first.

Signature verification needs the portal's public key and is not done yet:
the decoded values are still cross-checked against the rest of the invoice
and approved by the owner before anything is posted.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PIL import Image


@dataclass
class EInvoiceQR:
    seller_gstin: str | None
    buyer_gstin: str | None
    doc_no: str | None
    doc_type: str | None  # INV, CRN (credit note), DBN (debit note)
    doc_date: date | None
    total: Decimal | None
    item_count: int | None
    main_hsn: str | None
    irn: str | None
    raw: dict


def _b64url_json(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()))


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_qr_text(text: str) -> EInvoiceQR | None:
    """Parse the text inside an e-invoice QR. Returns None if it isn't one."""
    parts = text.strip().split(".")
    if len(parts) < 2:
        return None
    try:
        payload = _b64url_json(parts[1])
    except (ValueError, json.JSONDecodeError):
        return None
    data = payload.get("data", payload)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict) or not ({"SellerGstin", "Irn", "DocNo"} & data.keys()):
        return None

    total = None
    if data.get("TotInvVal") is not None:
        try:
            total = Decimal(str(data["TotInvVal"]))
        except InvalidOperation:
            total = None
    item_count = data.get("ItemCnt")
    return EInvoiceQR(
        seller_gstin=data.get("SellerGstin"),
        buyer_gstin=data.get("BuyerGstin"),
        doc_no=data.get("DocNo"),
        doc_type=data.get("DocTyp"),
        doc_date=_parse_date(data.get("DocDt")),
        total=total,
        item_count=int(item_count) if isinstance(item_count, (int, str)) and str(item_count).isdigit() else None,
        main_hsn=data.get("MainHsnCode"),
        irn=data.get("Irn"),
        raw=data,
    )


def read_qr_texts(image: Image.Image) -> list[str]:
    """Return the text of every QR code found in an image."""
    import zxingcpp

    results = zxingcpp.read_barcodes(image)
    return [r.text for r in results if r.text]


def find_einvoice(images: list[Image.Image]) -> EInvoiceQR | None:
    for img in images:
        for text in read_qr_texts(img):
            parsed = parse_qr_text(text)
            if parsed:
                return parsed
    return None


def find_einvoice_in_file(path: Path) -> EInvoiceQR | None:
    with Image.open(path) as img:
        return find_einvoice([img.convert("RGB")])
