"""Prompt-injection screening that runs on every document, whichever extractor read it.

Phrase matching will always be evadable, so this is one layer, not the defence. The
defence is structural: any document with a suspected injection is NEEDS_REVIEW, and no
finding priced from a NEEDS_REVIEW contract is sent anywhere without a person. The list
leans towards false alarms, because a false alarm costs one review and a miss costs money.
"""

from __future__ import annotations

import re

from recoup.ingest.spans import Span, skeleton

PHRASES = (
    "ignore previous", "ignore all previous", "ignore any other", "ignore the above", "disregard",
    "ai review", "ai system", "ai model", "ai tool", "language model", "llm", "chatbot", "processing model",
    "automated system", "automated reviewer", "automated reader", "automated tool", "automated review",
    "assistant:", "update your", "your extraction", "your instructions", "system note", "system prompt",
    "note to automated", "if you are an", "attention claims", "attention: ai", "override", "jailbreak",
)  # fmt: skip


_PATTERN = re.compile("|".join(rf"(?<!\w){re.escape(skeleton(p))}s?(?!\w)" for p in PHRASES))
"""Whole words only, on the OCR skeleton: 'llm' must not fire inside 'limit' (skeleton 'llmlt')."""


def is_injection(text: str) -> bool:
    return _PATTERN.search(skeleton(text)) is not None


def injection_spans(spans: list[Span]) -> list[str]:
    """Span ids that address an automated reader. Also catches a phrase split across two lines."""
    flagged: list[str] = []
    for index, span in enumerate(spans):
        if is_injection(span.text):
            flagged.append(span.span_id)
            continue
        following = spans[index + 1] if index + 1 < len(spans) else None
        if (
            following is not None
            and following.page == span.page
            and not is_injection(following.text)
            and is_injection(f"{span.text} {following.text}")
        ):
            flagged += [span.span_id, following.span_id]
    return list(dict.fromkeys(flagged))
