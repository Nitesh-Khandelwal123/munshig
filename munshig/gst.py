"""GST helpers: GSTIN validation, state codes and tax-split checks.

All checks here are deterministic. They are the safety net under whatever the
AI reads off an invoice: a GSTIN with a bad check digit, or a total that does
not add up, marks the entry "needs review" instead of reaching the owner as a
clean suggestion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GSTIN_SHAPE = re.compile(r"^[0-9]{2}[A-Z0-9]{10}[0-9A-Z]Z[0-9A-Z]$")

STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "25": "Daman and Diu",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra",
    "28": "Andhra Pradesh (old)",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
    "97": "Other Territory",
    "99": "Centre Jurisdiction",
}


def gstin_check_char(first14: str) -> str:
    """Return the check character for the first 14 characters of a GSTIN."""
    total = 0
    for i, ch in enumerate(first14):
        value = _CHARSET.index(ch)
        product = value * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _CHARSET[(36 - total % 36) % 36]


def normalize_gstin(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    return cleaned or None


def is_valid_gstin(raw: str | None) -> bool:
    gstin = normalize_gstin(raw)
    if not gstin or len(gstin) != 15 or not _GSTIN_SHAPE.match(gstin):
        return False
    if gstin[:2] not in STATE_CODES:
        return False
    return gstin_check_char(gstin[:14]) == gstin[14]


def state_of(gstin: str | None) -> str | None:
    gstin = normalize_gstin(gstin)
    if not gstin or len(gstin) < 2:
        return None
    return STATE_CODES.get(gstin[:2])


def is_interstate(gstin_a: str | None, gstin_b: str | None) -> bool | None:
    """True when the two GSTINs are in different states, None if unknown."""
    a, b = normalize_gstin(gstin_a), normalize_gstin(gstin_b)
    if not a or not b or len(a) < 2 or len(b) < 2:
        return None
    return a[:2] != b[:2]


TOLERANCE = Decimal("1.00")


@dataclass
class TaxCheck:
    ok: bool
    reasons: list[str]
    round_off: Decimal


def check_tax_split(
    taxable: Decimal | None,
    cgst: Decimal | None,
    sgst: Decimal | None,
    igst: Decimal | None,
    cess: Decimal | None,
    total: Decimal | None,
    interstate: bool | None,
) -> TaxCheck:
    """Check that an invoice's numbers are internally consistent.

    Returns the round-off needed to make taxable + taxes equal the total
    (at most one rupee either way, the usual invoice rounding).
    """
    reasons: list[str] = []
    zero = Decimal("0")
    cgst, sgst, igst, cess = (v or zero for v in (cgst, sgst, igst, cess))

    if total is None:
        reasons.append("total amount not found")
        return TaxCheck(False, reasons, zero)
    if taxable is None:
        # A bill with no tax break-up (e.g. an unregistered seller) is fine:
        # the whole total goes to one ledger.
        if cgst or sgst or igst:
            reasons.append("tax amounts found but taxable value missing")
        return TaxCheck(not reasons, reasons, zero)

    if (cgst or sgst) and igst:
        reasons.append("both IGST and CGST/SGST present")
    if abs(cgst - sgst) > Decimal("0.02"):
        reasons.append(f"CGST {cgst} and SGST {sgst} differ")
    if interstate is True and (cgst or sgst):
        reasons.append("seller and buyer are in different states but CGST/SGST charged (expected IGST)")
    if interstate is False and igst:
        reasons.append("seller and buyer are in the same state but IGST charged (expected CGST/SGST)")

    computed = taxable + cgst + sgst + igst + cess
    diff = total - computed
    if abs(diff) > TOLERANCE:
        reasons.append(f"taxable {taxable} + tax {computed - taxable} = {computed}, but total is {total}")
        diff = zero
    return TaxCheck(not reasons, reasons, diff)
