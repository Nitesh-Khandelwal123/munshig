"""Find money events in plain chat text: amounts, payment words, UPI references.

Deliberately conservative. A number only counts as an amount when it carries
a currency marker (Rs, INR, the rupee sign, /-) or sits next to a payment word
("paid 5000", "5000 bhej diya"). Phone numbers, order quantities and dates are
not amounts. Anything caught here still goes to the owner for approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_NUM = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
_SUFFIX = r"\s*(k|K|thousand|hazaar|hazar|lakh|lac|L)?\b"
# "10 pcs", "5 kg": quantities, not money.
_NOT_UNIT = r"(?!\s*(?:pcs|pc|pieces?|nos?|units?|kg|kgs|g|gm|grams?|ltr|litres?|liters?|mtrs?|meters?|metres?|ft|feet|boxes?|bags?|dozen|sets?|qty|days?|hours?|hrs?|mins?|minutes?|%)\b)"

_CURRENCY_BEFORE = re.compile(r"(?:₹|rs\.?|inr|rupees?)\s*" + _NUM + _SUFFIX, re.IGNORECASE)
_CURRENCY_AFTER = re.compile(_NUM + _SUFFIX + r"\s*(?:/-|rs\.?\b|rupees?\b|inr\b)", re.IGNORECASE)
_KEYWORD_NUM = re.compile(
    r"(?:paid|received|recd|rcvd|sent|transferred|credited|debited|deposited|"
    r"bheja|bhej diya|jama|advance|payment|amount|amt|total|balance|due)"
    r"\s*(?:of|is|:|-|=)?\s*" + _NUM + _SUFFIX + _NOT_UNIT,
    re.IGNORECASE,
)
_NUM_KEYWORD = re.compile(
    _NUM + _SUFFIX + r"\s*(?:rs\.?\s*)?(?:paid|received|sent|transferred|credited|debited|bheja|bhej diya|diya|jama|kar diya)",
    re.IGNORECASE,
)

_RECEIVED = re.compile(r"\b(received|recd|rcvd|credited|got|mil gaya|mila|aa gaya|jama)\b", re.IGNORECASE)
_PAID = re.compile(r"\b(paid|sent|transferred|bheja|bhej diya|bhej|diya|de diya|debited|kar diya|pay kiya)\b", re.IGNORECASE)
_UTR = re.compile(r"\b(?:utr|upi ref(?:erence)?|ref(?:erence)? no\.?|txn id|transaction id)\s*[:#-]?\s*([A-Z0-9]{10,22})\b", re.IGNORECASE)
_BARE_UTR = re.compile(r"\b(\d{12})\b")

_MULTIPLIER = {
    "k": 1000, "thousand": 1000, "hazaar": 1000, "hazar": 1000,
    "lakh": 100000, "lac": 100000, "l": 100000,
}


@dataclass
class MoneyMention:
    amount: Decimal
    direction: str | None  # "received", "paid" or None when unclear
    utr: str | None
    snippet: str


def _to_decimal(number: str, suffix: str | None) -> Decimal | None:
    try:
        value = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return None
    if suffix:
        value *= _MULTIPLIER.get(suffix.lower(), 1)
    return value


def find_money(text: str) -> list[MoneyMention]:
    """Return every distinct amount mentioned in a message, largest first."""
    if not text:
        return []
    found: dict[Decimal, str] = {}
    for pattern in (_CURRENCY_BEFORE, _CURRENCY_AFTER, _KEYWORD_NUM, _NUM_KEYWORD):
        for m in pattern.finditer(text):
            value = _to_decimal(m.group(1), m.group(2))
            if value is None or value <= 0:
                continue
            # A 10+ digit number with no separators is a phone or account number.
            if "," not in m.group(1) and len(m.group(1).split(".")[0]) >= 10:
                continue
            found.setdefault(value, m.group(0).strip())

    if not found:
        return []

    received, paid = bool(_RECEIVED.search(text)), bool(_PAID.search(text))
    direction = "received" if received and not paid else "paid" if paid and not received else None
    utr_match = _UTR.search(text) or _BARE_UTR.search(text)
    utr = utr_match.group(1).upper() if utr_match else None

    return [
        MoneyMention(amount=v, direction=direction, utr=utr, snippet=s)
        for v, s in sorted(found.items(), key=lambda kv: kv[0], reverse=True)
    ]
