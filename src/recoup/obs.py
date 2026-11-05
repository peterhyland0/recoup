"""Tracing. Every graph node is an OpenTelemetry span; export is off unless an endpoint is set.

Set OTEL_EXPORTER_OTLP_ENDPOINT to send spans to any OTLP collector (Grafana Tempo,
Jaeger, or LangSmith's OTLP endpoint). Without it, spans are created and dropped, so the
code path is the same in tests and in production.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from typing import Any, TypeVar

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

F = TypeVar("F", bound=Callable[..., Any])
_configured = False


def configure(exporter: SpanExporter | None = None) -> None:
    """Install a tracer provider once. Tests pass an in-memory exporter."""
    global _configured
    if _configured and exporter is None:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "recoup"}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def tracer() -> trace.Tracer:
    return trace.get_tracer("recoup")


def traced(name: str) -> Callable[[F], F]:
    """Wrap a function in a span named `name`, recording exceptions."""

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with tracer().start_as_current_span(name) as span:
                span.set_attribute("recoup.llm.cost_usd", 0.0)
                return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate
