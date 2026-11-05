"""Parse and write X12 835 (005010X221A1) health care claim payment advice.

Covers the segments a payment audit needs: BPR, TRN, DTM, N1/REF (payer and payee),
CLP, CAS, NM1*QC, SVC, AMT*B6, REF*6R and LQ*HE. PLB (provider-level adjustments) is
read past, not modelled: it moves money between claims, not within a service line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from recoup.core.models import (
    Adjustment,
    AdjustmentGroup,
    ClaimStatus,
    RemitClaim,
    RemitLine,
    Remittance,
)


class X12ParseError(ValueError):
    def __init__(self, message: str, segment_index: int | None = None, segment: str = ""):
        where = f" at segment {segment_index} ({segment[:40]!r})" if segment_index is not None else ""
        super().__init__(message + where)
        self.segment_index = segment_index


@dataclass(frozen=True)
class Delimiters:
    element: str = "*"
    component: str = ":"
    segment: str = "~"
    repetition: str = "^"


def detect_delimiters(text: str) -> Delimiters:
    """ISA is fixed width: element separator at 3, component at 104, terminator at 105."""
    if not text.startswith("ISA") or len(text) < 106:
        raise X12ParseError("input does not start with a 106-character ISA segment")
    element = text[3]
    isa = text[:106].split(element)
    if len(isa) != 17:
        raise X12ParseError(f"ISA has {len(isa) - 1} elements, expected 16")
    return Delimiters(element=element, component=text[104], segment=text[105], repetition=isa[11])


def split_segments(text: str) -> tuple[Delimiters, list[list[str]]]:
    text = text.lstrip("\ufeff \t\r\n")
    delims = detect_delimiters(text)
    segments = []
    for raw in text.split(delims.segment):
        raw = raw.strip("\r\n ")
        if raw:
            segments.append(raw.split(delims.element))
    return delims, segments


def _money(value: str, index: int, raw: list[str]) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        amount = None
    if amount is None or not amount.is_finite():
        raise X12ParseError(f"bad amount {value!r}", index, "*".join(raw))
    return amount


def _date(value: str, index: int, raw: list[str]) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise X12ParseError(f"bad date {value!r}", index, "*".join(raw)) from None


def _get(seg: list[str], position: int) -> str:
    return seg[position] if position < len(seg) else ""


def _adjustments(seg: list[str], index: int) -> list[Adjustment]:
    try:
        group = AdjustmentGroup(_get(seg, 1))
    except ValueError:
        raise X12ParseError(f"unknown CAS group {_get(seg, 1)!r}", index, "*".join(seg)) from None
    out = []
    for start in range(2, len(seg), 3):
        reason = _get(seg, start)
        if not reason:
            continue
        qty = _get(seg, start + 2)
        out.append(
            Adjustment(
                group=group,
                reason=reason,
                amount=_money(_get(seg, start + 1), index, seg),
                quantity=_money(qty, index, seg) if qty else None,
            )
        )
    return out


@dataclass
class _LineBuilder:
    code: str
    modifiers: tuple[str, ...]
    billed: Decimal
    paid: Decimal
    units: Decimal
    service_date: date | None = None
    adjustments: list[Adjustment] = field(default_factory=list)
    allowed: Decimal | None = None
    line_id: str = ""
    remarks: list[str] = field(default_factory=list)


@dataclass
class _ClaimBuilder:
    claim_id: str
    status: ClaimStatus
    billed: Decimal
    paid: Decimal
    patient_responsibility: Decimal
    payer_claim_id: str
    patient_name: str = ""
    statement_date: date | None = None
    adjustments: list[Adjustment] = field(default_factory=list)
    lines: list[_LineBuilder] = field(default_factory=list)

    occurrence: int = 1
    """How many claims with this CLP01 came before, plus one (reversal then correction)."""

    def build(self) -> RemitClaim:
        lines = []
        stem = self.claim_id if self.occurrence == 1 else f"{self.claim_id}#{self.occurrence}"
        for number, line in enumerate(self.lines, start=1):
            service_date = line.service_date or self.statement_date
            if service_date is None:
                raise X12ParseError(f"claim {self.claim_id} line {number} has no service date")
            lines.append(
                RemitLine(
                    line_id=line.line_id or f"{stem}-{number}",
                    code=line.code,
                    modifiers=line.modifiers,
                    units=line.units,
                    billed=line.billed,
                    paid=line.paid,
                    service_date=service_date,
                    adjustments=tuple(line.adjustments),
                    allowed=line.allowed,
                    remarks=tuple(line.remarks),
                )
            )
        return RemitClaim(
            claim_id=self.claim_id,
            status=self.status,
            billed=self.billed,
            paid=self.paid,
            patient_responsibility=self.patient_responsibility,
            payer_claim_id=self.payer_claim_id,
            patient_name=self.patient_name,
            lines=tuple(lines),
            adjustments=tuple(self.adjustments),
        )


def parse_835(text: str) -> list[Remittance]:
    """Parse every 835 transaction set (ST..SE) in an interchange."""
    delims, segments = split_segments(text)
    remits: list[Remittance] = []
    header: dict[str, object] = {}
    claims: list[_ClaimBuilder] = []
    claim: _ClaimBuilder | None = None
    line: _LineBuilder | None = None
    entity = ""
    st_index: int | None = None

    for index, seg in enumerate(segments):
        tag = seg[0]
        if tag == "ST":
            if _get(seg, 1) != "835":
                raise X12ParseError("transaction set is not an 835", index, "*".join(seg))
            if st_index is not None:
                raise X12ParseError("ST before the previous set's SE", index, "*".join(seg))
            st_index = index
            header, claims, claim, line, entity = {}, [], None, None, ""
        elif tag == "BPR":
            header["total_paid"] = _money(_get(seg, 2), index, seg)
            if _get(seg, 16):
                header["effective_date"] = _date(_get(seg, 16), index, seg)
        elif tag == "TRN":
            header["check_number"] = _get(seg, 2)
        elif tag == "DTM":
            qualifier, value = _get(seg, 1), _get(seg, 2)
            if qualifier == "405":
                header["payment_date"] = _date(value, index, seg)
            elif (qualifier == "472" and line is not None) or (
                qualifier == "150" and line is not None and line.service_date is None
            ):
                line.service_date = _date(value, index, seg)
            elif qualifier in ("232", "472") and claim is not None:
                claim.statement_date = _date(value, index, seg)
        elif tag == "N1":
            entity = _get(seg, 1)
            if entity == "PR":
                header["payer_name"] = _get(seg, 2)
                header.setdefault("payer_id", _get(seg, 4))
            elif entity == "PE":
                header["payee_name"] = _get(seg, 2)
                if _get(seg, 3) == "XX":
                    header["payee_npi"] = _get(seg, 4)
        elif tag == "REF":
            qualifier = _get(seg, 1)
            if qualifier == "2U" and entity == "PR" and claim is None:
                header["payer_id"] = _get(seg, 2)
            elif qualifier == "HPI" and entity == "PE" and claim is None:
                header.setdefault("payee_npi", _get(seg, 2))
            elif qualifier == "6R" and line is not None:
                line.line_id = _get(seg, 2)
        elif tag == "CLP":
            try:
                status = ClaimStatus(_get(seg, 2))
            except ValueError:
                raise X12ParseError(f"unknown claim status {_get(seg, 2)!r}", index, "*".join(seg)) from None
            claim = _ClaimBuilder(
                claim_id=_get(seg, 1),
                status=status,
                billed=_money(_get(seg, 3), index, seg),
                paid=_money(_get(seg, 4), index, seg),
                patient_responsibility=_money(_get(seg, 5) or "0", index, seg),
                payer_claim_id=_get(seg, 7),
                occurrence=1 + sum(c.claim_id == _get(seg, 1) for c in claims),
            )
            claims.append(claim)
            line = None
        elif tag == "NM1" and _get(seg, 1) == "QC" and claim is not None:
            last, first = _get(seg, 3), _get(seg, 4)
            claim.patient_name = f"{last}, {first}" if first else last
        elif tag == "SVC":
            if claim is None:
                raise X12ParseError("SVC before any CLP", index, "*".join(seg))
            parts = _get(seg, 1).split(delims.component)
            if len(parts) < 2:
                raise X12ParseError("SVC01 has no procedure code", index, "*".join(seg))
            units = _get(seg, 5)
            line = _LineBuilder(
                code=parts[1],
                modifiers=tuple(m for m in parts[2:6] if m),
                billed=_money(_get(seg, 2), index, seg),
                paid=_money(_get(seg, 3), index, seg),
                units=_money(units, index, seg) if units else Decimal(1),
            )
            claim.lines.append(line)
        elif tag == "CAS":
            target = line.adjustments if line is not None else claim.adjustments if claim else None
            if target is None:
                raise X12ParseError("CAS outside a claim", index, "*".join(seg))
            target.extend(_adjustments(seg, index))
        elif tag == "AMT" and _get(seg, 1) == "B6" and line is not None:
            line.allowed = _money(_get(seg, 2), index, seg)
        elif tag == "LQ" and _get(seg, 1) == "HE" and line is not None:
            line.remarks.append(_get(seg, 2))
        elif tag == "SE":
            if st_index is None:
                raise X12ParseError("SE without ST", index, "*".join(seg))
            count = index - st_index + 1
            if _get(seg, 1) != str(count):
                raise X12ParseError(
                    f"SE01 says {_get(seg, 1)} segments, counted {count}", index, "*".join(seg)
                )
            st_index = None
            header.setdefault("payment_date", header.get("effective_date"))
            if header["payment_date"] is None:
                del header["payment_date"]
            missing = [
                k
                for k in ("total_paid", "check_number", "payment_date", "payer_name", "payee_npi")
                if k not in header
            ]
            if missing:
                raise X12ParseError(f"835 is missing {', '.join(missing)}", index, "*".join(seg))
            remits.append(
                Remittance(
                    payer_id=str(header.get("payer_id", "")),
                    payer_name=str(header["payer_name"]),
                    payee_npi=str(header["payee_npi"]),
                    payee_name=str(header.get("payee_name", "")),
                    check_number=str(header["check_number"]),
                    payment_date=header["payment_date"],
                    total_paid=header["total_paid"],
                    claims=tuple(c.build() for c in claims),
                )
            )
            claim, line = None, None
    if st_index is not None:
        raise X12ParseError("transaction set has no SE", st_index)
    if not remits:
        raise X12ParseError("no 835 transaction sets found")
    return remits


def _amount(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _isa(sender: str, receiver: str, when: datetime, control: int) -> str:
    fields = [
        "ISA", "00", " " * 10, "00", " " * 10,
        "ZZ", sender.ljust(15)[:15], "ZZ", receiver.ljust(15)[:15],
        when.strftime("%y%m%d"), when.strftime("%H%M"), "^", "00501",
        f"{control:09d}", "0", "P", ":",
    ]  # fmt: skip
    return "*".join(fields)


_RESERVED = "*~:^"


def _check_text(remit: Remittance) -> None:
    fields = [remit.payer_id, remit.payer_name, remit.payee_npi, remit.payee_name, remit.check_number]
    for claim in remit.claims:
        fields += [claim.claim_id, claim.payer_claim_id, claim.patient_name]
        for line in claim.lines:
            fields += [line.line_id, line.code, *line.modifiers, *line.remarks]
    for value in fields:
        if any(ch in value for ch in _RESERVED):
            raise ValueError(f"{value!r} contains an X12 delimiter")


def write_835(remit: Remittance, control: int = 1) -> str:
    """Write one remittance as a complete interchange. Lines are newline-separated for diffs."""
    if not 0 <= control <= 999_999_999:
        raise ValueError("ISA control number must fit in 9 digits")
    _check_text(remit)
    when = datetime.combine(remit.payment_date, datetime.min.time()).replace(hour=12)
    body: list[str] = [
        "ST*835*0001",
        f"BPR*{'I' if remit.total_paid else 'H'}*{_amount(remit.total_paid)}*C*{'ACH*CCP' if remit.total_paid else 'NON*'}"
        f"***********{remit.payment_date:%Y%m%d}",
        f"TRN*1*{remit.check_number}*1{remit.payer_id}",
        f"DTM*405*{remit.payment_date:%Y%m%d}",
        f"N1*PR*{remit.payer_name}",
        f"REF*2U*{remit.payer_id}",
        f"N1*PE*{remit.payee_name}*XX*{remit.payee_npi}",
        "LX*1",
    ]
    for claim in remit.claims:
        body.append(
            f"CLP*{claim.claim_id}*{claim.status.value}*{_amount(claim.billed)}*{_amount(claim.paid)}"
            f"*{_amount(claim.patient_responsibility)}*12*{claim.payer_claim_id}"
        )
        body += _cas_segments(claim.adjustments)
        if claim.patient_name:
            last, _, first = claim.patient_name.partition(", ")
            body.append(f"NM1*QC*1*{last}*{first}" if first else f"NM1*QC*1*{last}")
        for line in claim.lines:
            composite = ":".join(("HC", line.code, *line.modifiers))
            body.append(f"SVC*{composite}*{_amount(line.billed)}*{_amount(line.paid)}**{line.units}")
            body.append(f"DTM*472*{line.service_date:%Y%m%d}")
            body += _cas_segments(line.adjustments)
            body.append(f"REF*6R*{line.line_id}")
            if line.allowed is not None:
                body.append(f"AMT*B6*{_amount(line.allowed)}")
            body += [f"LQ*HE*{code}" for code in line.remarks]
    body.append(f"SE*{len(body) + 1}*0001")
    segments = [
        _isa(remit.payer_id, remit.payee_npi, when, control),
        f"GS*HP*{remit.payer_id}*{remit.payee_npi}*{when:%Y%m%d}*{when:%H%M}*{control}*X*005010X221A1",
        *body,
        f"GE*1*{control}",
        f"IEA*1*{control:09d}",
    ]
    return "~\n".join(segments) + "~\n"


def _cas_segments(adjustments: tuple[Adjustment, ...]) -> list[str]:
    """One CAS per group, up to six reason triples each, in first-seen order."""
    by_group: dict[AdjustmentGroup, list[Adjustment]] = {}
    for adj in adjustments:
        by_group.setdefault(adj.group, []).append(adj)
    out = []
    for group, items in by_group.items():
        for start in range(0, len(items), 6):
            triples = []
            for adj in items[start : start + 6]:
                qty = _amount(adj.quantity) if adj.quantity is not None else ""
                triples.append(f"{adj.reason}*{_amount(adj.amount)}*{qty}")
            out.append((f"CAS*{group.value}*" + "*".join(triples)).rstrip("*"))
    return out
