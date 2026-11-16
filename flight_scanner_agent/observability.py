"""Structured JSON logging and OpenTelemetry distributed tracing with PII scrubbing."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Iterator

from .pii_redactor import scrub_payload

try:
    import structlog
except ImportError:  # pragma: no cover
    structlog = None  # type: ignore[assignment]

try:
    import google.cloud.logging as cloud_logging
    from google.cloud.logging_v2.handlers import StructuredLogHandler
except ImportError:  # pragma: no cover
    cloud_logging = None  # type: ignore[assignment]
    StructuredLogHandler = None  # type: ignore[assignment]

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
except ImportError:  # pragma: no cover
    trace = None  # type: ignore[assignment]
    Resource = None  # type: ignore[assignment]
    TracerProvider = None  # type: ignore[assignment]
    BatchSpanProcessor = None  # type: ignore[assignment]

try:
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
except ImportError:  # pragma: no cover
    CloudTraceSpanExporter = None  # type: ignore[assignment]


class PiiScrubbingJsonFormatter(logging.Formatter):
    """Formats Python stdlib log records as single-line structured JSON with PII redacted."""

    def format(self, record: logging.LogRecord) -> str:
        base_payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra_fields = getattr(record, "structured_data", None)
        if isinstance(extra_fields, dict):
            base_payload.update(extra_fields)
        for key in (
            "event_phase",
            "invocation_id",
            "session_id",
            "user_id",
            "tool_name",
            "latency_ms",
            "status",
        ):
            if hasattr(record, key):
                base_payload[key] = getattr(record, key)
        return json.dumps(scrub_payload(base_payload), default=str)


_LOGGING_CONFIGURED = False
_TRACING_CONFIGURED = False


def configure_observability(service_name: str = "flight-deal-scanner-agent") -> logging.Logger:
    """Initializes structured JSON logging (Cloud Logging + structlog) and OpenTelemetry."""
    global _LOGGING_CONFIGURED, _TRACING_CONFIGURED

    logger = logging.getLogger(service_name)
    if not _LOGGING_CONFIGURED:
        logger.setLevel(logging.INFO)
        logger.propagate = False

        if (
            os.getenv("ENABLE_GCP_STRUCTURED_LOGGING", "false").lower() == "true"
            and StructuredLogHandler is not None
        ):
            handler: logging.Handler = StructuredLogHandler()
        else:
            handler = logging.StreamHandler()
            handler.setFormatter(PiiScrubbingJsonFormatter())

        logger.handlers = [handler]

        if structlog is not None:
            structlog.configure(
                processors=[
                    structlog.processors.TimeStamper(fmt="iso"),
                    structlog.processors.add_log_level,
                    lambda _, __, event_dict: scrub_payload(event_dict),
                    structlog.processors.JSONRenderer(),
                ],
                logger_factory=structlog.stdlib.LoggerFactory(),
                cache_logger_on_first_use=True,
            )
        _LOGGING_CONFIGURED = True

    if not _TRACING_CONFIGURED and trace is not None and TracerProvider is not None:
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        if (
            os.getenv("OTEL_EXPORT_TO_CLOUD", "false").lower() == "true"
            and CloudTraceSpanExporter is not None
            and BatchSpanProcessor is not None
        ):
            exporter = CloudTraceSpanExporter()
            provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _TRACING_CONFIGURED = True

    return logger


def get_structured_logger() -> logging.Logger:
    """Returns the singleton structured JSON logger."""
    return configure_observability()


@contextmanager
def trace_span(span_name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    """Creates an OpenTelemetry span around external provider calls and DB operations."""
    if trace is None:
        yield None
        return

    tracer = trace.get_tracer("flight_scanner_agent")
    with tracer.start_as_current_span(span_name) as span:
        if attributes and span is not None:
            scrubbed = scrub_payload(attributes)
            for key, value in scrubbed.items():
                span.set_attribute(key, str(value))
        yield span
