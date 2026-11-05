"""Checks that sit between any extractor and the rate engine.

Grounding: every value must be visibly present in the spans it cites, and no cited span
may be one flagged as an injection. A value that fails is dropped, never repaired.

Consistency: values must be plausible on their own and against each other. A value that
fails stays, but its confidence drops and the contract goes to review.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field

from recoup.core.fees import FeeTable
from recoup.ingest.extractors.base import ExtractedValue, Extraction
from recoup.ingest.spans import Span, find_dates, repair_digits, skeleton

DAY_KEYS = ("timely_filing_days", "appeal_window_days")
SYMBOLIC = {"true", "false", "none"}


class Evidence(BaseModel):
    """A value as the rest of the system sees it: what, where, how sure, and what is wrong with it."""

    key: str
    value: str
    span_ids: list[str]
    confidence: float
    grounded: bool
    issues: list[str] = Field(default_factory=list)


def _leaf(key: str) -> str:
    """'amendment.A1.fee_schedule.99213' -> 'fee_schedule.99213'."""
    parts = key.split(".")
    return ".".join(parts[2:]) if parts[0] == "amendment" else key


_PERCENT = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*(?:%|p\w{2,4}nt\b)")
_CODE = re.compile(r"(?<!\d)\d{5}(?!\d)")
_MODIFIER = re.compile(r"\bmodi\w{2,3}r\s+(\w{2})\b|\bmod\.\s*(\w{2})\b", re.IGNORECASE)
SYMBOL_WORDS = {
    "lesser_of_billed": ("billed", "charge"),
    "mpr_secondary_pct": ("multiple", "procedure", "reduc"),
    "mpr_exempt": ("multiple", "procedure", "reduc"),
}


def _bound_segment(text: str, start: int, stop: re.Pattern[str]) -> str:
    following = stop.search(text, start + 1)
    return text[start : following.start() if following else len(text)]


def _check(value: ExtractedValue, text: str) -> tuple[str | None, str | None]:
    """(hard issue, ambiguity). A hard issue drops the value; an ambiguity sends it to review.

    Values bind to their own context: a fee amount must sit after its code and before the
    next code, a modifier's percentage after that modifier, so a number elsewhere in the
    cited text cannot vouch for it.
    """
    leaf, raw = _leaf(value.key), value.value
    fixed = repair_digits(text)
    low = fixed.lower()
    family = leaf.split(".")[0]
    if raw in SYMBOLIC or leaf.endswith(".mpr_exempt"):
        words = SYMBOL_WORDS.get("mpr_exempt" if leaf.endswith(".mpr_exempt") else family)
        if words is None:
            return f"{raw!r} is not a valid value for {leaf}", None
        if not any(skeleton(w) in skeleton(low) for w in words):
            return f"the cited text says nothing about {leaf}", None
        if leaf.endswith(".mpr_exempt") and leaf.split(".")[1] not in fixed:
            return f"code {leaf.split('.')[1]} is not in the cited text", None
        return None, None
    try:
        if family in ("pct_medicare", "mpr_secondary_pct", "modifier"):
            number = format((Decimal(raw) * 100).normalize(), "f")
            scope = low
            if family == "modifier":
                code = leaf.split(".")[1].lower()
                starts = [m.start() for m in _MODIFIER.finditer(low) if code in (m.group(1), m.group(2))]
                if not starts:
                    return f"modifier {code} is not in the cited text", None
                scope = " ".join(_bound_segment(low, start, _MODIFIER) for start in starts)
            found = set(_PERCENT.findall(scope))
            if number not in found:
                return f"{number}% is not stated for {leaf} in the cited text", None
            others = {p for p in found if Decimal(p) not in (Decimal(number), Decimal(100))}
            if others and not re.search(rf"\bto\s+{re.escape(number)}\s*(?:%|p)", scope):
                return (
                    None,
                    f"the cited text also states {', '.join(sorted(others))}%; which applies is ambiguous",
                )
            return None, None
        if family in ("fee_schedule", "carve_out"):
            code = leaf.split(".")[1]
            starts = [m.start() for m in _CODE.finditer(fixed) if m.group(0) == code]
            if not starts:
                return f"code {code} is not in the cited text", None
            amounts: set[Decimal] = set()
            for start in starts:
                segment = _bound_segment(fixed, start, _CODE)
                amounts |= {Decimal(m.replace(",", "")) for m in re.findall(r"\d[\d,]*\.\d{2}", segment)}
            if Decimal(raw) not in amounts:
                return f"{raw} is not stated for {code} in the cited text", None
            return None, None
    except InvalidOperation:
        return f"{raw!r} is not a number", None
    if leaf in DAY_KEYS or leaf == "medicare_year":
        hit = re.search(rf"(?<!\d){re.escape(raw)}(?!\d)", fixed)
        return (None, None) if hit else (f"{raw} is not in the cited text", None)
    if leaf in ("effective_date", "end_date"):
        try:
            wanted = date.fromisoformat(raw)
        except ValueError:
            return f"{raw!r} is not a date", None
        return (None, None) if wanted in find_dates(text) else (f"{raw} is not in the cited text", None)
    return (None, None) if skeleton(raw) in skeleton(fixed) else (f"{raw!r} is not in the cited text", None)


def ground(extraction: Extraction, spans: list[Span]) -> list[Evidence]:
    by_id = {span.span_id: span for span in spans}
    poisoned = set(extraction.injection_span_ids)
    out = []
    for value in extraction.values:
        issues = []
        missing = [sid for sid in value.span_ids if sid not in by_id]
        if missing:
            issues.append(f"cites unknown spans {missing}")
        if poisoned.intersection(value.span_ids):
            issues.append("cites a span flagged as an injection")
        ambiguity = None
        if not issues:
            text = " ".join(by_id[sid].text for sid in value.span_ids)
            issue, ambiguity = _check(value, text)
            if issue:
                issues.append(issue)
        grounded = not issues
        confidence = value.confidence if grounded else 0.0
        if ambiguity:
            issues.append(ambiguity)
            confidence = min(confidence, 0.5)
        out.append(Evidence(key=value.key, value=value.value, span_ids=value.span_ids, confidence=confidence,
                            grounded=grounded, issues=issues))  # fmt: skip
    # A key stated twice with different values is not a fact; neither copy is used.
    values_by_key: dict[str, set[str]] = {}
    for item in out:
        if item.grounded:
            values_by_key.setdefault(item.key, set()).add(item.value)
    seen: set[str] = set()
    for item in out:
        if not item.grounded:
            continue
        if len(values_by_key[item.key]) > 1:
            item.grounded, item.confidence = False, 0.0
            item.issues.append(f"conflicting values {sorted(values_by_key[item.key])}")
        elif item.key in seen:
            item.grounded = False
            item.issues.append("duplicate of an earlier value")
        seen.add(item.key)
    return out


RANGES: dict[str, tuple[Decimal, Decimal]] = {
    "pct_medicare": (Decimal("0.5"), Decimal("3")),
    "mpr_secondary_pct": (Decimal("0.1"), Decimal("1")),
    "modifier": (Decimal("0.05"), Decimal("3")),
    "timely_filing_days": (Decimal(30), Decimal(730)),
    "appeal_window_days": (Decimal(15), Decimal(730)),
}


def check_consistency(evidence: list[Evidence], fees: FeeTable) -> None:
    """Annotates evidence in place. Ranges are wide on purpose: they catch misreads, not hard bargains."""
    by_key = {e.key: e for e in evidence if e.grounded}
    start = by_key.get("effective_date")
    end = by_key.get("end_date")
    year_item = by_key.get("medicare_year")
    year = int(year_item.value) if year_item else date.fromisoformat(start.value).year if start else None
    for item in evidence:
        if not item.grounded or item.value in SYMBOLIC:
            continue
        leaf = _leaf(item.key)
        family = leaf.split(".")[0]
        problem = None
        if family in RANGES:
            low, high = RANGES[family]
            if not low <= Decimal(item.value) <= high:
                problem = f"{item.value} is outside the plausible range {low} to {high}"
        elif family in ("fee_schedule", "carve_out") and not leaf.endswith(".mpr_exempt"):
            medicare = fees.fee(leaf.split(".")[1], year) if year else None
            if medicare and not medicare * Decimal("0.3") <= Decimal(item.value) <= medicare * 5:
                problem = f"${item.value} is implausible against the Medicare fee ${medicare}"
        elif leaf == "effective_date" and item.key.startswith("amendment.") and start:
            when = date.fromisoformat(item.value)
            if when < date.fromisoformat(start.value) or (end and when > date.fromisoformat(end.value)):
                problem = f"amendment date {item.value} is outside the contract term"
        if problem:
            item.issues.append(problem)
            item.confidence = min(item.confidence, 0.4)
    if start and end and date.fromisoformat(end.value) <= date.fromisoformat(start.value):
        end.issues.append("end date is not after the effective date")
        end.confidence = min(end.confidence, 0.4)
