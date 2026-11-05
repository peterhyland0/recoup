"""One resilient way to call someone else's API.

What is retried, and why it is safe:
  429                    always: the server says it did not process the request
  5xx, network errors    only if repeating the request cannot do the work twice: an
                         idempotent method (GET, HEAD, PUT, DELETE, OPTIONS) or a request
                         carrying an Idempotency-Key, which is sent unchanged on every attempt
  other 4xx              never

Backoff is exponential with full jitter, capped. Retry-After is honoured when it is a
sane number of seconds or an HTTP date; anything else (negative, NaN, garbage) falls back
to jitter instead of crashing the caller.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from recoup.obs import tracer

RETRYABLE = {429, 500, 502, 503, 504}
IDEMPOTENT = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}


class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def retry_after_seconds(
    header: str | None, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> float | None:
    if not header:
        return None
    try:
        seconds = float(header)
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - now()).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


class ResilientClient:
    def __init__(self, http: httpx.Client, *, attempts: int = 5, base_delay: float = 0.25, max_delay: float = 8.0,
                 sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None):  # fmt: skip
        self.http = http
        self.attempts = attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.sleep = sleep
        self.rng = rng or random.Random()

    def _delay(self, attempt: int, response: httpx.Response | None) -> float:
        hinted = retry_after_seconds(response.headers.get("Retry-After")) if response is not None else None
        if hinted is not None:
            return min(hinted, self.max_delay)
        return self.rng.uniform(0, min(self.max_delay, self.base_delay * 2**attempt))

    def request(
        self, method: str, url: str, *, idempotency_key: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", None) or {})
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        repeatable = method.upper() in IDEMPOTENT or idempotency_key is not None
        last = ""
        with tracer().start_as_current_span(f"http {method} {url}") as span:
            for attempt in range(self.attempts):
                response: httpx.Response | None = None
                try:
                    response = self.http.request(method, url, headers=headers, **kwargs)
                except httpx.TransportError as error:
                    last = f"{type(error).__name__}: {error}"
                    if not repeatable:
                        raise UpstreamError(
                            f"{method} {url} failed and is not safe to repeat ({last})"
                        ) from error
                else:
                    status = response.status_code
                    retry = status == 429 or (status in RETRYABLE and repeatable)
                    if not retry:
                        span.set_attribute("http.attempts", attempt + 1)
                        span.set_attribute("http.status_code", status)
                        if response.is_error:
                            raise UpstreamError(f"{method} {url} -> {status}: {response.text[:200]}", status)
                        return response
                    last = str(status)
                if attempt + 1 < self.attempts:
                    self.sleep(self._delay(attempt, response))
            span.set_attribute("http.attempts", self.attempts)
        raise UpstreamError(f"{method} {url} failed after {self.attempts} attempts (last: {last})")
