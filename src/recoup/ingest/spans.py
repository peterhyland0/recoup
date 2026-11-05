"""Pages to numbered spans, and the text repairs every extractor shares.

A span is one non-empty text line. Its id, `p{page}-l{line}`, is what a citation points
at, so the grounding check can always go back to the exact words.
"""

from __future__ import annotations

import io
import re
from datetime import date

from pydantic import BaseModel, ConfigDict
from pypdf import PdfReader


class Span(BaseModel):
    model_config = ConfigDict(frozen=True)

    span_id: str
    page: int
    line: int
    text: str


def spans_from_pages(pages: list[str]) -> list[Span]:
    spans = []
    for page_no, text in enumerate(pages, start=1):
        number = 0
        for raw in text.split("\n"):
            if not raw.strip():
                continue
            number += 1
            spans.append(Span(span_id=f"p{page_no}-l{number}", page=page_no, line=number, text=raw.rstrip()))
    return spans


def spans_from_pdf(data: bytes) -> list[Span]:
    """Text-layer PDFs. An OCR engine would produce the same per-page text and plug in here."""
    reader = PdfReader(io.BytesIO(data))
    return spans_from_pages([page.extract_text() or "" for page in reader.pages])


# OCR repair. Two views of a line: digits repaired inside numeric tokens (for values), and a
# letter skeleton that folds look-alike characters together (for keyword matching).

_DIGIT_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8"})
_NUMERIC_TOKEN = re.compile(r"(?<![A-Za-z])[$\dOolISB][\dOolISB,.]*(?![A-Za-z])")


def repair_digits(text: str) -> str:
    """Fix look-alike letters inside tokens that are mostly digits, e.g. '$1O1.0O' -> '$101.00'."""

    def fix(match: re.Match[str]) -> str:
        token = match.group(0)
        digits = sum(ch.isdigit() for ch in token)
        letters = sum(ch in "OolISB" for ch in token)
        if digits == 0 or letters > digits:
            return token
        return token.translate(_DIGIT_LOOKALIKES)

    return re.sub(r" {2,}", " ", _NUMERIC_TOKEN.sub(fix, text))


_SKELETON = str.maketrans({"0": "o", "1": "l", "5": "s", "8": "b", "c": "e", "i": "l"})


def skeleton(text: str) -> str:
    """Lowercase, fold OCR look-alikes, collapse spaces. Apply to both sides of a comparison."""
    lowered = re.sub(r"\s+", " ", text.lower()).replace("rn", "m")
    return lowered.translate(_SKELETON)


def has(text_skeleton: str, *phrases: str) -> bool:
    return any(skeleton(p) in text_skeleton for p in phrases)


_MONTHS = {
    skeleton(name): number
    for number, name in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august", "september",
         "october", "november", "december"], start=1)
}  # fmt: skip
_DAY_LOOKALIKES = str.maketrans("lio", "110")


def find_dates(text: str) -> list[date]:
    """Every date written as 'March 1, 2026', '03/01/2026' or '2026-03-01', OCR-tolerant, in order."""
    lowered = repair_digits(text).lower()
    found: list[tuple[int, date | None]] = []
    for match in re.finditer(r"([a-z]{3,10})\s+([\dlio]{1,2}),\s*(\d{4})", lowered):
        month = _MONTHS.get(skeleton(match.group(1)))
        if month:
            day = int(match.group(2).translate(_DAY_LOOKALIKES))
            found.append((match.start(), _safe_date(int(match.group(3)), month, day)))
    for match in re.finditer(r"\b(\d{2})/(\d{2})/(\d{4})\b", lowered):
        found.append(
            (match.start(), _safe_date(int(match.group(3)), int(match.group(1)), int(match.group(2))))
        )
    for match in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", lowered):
        found.append(
            (match.start(), _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        )
    return [d for _, d in sorted(found, key=lambda pair: pair[0]) if d is not None]


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None
