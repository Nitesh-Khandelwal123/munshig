"""Generate the dummy documents used by tests and `munshig demo`.

All parties, GSTINs and numbers are made up (GSTINs have valid check digits
but belong to no one). Re-run after changing a sample:

    .venv/bin/python scripts/make_samples.py
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import qrcode
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "munshig" / "demo" / "samples"

BUSINESS = {"name": "Demo Traders", "gstin": "27AABCD1234E1Z8", "addr": "Shop 4, Market Yard, Pune 411037"}
SHARMA = {"name": "Sharma Steel Suppliers", "gstin": "27AAECS5678F1Z0", "addr": "Bhosari MIDC, Pune 411026"}
KUMAR = {"name": "Kumar Electricals", "gstin": "29AAHFK4321L1Z4", "addr": "SP Road, Bengaluru 560002"}
COMFORT = {"name": "Comfort Furniture House", "gstin": "27AAKFC2468M1ZZ", "addr": "Camp, Pune 411001"}
XYZ = {"name": "XYZ Packaging", "gstin": "27AAFFX1357N1ZN", "addr": "Hadapsar, Pune 411028"}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def einvoice_qr_text(seller, buyer, doc_no, doc_date, total, items, hsn, irn) -> str:
    data = {"SellerGstin": seller, "BuyerGstin": buyer, "DocNo": doc_no, "DocTyp": "INV", "DocDt": doc_date,
            "TotInvVal": total, "ItemCnt": items, "MainHsnCode": hsn, "Irn": irn, "IrnDt": "2026-11-02 11:15:00"}
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"data": json.dumps(data), "iss": "NIC"}).encode())
    return f"{header}.{payload}.{b64url(b'DEMO-ONLY-NOT-SIGNED')}"


def invoice_pdf(path: Path, title: str, seller: dict, buyer: dict, inv_no: str, inv_date: str,
                rows: list[tuple[str, str, int, float]], taxes: list[tuple[str, float]], total: float,
                qr_text: str | None = None) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 7, seller["name"], new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, seller["addr"], new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"GSTIN: {seller['gstin']}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.cell(0, 5, f"Invoice No: {inv_no}        Date: {inv_date}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 5, "Bill To:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, buyer["name"], new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, buyer["addr"], new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"GSTIN: {buyer['gstin']}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 10)
    for head, w in (("Description", 80), ("HSN", 25), ("Qty", 20), ("Amount (Rs.)", 45)):
        pdf.cell(w, 7, head, border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "", 10)
    taxable = 0.0
    for desc, hsn, qty, amount in rows:
        taxable += amount
        pdf.cell(80, 7, desc, border=1)
        pdf.cell(25, 7, hsn, border=1)
        pdf.cell(20, 7, str(qty), border=1)
        pdf.cell(45, 7, f"{amount:,.2f}", border=1, align="R")
        pdf.ln()
    pdf.ln(2)
    for label, value in [("Taxable Value", taxable), *taxes, ("Grand Total", total)]:
        pdf.set_font("Helvetica", "B" if label == "Grand Total" else "", 10)
        pdf.cell(125, 6, label, align="R")
        pdf.cell(45, 6, f"Rs. {value:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")
    if qr_text:
        img = qrcode.make(qr_text, box_size=4, border=2).get_image()
        qr_path = path.with_suffix(".qr.png")
        img.save(qr_path)
        pdf.image(str(qr_path), x=150, y=12, w=45)
        qr_path.unlink()
        pdf.set_y(-30)
        pdf.set_font("Helvetica", "", 8)
        pdf.cell(0, 5, "IRN: " + qr_text[-24:], align="R")
    pdf.output(str(path))


def font(size: int):
    return ImageFont.load_default(size=size)


def chair_bill_png(path: Path) -> None:
    img = Image.new("RGB", (900, 1100), "white")
    d = ImageDraw.Draw(img)
    d.text((300, 40), "TAX INVOICE", fill="black", font=font(40))
    d.text((60, 120), COMFORT["name"], fill="black", font=font(32))
    d.text((60, 165), COMFORT["addr"], fill="black", font=font(22))
    d.text((60, 200), f"GSTIN: {COMFORT['gstin']}", fill="black", font=font(22))
    d.text((60, 260), "Invoice No: CF-889", fill="black", font=font(24))
    d.text((520, 260), "Date: 31/10/2026", fill="black", font=font(24))
    d.text((60, 320), f"To: {BUSINESS['name']}, {BUSINESS['addr']}", fill="black", font=font(20))
    d.text((60, 350), f"GSTIN: {BUSINESS['gstin']}", fill="black", font=font(20))
    d.line((60, 400, 840, 400), fill="black", width=2)
    d.text((60, 420), "Ergonomic office chair x 2", fill="black", font=font(24))
    d.text((640, 420), "5,500.00", fill="black", font=font(24))
    d.line((60, 470, 840, 470), fill="black", width=2)
    y = 500
    for label, value in (("Taxable Value", "5,500.00"), ("CGST @ 9%", "495.00"), ("SGST @ 9%", "495.00"),
                         ("Grand Total", "6,490.00")):
        d.text((380, y), label, fill="black", font=font(24))
        d.text((640, y), value, fill="black", font=font(24))
        y += 45
    d.text((60, 1000), "Thank you for your business", fill="gray", font=font(20))
    img = img.rotate(1.2, expand=False, fillcolor="white")  # a slightly crooked phone photo
    img.save(path)


def upi_screenshot_png(path: Path) -> None:
    img = Image.new("RGB", (720, 1280), (245, 247, 250))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 720, 120), fill=(32, 98, 200))
    d.text((40, 40), "Payment successful", fill="white", font=font(40))
    d.ellipse((310, 180, 410, 280), fill=(46, 160, 90))
    d.text((337, 205), "OK", fill="white", font=font(40))
    d.text((230, 320), "Rs. 25,000", fill="black", font=font(56))
    d.text((40, 450), f"Paid to: {BUSINESS['name']}", fill="black", font=font(28))
    d.text((40, 500), "UPI ID: demotraders@okbank", fill=(90, 90, 90), font=font(24))
    d.text((40, 570), f"From: {KUMAR['name']}", fill="black", font=font(28))
    d.text((40, 640), "UPI transaction ID: 631245789012", fill="black", font=font(26))
    d.text((40, 690), "15 Nov 2026, 4:12 pm", fill=(90, 90, 90), font=font(26))
    d.text((40, 760), "Note: against bill DT/26-27/118", fill=(90, 90, 90), font=font(24))
    img.save(path)


def quotation_pdf(path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "QUOTATION", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 11)
    for line in (XYZ["name"], XYZ["addr"], f"GSTIN: {XYZ['gstin']}", "", "Quotation No: Q-2231   Date: 12/11/2026",
                 f"To: {BUSINESS['name']}", "", "Corrugated boxes 18x12x12, 2000 pcs ........ Rs. 40,000.00",
                 "GST extra as applicable. Valid for 15 days. This is not a tax invoice."):
        pdf.cell(0, 7, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    qr = einvoice_qr_text(SHARMA["gstin"], BUSINESS["gstin"], "SS/2026/0412", "02/11/2026", 11800, 1, "7306",
                          "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90")
    invoice_pdf(OUT / "sharma_steel_invoice.pdf", "TAX INVOICE", SHARMA, BUSINESS, "SS/2026/0412", "02/11/2026",
                [("MS pipe 1 inch, 6 m", "7306", 20, 10000.00)], [("CGST @ 9%", 900.0), ("SGST @ 9%", 900.0)],
                11800.00, qr_text=qr)
    invoice_pdf(OUT / "sales_invoice_kumar.pdf", "TAX INVOICE", BUSINESS, KUMAR, "DT/26-27/118", "14/11/2026",
                [("GI sheet 0.5 mm", "7210", 40, 21186.44)], [("IGST @ 18%", 3813.56)], 25000.00)
    chair_bill_png(OUT / "office_chair_bill.png")
    upi_screenshot_png(OUT / "upi_payment_kumar.png")
    quotation_pdf(OUT / "quotation_xyz.pdf")

    truth = {
        "sharma_steel_invoice.pdf": {
            "doc_type": "invoice", "seller_name": SHARMA["name"], "seller_gstin": SHARMA["gstin"],
            "buyer_name": BUSINESS["name"], "buyer_gstin": BUSINESS["gstin"], "invoice_no": "SS/2026/0412",
            "invoice_date": "2026-11-02", "taxable": 10000, "cgst": 900, "sgst": 900, "igst": None,
            "cess": None, "round_off": None, "total": 11800},
        "sales_invoice_kumar.pdf": {
            "doc_type": "invoice", "seller_name": BUSINESS["name"], "seller_gstin": BUSINESS["gstin"],
            "buyer_name": KUMAR["name"], "buyer_gstin": KUMAR["gstin"], "invoice_no": "DT/26-27/118",
            "invoice_date": "2026-11-14", "taxable": 21186.44, "cgst": None, "sgst": None, "igst": 3813.56,
            "cess": None, "round_off": None, "total": 25000},
        "office_chair_bill.png": {
            "doc_type": "invoice", "seller_name": COMFORT["name"], "seller_gstin": COMFORT["gstin"],
            "buyer_name": BUSINESS["name"], "buyer_gstin": BUSINESS["gstin"], "invoice_no": "CF-889",
            "invoice_date": "2026-10-31", "taxable": 5500, "cgst": 495, "sgst": 495, "igst": None,
            "cess": None, "round_off": None, "total": 6490},
        "upi_payment_kumar.png": {
            "doc_type": "payment", "payer_name": KUMAR["name"], "payee_name": BUSINESS["name"],
            "utr": "631245789012", "payment_status": "success", "invoice_date": "2026-11-15", "total": 25000,
            "against_bill": "DT/26-27/118"},
        "quotation_xyz.pdf": {
            "doc_type": "not_a_bill", "not_a_bill_kind": "quotation", "seller_name": XYZ["name"],
            "seller_gstin": XYZ["gstin"], "invoice_no": "Q-2231", "total": 40000},
    }
    (OUT / "truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")
    print(f"Wrote samples to {OUT}")


if __name__ == "__main__":
    main()
