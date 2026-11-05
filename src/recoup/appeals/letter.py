"""The appeal letter. A template filled from the audit trace and the contract's own words.

Every number comes from the rate engine and every quoted clause comes from the cited
spans, so the person approving the letter is checking facts, not prose a model wrote.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from jinja2 import Environment, StrictUndefined

from recoup.audit.findings import Finding, Kind

TEMPLATE = """\
{{ today }}

To: {{ payer_name }}, Provider Appeals
From: {{ provider_name }} (NPI {{ npi }})
Re: Request for reconsideration of claim {{ claim_id }}, payer claim {{ payer_claim_id }},
    payment {{ check_number }} dated {{ payment_date }}

We ask you to reconsider the payment on the service lines below. Each amount is what our
agreement {{ contract_id }} sets for the service; the clauses relied on are quoted.
{% for item in items %}
{{ loop.index }}. Line {{ item.f.line_id }}, CPT {{ item.f.code }}{{ item.mods }}, date of service {{ item.f.service_date }}
   {% if item.f.kind == "denial_invalid" -%}
   Denied as untimely, but {{ item.reason }}.
   Contract allowed amount: ${{ "%.2f"|format(item.f.expected_allowed) }}. Requested: ${{ "%.2f"|format(item.f.recoverable) }}.
   {%- else -%}
   Contract allowed amount: ${{ "%.2f"|format(item.f.expected_allowed) }}. Amount allowed on the remittance: ${{ "%.2f"|format(item.f.payer_allowed) }}.
   Requested: ${{ "%.2f"|format(item.f.recoverable) }}.
   {%- endif %}
   How the contract amount is reached: {{ item.basis }}.
   {%- for quote in item.quotes %}
   Contract, page {{ quote.page }}, line {{ quote.line }}: "{{ quote.text }}"
   {%- endfor %}
{% endfor %}
Total requested: ${{ "%.2f"|format(total) }}

This request is made within the {{ window }}-day appeal window (deadline {{ deadline }}).
Please reprocess the lines above at the contracted amounts and send a corrected remittance.

{{ provider_name }}
"""

_env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)


def draft_letter(findings: list[Finding], quotes: dict[str, str], *, today: date, payer_name: str,
                 provider_name: str, npi: str, window: int | None, deadline: date | None) -> str:  # fmt: skip
    items = []
    for finding in findings:
        span_ids = sorted({sid for e in finding.evidence for sid in e.span_ids}, key=_span_order)
        reason = (
            finding.explanation.split("but ", 1)[1].split(". ", 1)[0] if "but " in finding.explanation else ""
        )
        items.append({
            "f": finding,
            "mods": f"-{'-'.join(finding.modifiers)}" if finding.modifiers else "",
            "basis": "; ".join(s.detail for s in finding.trace if s.rule != "round") or "contract rate",
            "quotes": [_quote(sid, quotes[sid]) for sid in span_ids if sid in quotes],
            "reason": reason if finding.kind is Kind.DENIAL_INVALID else "",
        })  # fmt: skip
    first = findings[0]
    return _env.from_string(TEMPLATE).render(
        today=today.isoformat(), payer_name=payer_name, provider_name=provider_name, npi=npi,
        claim_id=first.claim_id, payer_claim_id=first.payer_claim_id, check_number=first.check_number,
        payment_date=first.payment_date.isoformat(), contract_id=first.contract_id, items=items,
        total=sum((f.recoverable for f in findings), Decimal(0)), window=window or "contractual",
        deadline=deadline.isoformat() if deadline else "not stated",
    )  # fmt: skip


def _span_order(span_id: str) -> tuple[int, int]:
    page, line = span_id.removeprefix("p").split("-l")
    return int(page), int(line)


def _quote(span_id: str, text: str) -> dict[str, object]:
    page, line = _span_order(span_id)
    return {"page": page, "line": line, "text": " ".join(text.split()).replace('"', "'")}
