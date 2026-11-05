"""Write a rendered contract to PDF, byte-for-byte reproducible for a given document."""

from __future__ import annotations

from datetime import UTC, datetime

from fpdf import FPDF

from recoup.synth.render import Document, LineKind

FIXED_DATE = datetime(2026, 1, 1, tzinfo=UTC)
LINE_HEIGHT = 4.6


def to_pdf(doc: Document, title: str) -> bytes:
    pdf = FPDF(format="letter", unit="mm")
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_title(title)
    pdf.set_producer("recoup synth")
    pdf.set_margins(18, 16, 18)
    pdf.set_auto_page_break(False)
    for page in doc.pages:
        pdf.add_page()
        for line in page:
            bold = line.kind is LineKind.HEADING
            pdf.set_font("Courier", style="B" if bold else "", size=8.6)
            text = line.text.encode("latin-1", "replace").decode("latin-1")
            pdf.cell(0, LINE_HEIGHT if text else LINE_HEIGHT / 2, text, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
