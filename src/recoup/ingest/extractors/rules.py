"""The default extractor: deterministic rules, no model, no network, no cost.

How it works: wrapped lines are joined back into clauses (keeping every span id), each
clause is matched against keyword families on an OCR-folded skeleton, and the value is
read from a digit-repaired copy of the text. Every value cites the clause's spans.

The keyword families were written from development wording (phrasing pack A) only. They
are deliberately not extended to the holdout wording, so the holdout score shows how far
rules generalise, which is the case for swapping in a model-based extractor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from recoup.ingest.extractors.base import ExtractedValue, Extraction
from recoup.ingest.guards import is_injection
from recoup.ingest.spans import Span, find_dates, has, repair_digits, skeleton

PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|p\w{2,4}nt\b)")
MONEY = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})")
DAYS = re.compile(r"(?:\((\d{1,3})\)|(\d{1,3}))\s*(?:calendar\s+)?d\w{1,2}s\b")
CODE = re.compile(r"\b(\d{5})\b")
YEAR = re.compile(r"\b(20\d{2})\b")
FEE_ROW = re.compile(r"^\s*(?:cpt\s+)?(\d{5})\b.*?(\d[\d,]*\.\d{2})\s*$", re.IGNORECASE)
MODIFIER = re.compile(r"\bmodi\w{2,3}r\s+(\w{2})\b", re.IGNORECASE)
MODIFIER_WORD = re.compile(r"\bmodi\w{2,3}r\b", re.IGNORECASE)
HEADING = re.compile(r"^(?i:section\s+\d+\.|amendment\s+no\.|exhibit\s+[a-z]\s+-)")
LABEL = re.compile(r"^[A-Z][A-Za-z .]{1,30}(?::|\bNo\.)\s*\S")
LABEL_MAX = 50
MODIFIER_ROW = re.compile(r"^(?:modi\w{2,3}r\s+\w{2}\b|cpt\s+\d{5}\b)", re.IGNORECASE)
"""A line opening with 'Modifier 50' or 'CPT 99213' starts its own clause."""


def _is_heading(text: str) -> bool:
    """Section/amendment/exhibit openers, or a mostly upper-case line (OCR may lower a few letters)."""
    if HEADING.match(text) or re.match(r"^(seetlon|amendment no\.)\s*\w{1,3}\.", skeleton(text)):
        return True
    letters = [ch for ch in text if ch.isalpha()]
    upper = sum(ch.isupper() for ch in letters)
    return len(letters) >= 8 and upper / len(letters) >= 0.85 and not re.search(r"\d{3,}", text)


def _is_title(text: str) -> bool:
    """A short line in title case with no closing punctuation: a heading, never part of a sentence."""
    words = re.findall(r"[A-Za-z]{3,}", text)
    return (
        len(text) <= 40
        and not text.endswith((".", ",", ";", ":", ")"))
        and bool(words)
        and all(w[0].isupper() for w in words)
    )


CLEAN, REPAIRED = 0.95, 0.80


@dataclass
class Clause:
    spans: list[Span]
    amendment: str | None
    in_exhibit: bool

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.spans)

    @property
    def fixed(self) -> str:
        return repair_digits(self.text)

    @property
    def skel(self) -> str:
        return skeleton(self.text)

    @property
    def ids(self) -> list[str]:
        return [s.span_id for s in self.spans]

    @property
    def confidence(self) -> float:
        return CLEAN if self.fixed == re.sub(r" {2,}", " ", self.text) else REPAIRED


def _is_standalone(text: str) -> bool:
    stripped = text.strip()
    return bool(
        _is_heading(stripped)
        or _is_title(stripped)
        or (LABEL.match(stripped) and len(stripped) <= LABEL_MAX)
        or MODIFIER_ROW.match(stripped)
        or FEE_ROW.match(repair_digits(stripped))
    )


def clauses(spans: list[Span]) -> list[Clause]:
    out: list[Clause] = []
    amendment: str | None = None
    in_exhibit = False
    for span in spans:
        text = span.text.strip()
        if _is_heading(text):
            skel = skeleton(text)
            number = re.search(r"amendment\s+no\.\s*(\d+)", repair_digits(text), re.IGNORECASE)
            if number:
                amendment, in_exhibit = f"A{number.group(1)}", False
            elif has(skel, "exhibit", "fee schedule"):
                amendment, in_exhibit = None, True  # an exhibit is base contract, even after an amendment
            elif skel.startswith(skeleton("section")):
                in_exhibit = False
            out.append(Clause([span], amendment, in_exhibit))
            continue
        previous = out[-1] if out else None
        joinable = (
            previous is not None
            and previous.spans[-1].page == span.page
            and not _is_standalone(text)
            and not _is_standalone(previous.text)
            and not previous.text.rstrip().endswith((".", ":"))
        )
        if previous is not None and joinable:
            previous.spans.append(span)
        else:
            out.append(Clause([span], amendment, in_exhibit))
    return out


HEADER_LINES = 14


def _in_header(clause: Clause) -> bool:
    """Parties, ids and dates are only read from the top of page 1, where agreements state them."""
    first = clause.spans[0]
    return first.page == 1 and first.line <= HEADER_LINES


def _pct(value: str) -> str:
    return format((Decimal(value) / 100).normalize(), "f")


def _days(clause: Clause) -> str | None:
    match = DAYS.search(clause.fixed.lower())
    return (match.group(1) or match.group(2)) if match else None


def _date(text: str) -> date | None:
    found = find_dates(text)
    return found[0] if found else None


def _segment(text: str, start: int, stop: re.Pattern[str]) -> str:
    """Text from `start` up to the next match of `stop`, so a value binds to its own code."""
    following = stop.search(text, start + 1)
    return text[start : following.start() if following else len(text)]


FROM_TO = re.compile(
    r"from\s+\d{1,3}(?:\.\d+)?\s*(?:%|p\w{2,4}nt)\s+to\s+(\d{1,3}(?:\.\d+)?)\s*(?:%|p\w{2,4}nt)"
)
MPR_PHRASES = (
    "multiple procedure",
    "procedure after the first",
    "secondary procedure",
    "additional procedure",
)


class RuleExtractor:
    name = "rules"

    def extract(self, spans: list[Span]) -> Extraction:
        result = Extraction(extractor=self.name)
        amendments: set[str] = set()
        for clause in clauses(spans):
            if clause.amendment:
                amendments.add(clause.amendment)
            if is_injection(clause.text):
                result.injection_span_ids += clause.ids
                continue
            for key, value in self._values(clause, result):
                self._add(result, key, value, clause)
        for amendment in sorted(amendments):
            if not any(v.key.startswith(f"amendment.{amendment}.") for v in result.values):
                result.warnings.append(f"amendment {amendment}: nothing could be read from it")
        return result

    @staticmethod
    def _add(result: Extraction, key: str, value: str, clause: Clause) -> None:
        existing = result.get(key)
        if existing is None:
            result.values.append(ExtractedValue(key=key, value=value, span_ids=clause.ids,
                                                confidence=clause.confidence))  # fmt: skip
        elif existing.value != value:
            existing.confidence = min(existing.confidence, 0.5)
            result.warnings.append(f"{key}: conflicting values {existing.value!r} and {value!r}")

    def _values(self, clause: Clause, result: Extraction) -> list[tuple[str, str]]:
        s, fixed, low = clause.skel, clause.fixed, clause.fixed.lower()
        prefix = f"amendment.{clause.amendment}." if clause.amendment else ""
        percents = [p for p in PERCENT.findall(low) if Decimal(p) != 100]
        codes = CODE.findall(fixed)
        out: list[tuple[str, str]] = []

        if clause.in_exhibit and not clause.amendment:
            row = FEE_ROW.match(fixed)
            if row and len(codes) > 1:
                result.warnings.append(f"{clause.ids[0]}: several codes on one fee row, not read")
                return []
            return (
                [(f"fee_schedule.{row.group(1)}", f"{Decimal(row.group(2).replace(',', '')):.2f}")]
                if row
                else []
            )

        if clause.amendment and has(s, "effective", "takes effect"):
            found = _date(clause.text)
            if found:
                out.append((f"{prefix}effective_date", found.isoformat()))

        if clause.amendment and has(s, "exhibit a") and has(s, "cpt"):
            match = re.search(r"cpt\s+(\d{5})\b.*?\$\s*([\d,]+\.\d{2})", low)
            if match and len(codes) == 1:
                return [*out, (f"{prefix}fee_schedule.{match.group(1)}", match.group(2).replace(",", ""))]

        if has(s, "flat rate", "carved out"):
            money = MONEY.findall(fixed)
            if len(codes) == 1 and len(money) == 1:
                base = f"{prefix}carve_out.{codes[0]}"
                out.append((base, money[0].replace(",", "")))
                if has(s, "not subject to", "exempt from"):
                    out.append((f"{base}.mpr_exempt", "true"))
                elif has(s, "rules apply", "subject to multiple"):
                    out.append((f"{base}.mpr_exempt", "false"))
            return out

        modifiers = list(MODIFIER.finditer(fixed))
        if modifiers and not has(s, "medicare", *MPR_PHRASES):
            for match in modifiers:
                segment = _segment(low, match.start(), MODIFIER_WORD)
                stated = [p for p in PERCENT.findall(segment) if Decimal(p) != 100]
                if stated:
                    out.append((f"{prefix}modifier.{match.group(1)}", _pct(stated[0])))
            return out

        if has(s, *MPR_PHRASES):
            if has(s, "not be reduced"):
                return [*out, (f"{prefix}mpr_secondary_pct", "none")]
            return [*out, (f"{prefix}mpr_secondary_pct", _pct(percents[-1]))] if percents else out

        if has(s, "medicare") and percents:
            moved = FROM_TO.search(low)
            out.append((f"{prefix}pct_medicare", _pct(moved.group(1) if moved else percents[0])))
            year = YEAR.search(fixed)
            if year:
                out.append((f"{prefix}medicare_year", year.group(1)))
            return out

        if has(s, "billed charge"):
            if has(s, "not limited by"):
                return [*out, (f"{prefix}lesser_of_billed", "false")]
            if has(s, "lesser of", "lower of", "exceed"):
                return [*out, (f"{prefix}lesser_of_billed", "true")]

        if has(s, "date of service") and (
            has(s, "timely filing") or (has(s, "claims") and has(s, "submitted", "file"))
        ):
            days = _days(clause)
            return [*out, (f"{prefix}timely_filing_days", days)] if days else out

        if has(s, "appeal", "reconsideration"):
            days = _days(clause)
            return [*out, (f"{prefix}appeal_window_days", days)] if days else out

        if clause.amendment or not _in_header(clause):
            return out
        return self._header(clause)

    @staticmethod
    def _header(clause: Clause) -> list[tuple[str, str]]:
        s, fixed = clause.skel, clause.fixed
        contract_id = re.search(r"\bno\.\s*([a-z]{2}-\d{4}-\d{3})\b", fixed, re.IGNORECASE)
        if contract_id and has(s, "agreement"):
            return [("contract_id", contract_id.group(1).upper())]
        npi = re.search(r"\b(\d{10})\b", fixed)
        if has(s, "npi") and npi:
            return [("provider_npi", npi.group(1))]
        payer = re.search(r'between\s+(.+?)\s*\("payer"\)', clause.text, re.IGNORECASE)
        if payer:
            return [("payer_name", payer.group(1).strip())]
        found = _date(clause.text)
        if found and has(s, "effective"):
            return [("effective_date", found.isoformat())]
        if found and has(s, "through", "in force"):
            return [("end_date", found.isoformat())]
        return []
