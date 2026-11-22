"""Read an invoice, credit note or payment screenshot into fixed fields.

Order of trust:
1. The e-invoice QR code (exact, issued by the GST portal).
2. A PDF's own text layer, read by the chat model.
3. The vision model reading the image.

Where the QR and the AI disagree, the QR value wins and the disagreement is
recorded as a review reason so the owner sees it.
"""

from __future__ import annotations

import io
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PIL import Image

from .einvoice import EInvoiceQR, find_einvoice
from .gst import normalize_gstin
from .llm import LLM, LLMError, parse_json

MAX_PAGES = 3
MIN_TEXT_CHARS = 80

SYSTEM = (
    "You read Indian business documents (GST tax invoices, bills, credit notes, UPI payment "
    "screenshots) and copy their values into JSON. Copy exactly what is printed. Never guess: "
    "use null for anything not clearly visible. Amounts are plain numbers without commas or "
    "currency signs. Dates are YYYY-MM-DD."
)

PROMPT = """Classify this document and extract its values.

doc_type is one of:
- "invoice": a tax invoice or bill for goods or services
- "credit_note" or "debit_note"
- "payment": a UPI/bank payment confirmation or screenshot
- "not_a_bill": a quotation, proforma invoice, estimate, delivery challan, order or anything else

For an invoice or note fill seller/buyer names and GSTINs, the invoice number and date, the
taxable value (before tax), CGST, SGST, IGST and cess amounts, round-off and the grand total.
For a payment fill amount (as total), payer_name, payee_name, utr (the UPI/UTR reference),
payment_status ("success", "failed" or "pending"), the date, and against_bill: an invoice or
bill number the payment note says it is for (null if none).
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": ["invoice", "credit_note", "debit_note", "payment", "not_a_bill"]},
        "seller_name": {"type": ["string", "null"]},
        "seller_gstin": {"type": ["string", "null"]},
        "buyer_name": {"type": ["string", "null"]},
        "buyer_gstin": {"type": ["string", "null"]},
        "invoice_no": {"type": ["string", "null"]},
        "invoice_date": {"type": ["string", "null"]},
        "taxable": {"type": ["number", "null"]},
        "cgst": {"type": ["number", "null"]},
        "sgst": {"type": ["number", "null"]},
        "igst": {"type": ["number", "null"]},
        "cess": {"type": ["number", "null"]},
        "round_off": {"type": ["number", "null"]},
        "total": {"type": ["number", "null"]},
        "payer_name": {"type": ["string", "null"]},
        "payee_name": {"type": ["string", "null"]},
        "utr": {"type": ["string", "null"]},
        "payment_status": {"type": ["string", "null"]},
        "not_a_bill_kind": {"type": ["string", "null"]},
        "against_bill": {"type": ["string", "null"]},
    },
    "required": ["doc_type"],
}

_MONEY = ("taxable", "cgst", "sgst", "igst", "cess", "round_off", "total")


@dataclass
class Extraction:
    doc_type: str = "unknown"
    seller_name: str | None = None
    seller_gstin: str | None = None
    buyer_name: str | None = None
    buyer_gstin: str | None = None
    invoice_no: str | None = None
    invoice_date: date | None = None
    taxable: Decimal | None = None
    cgst: Decimal | None = None
    sgst: Decimal | None = None
    igst: Decimal | None = None
    cess: Decimal | None = None
    round_off: Decimal | None = None
    total: Decimal | None = None
    payer_name: str | None = None
    payee_name: str | None = None
    utr: str | None = None
    payment_status: str | None = None
    not_a_bill_kind: str | None = None
    against_bill: str | None = None
    source: str = "none"
    irn: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def _money(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("₹", "").strip()).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _date(value) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def from_model_json(data: dict, source: str) -> Extraction:
    ex = Extraction(doc_type=data.get("doc_type") or "unknown", source=source)
    for key in ("seller_name", "buyer_name", "invoice_no", "payer_name", "payee_name",
                "payment_status", "not_a_bill_kind", "against_bill"):
        setattr(ex, key, _clean(data.get(key)))
    ex.utr = _clean(data.get("utr"))
    ex.seller_gstin = normalize_gstin(data.get("seller_gstin"))
    ex.buyer_gstin = normalize_gstin(data.get("buyer_gstin"))
    ex.invoice_date = _date(data.get("invoice_date"))
    for key in _MONEY:
        setattr(ex, key, _money(data.get(key)))
    return ex


def merge_qr(ex: Extraction, qr: EInvoiceQR) -> Extraction:
    """QR values are authoritative. Disagreements become warnings."""
    def take(attr: str, qr_value, label: str):
        current = getattr(ex, attr)
        if qr_value in (None, ""):
            return
        if current not in (None, "") and str(current).upper() != str(qr_value).upper():
            ex.warnings.append(f"{label} on the page ({current}) differs from the e-invoice QR ({qr_value}); using the QR")
        setattr(ex, attr, qr_value)

    take("seller_gstin", normalize_gstin(qr.seller_gstin), "Seller GSTIN")
    take("buyer_gstin", normalize_gstin(qr.buyer_gstin), "Buyer GSTIN")
    take("invoice_no", qr.doc_no, "Invoice number")
    take("invoice_date", qr.doc_date, "Invoice date")
    take("total", qr.total.quantize(Decimal("0.01")) if qr.total is not None else None, "Total")
    if qr.doc_type == "CRN":
        ex.doc_type = "credit_note"
    elif qr.doc_type == "DBN":
        ex.doc_type = "debit_note"
    elif ex.doc_type in ("unknown", "not_a_bill"):
        ex.doc_type = "invoice"
    ex.irn = qr.irn
    ex.source = "qr" if ex.source == "none" else f"qr+{ex.source}"
    return ex


def load_pages(path: Path, mime: str | None = None) -> tuple[list[Image.Image], str]:
    """Return (page images, text layer). Images only for picture files."""
    path = Path(path)
    is_pdf = (mime or "").endswith("pdf") or path.suffix.lower() == ".pdf"
    if not is_pdf:
        with Image.open(path) as img:
            return [img.convert("RGB")], ""

    import pypdfium2 as pdfium
    from pypdf import PdfReader

    text = ""
    try:
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PAGES])
    except Exception:  # a damaged or encrypted PDF still gets the image route
        text = ""
    images = []
    pdf = pdfium.PdfDocument(str(path))
    try:
        for i in range(min(len(pdf), MAX_PAGES)):
            images.append(pdf[i].render(scale=2).to_pil().convert("RGB"))
    finally:
        pdf.close()
    return images, text


def _png(img: Image.Image, max_side: int = 1600) -> bytes:
    img = img.copy()
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def extract_document(path: Path, llm: LLM | None, mime: str | None = None) -> Extraction:
    images, text = load_pages(path, mime)
    qr = find_einvoice(images)

    ex = Extraction()
    if llm is not None:
        try:
            if len(text.strip()) >= MIN_TEXT_CHARS:
                reply = llm.complete(SYSTEM, PROMPT + "\n\nDocument text:\n" + text[:12000], schema=SCHEMA)
                ex = from_model_json(parse_json(reply), "pdf_text")
            else:
                reply = llm.complete(SYSTEM, PROMPT, images=[_png(i) for i in images[:2]], schema=SCHEMA, vision=True)
                ex = from_model_json(parse_json(reply), "vision")
        except LLMError as exc:
            ex.warnings.append(f"AI could not read this document: {exc}")
    elif not qr:
        ex.warnings.append("AI is switched off and there is no e-invoice QR, so nothing could be read")

    if qr:
        ex = merge_qr(ex, qr)
    return ex
