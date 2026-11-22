"""Parse the owner's replies to the digest.

    1P 2R 3A 4R 5X      tag entries (S P E A L R PY, or X to ignore)
    3 E Rent            tag with a specific ledger
    ALL                 accept every suggested tag (except "needs review")
    3?                  show entry 3 in full, with its document
    F1 Rs 420 each      send this answer to the customer behind follow-up F1
    D2                  send draft reply D2 as written
    D2 <new text>       send D2 with this text instead
    D2 NO               discard draft D2
    UNDO 2              delete entry 2's voucher from Tally (same day only)
    DIGEST              send the digest now
    HELP                list these commands

Entry numbers refer to the latest digest. F and D numbers are permanent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TAG_CODES = ("PY", "S", "P", "E", "A", "L", "R", "X")
_TAG_TOKEN = re.compile(r"(?<![A-Za-z0-9])(\d{1,3})\s*(PY|S|P|E|A|L|R|X)(?![A-Za-z0-9])", re.IGNORECASE)


@dataclass
class Tag:
    number: int
    tag: str
    ledger: str | None = None


@dataclass
class Parsed:
    tags: list[Tag] = field(default_factory=list)
    accept_all: bool = False
    details: list[int] = field(default_factory=list)
    followup_answers: list[tuple[int, str]] = field(default_factory=list)
    draft_send: list[tuple[int, str | None]] = field(default_factory=list)
    draft_discard: list[int] = field(default_factory=list)
    undo: list[int] = field(default_factory=list)
    digest_now: bool = False
    help: bool = False
    unknown: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any((self.tags, self.accept_all, self.details, self.followup_answers, self.draft_send,
                        self.draft_discard, self.undo, self.digest_now, self.help))


def _parse_tags(line: str) -> list[Tag] | None:
    matches = list(_TAG_TOKEN.finditer(line))
    if not matches or line[:matches[0].start()].strip(" ,;"):
        return None
    tags = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        ledger = line[m.end():end].strip(" ,;:-")
        tags.append(Tag(int(m.group(1)), m.group(2).upper(), ledger or None))
    return tags


def parse(text: str) -> Parsed:
    out = Parsed()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low in ("all", "approve all", "ok all"):
            out.accept_all = True
        elif low in ("help", "?", "commands"):
            out.help = True
        elif low in ("digest", "digest now"):
            out.digest_now = True
        elif m := re.fullmatch(r"undo\s+(\d{1,3})", low):
            out.undo.append(int(m.group(1)))
        elif m := re.fullmatch(r"(\d{1,3})\s*\?", low):
            out.details.append(int(m.group(1)))
        elif m := re.fullmatch(r"f(\d{1,4})\s*[:\-]?\s+(.+)", line, re.IGNORECASE | re.DOTALL):
            out.followup_answers.append((int(m.group(1)), m.group(2).strip()))
        elif m := re.fullmatch(r"d(\d{1,4})\s+(no|x|skip|discard)", low):
            out.draft_discard.append(int(m.group(1)))
        elif m := re.fullmatch(r"d(\d{1,4})", low):
            out.draft_send.append((int(m.group(1)), None))
        elif m := re.fullmatch(r"d(\d{1,4})\s*[:\-]?\s+(.+)", line, re.IGNORECASE | re.DOTALL):
            out.draft_send.append((int(m.group(1)), m.group(2).strip()))
        elif (tags := _parse_tags(line)) is not None:
            out.tags.extend(tags)
        else:
            out.unknown.append(line)
    return out


HELP_TEXT = """MunshiG commands
1P 2R 3X  tag entries: S sales, P purchase, E expense, A asset, L liability, R receipt, PY payment, X ignore
3 E Rent  tag with a ledger name
ALL  accept every suggested tag
3?  see entry 3 in full
F1 <answer>  reply to follow-up F1
D2  send draft D2 / D2 <text> send edited / D2 NO discard
UNDO 2  remove entry 2 from Tally (same day)
DIGEST  send the digest now"""
