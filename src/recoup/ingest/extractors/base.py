"""The contract every extractor fulfils: values as strings, each with the spans that state it."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from recoup.ingest.spans import Span


class ExtractedValue(BaseModel):
    key: str
    """A term key, e.g. "pct_medicare", "fee_schedule.99213", "amendment.A1.modifier.50",
    or a header key: "contract_id", "provider_npi", "effective_date", "end_date", "payer_name"."""
    value: str
    """Canonical string: decimals as "1.15", days as "90", booleans as "true", dates ISO."""
    span_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    note: str = ""


class Extraction(BaseModel):
    extractor: str
    values: list[ExtractedValue] = Field(default_factory=list)
    injection_span_ids: list[str] = Field(default_factory=list)
    """Spans that try to instruct an automated reader. Never a source for any value."""
    warnings: list[str] = Field(default_factory=list)

    def get(self, key: str) -> ExtractedValue | None:
        for value in self.values:
            if value.key == key:
                return value
        return None


class Extractor(Protocol):
    name: str

    def extract(self, spans: list[Span]) -> Extraction: ...
