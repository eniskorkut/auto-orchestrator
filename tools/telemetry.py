#!/usr/bin/env python3
"""Optional, no-op-safe OpenTelemetry adapter (stdlib only at import time).

Telemetry is **opt-in**. Importing this module performs no I/O and requires no
third-party package. If ``opentelemetry-api`` and ``opentelemetry-sdk`` are not
installed, every public function is a safe no-op; a missing collector can never
fail the dashboard, installer or Supervisor. Call :func:`configure` explicitly
before using :func:`span` or :func:`record_event`.

Privacy defaults:

* Only whitelisted attribute names are exported.
* Attribute keys containing ``prompt``, ``secret``, ``token``, ``password``,
  ``content``, ``code``, ``command`` or ``output`` are dropped.
* Attribute values are limited to scalars and short strings; containers are
  dropped. Exception messages and span bodies are never recorded as attributes.
* No endpoint is read from the environment. Passing an endpoint is an explicit
  opt-in to OTLP/HTTP export; a collector is otherwise not required.

The core product keeps its stdlib HTTP server. FastAPI is intentionally not a
dependency and OpenTelemetry must not replace the Supervisor's SQLite evidence.
"""
from __future__ import annotations

import contextlib
import math
import re
import threading
from urllib.parse import urlsplit

SERVICE_NAME = "auto-orchestrator"
INSTRUMENTATION_NAME = "auto_orchestrator"
MAX_ATTRIBUTE_LENGTH = 200
MAX_EVENT_NAME_LENGTH = 120

# Conservative allowlist. Identity fields are filled explicitly by span() and
# record_event(); arbitrary caller attributes must also appear here.
SAFE_ATTRIBUTE_KEYS = frozenset({
    "project_id",
    "run_id",
    "task_id",
    "attempt_id",
    "agent_id",
    "platform",
    "status",
    "reason",
    "event",
    "event_type",
    "phase",
    "kind",
    "mode",
    "role",
    "theme",
    "source",
    "confidence",
    "skill",
    "count",
    "duration_ms",
    "exit",
    "retry",
    "http_method",
    "server_port",
    "route",
    "sequence_id",
    "schema_version",
})

# Any key containing one of these fragments is dropped before it can reach an
# exporter, even if a future allowlist entry accidentally matches.
FORBIDDEN_ATTRIBUTE_PARTS = (
    "prompt",
    "secret",
    "token",
    "password",
    "content",
    "code",
    "command",
    "output",
    "credential",
    "authorization",
)

_NAME_SAFE = re.compile(r"[^A-Za-z0-9_.:/-]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

_LOCK = threading.RLock()
_TRACER = None
_PROVIDER = None
_CONFIG = None
_CONFIGURED = False


class NoopSpan:
    """Minimal span used when OpenTelemetry is absent or not configured."""

    def set_attribute(self, key, value) -> None:
        return None

    def add_event(self, name, attributes=None) -> None:
        return None

    def set_status(self, status) -> None:
        return None

    def end(self) -> None:
        return None

    def is_recording(self) -> bool:
        return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def otel_available() -> bool:
    """Return whether the OpenTelemetry API and SDK can both be imported.

    This performs local imports only. It never contacts a collector and never
    raises for a missing or broken installation.
    """
    try:
        from opentelemetry import trace  # noqa: F401
        from opentelemetry.sdk.trace import TracerProvider  # noqa: F401
    except Exception:
        return False
    return True


def _normalize_service_name(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 100:
        return None
    if any(not character.isprintable() for character in value):
        return None
    return value


def _valid_endpoint(value) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    return True


def _safe_name(value, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    value = _CONTROL.sub(" ", value).strip()
    value = _NAME_SAFE.sub("_", value)[:MAX_EVENT_NAME_LENGTH]
    return value or fallback


def _clean_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        value = _CONTROL.sub(" ", value).strip()
        return value[:MAX_ATTRIBUTE_LENGTH] or None
    return None


def safe_attributes(attributes) -> dict:
    """Return only whitelisted, non-sensitive scalar attributes.

    Keys are normalized to lowercase snake case. Values are limited to bool,
    int, finite float and short strings. Nested mappings/lists and unknown
    keys are silently dropped rather than exported.
    """
    if not isinstance(attributes, dict):
        return {}
    result = {}
    for raw_key, raw_value in attributes.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip().lower().replace("-", "_").replace(" ", "_")
        if not key or key not in SAFE_ATTRIBUTE_KEYS:
            continue
        if any(part in key for part in FORBIDDEN_ATTRIBUTE_PARTS):
            continue
        value = _clean_value(raw_value)
        if value is None:
            continue
        result[key] = value
    return result


def _identity_attributes(
    project_id=None,
    run_id=None,
    task_id=None,
    attempt_id=None,
    agent_id=None,
    attributes=None,
) -> dict:
    values = {
        "project_id": project_id,
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "agent_id": agent_id,
    }
    if attributes:
        values.update(attributes)
    return safe_attributes(values)


def _build_tracer(service_name: str, endpoint: str | None):
    """Create the SDK tracer. Kept separate so tests can stub the SDK boundary."""
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if endpoint is not None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    return provider.get_tracer(INSTRUMENTATION_NAME), provider


def configure(service_name: str = SERVICE_NAME, endpoint: str | None = None) -> bool:
    """Opt in to OpenTelemetry.

    Returns ``True`` only when a tracer was configured. Returns ``False`` when
    OpenTelemetry is absent, the configuration is invalid, or SDK setup fails.
    Repeated calls with the same arguments are idempotent and do not replace
    the tracer. A conflicting second configuration is refused so multiple
    callers cannot silently redirect telemetry.
    """
    global _TRACER, _PROVIDER, _CONFIG, _CONFIGURED
    service = _normalize_service_name(service_name)
    if service is None:
        return False
    if not _valid_endpoint(endpoint):
        return False
    normalized_endpoint = endpoint or None
    key = (service, normalized_endpoint)
    with _LOCK:
        if _CONFIGURED:
            return key == _CONFIG
        if not otel_available():
            return False
        try:
            setup = _build_tracer(service, normalized_endpoint)
        except Exception:
            return False
        if not isinstance(setup, tuple) or len(setup) != 2 or setup[0] is None:
            return False
        _TRACER = setup[0]
        _PROVIDER = setup[1]
        _CONFIG = key
        _CONFIGURED = True
        return True


@contextlib.contextmanager
def _activate(otel_span):
    """Attach a span as current when the OTel API is importable."""
    manager = None
    try:
        from opentelemetry import trace

        manager = trace.use_span(
            otel_span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        )
    except Exception:
        manager = None
    if manager is None:
        yield
    else:
        with manager:
            yield


@contextlib.contextmanager
def span(
    name,
    project_id=None,
    run_id=None,
    task_id=None,
    attempt_id=None,
    agent_id=None,
    **attributes,
):
    """No-op-safe span context manager.

    The body always executes. Attribute filtering happens before any SDK call.
    A body exception is re-raised unchanged; telemetry never swallows product
    errors and never records exception messages.
    """
    tracer = _TRACER
    if tracer is None:
        yield NoopSpan()
        return
    try:
        otel_span = tracer.start_span(_safe_name(name, "auto_orchestrator.span"))
    except Exception:
        otel_span = None
    if otel_span is None:
        yield NoopSpan()
        return
    try:
        for key, value in _identity_attributes(
            project_id, run_id, task_id, attempt_id, agent_id, attributes
        ).items():
            try:
                otel_span.set_attribute(key, value)
            except Exception:
                pass
        try:
            with _activate(otel_span):
                yield otel_span
        except Exception:
            try:
                from opentelemetry import trace

                otel_span.set_status(trace.Status(trace.StatusCode.ERROR))
            except Exception:
                pass
            raise
    finally:
        try:
            otel_span.end()
        except Exception:
            pass


def record_event(
    name,
    project_id=None,
    run_id=None,
    task_id=None,
    attempt_id=None,
    agent_id=None,
    **attributes,
) -> bool:
    """Add a filtered event to the current recording span, if any.

    Returns ``True`` only when an event was actually added. Without OTel, a
    configured tracer, or an active recording span this is a no-op returning
    ``False``. It never creates a new span so it cannot fabricate activity.
    """
    if _TRACER is None:
        return False
    try:
        from opentelemetry import trace

        current = trace.get_current_span()
        if current is None or not current.is_recording():
            return False
        payload = _identity_attributes(
            project_id, run_id, task_id, attempt_id, agent_id, attributes
        )
        current.add_event(_safe_name(name, "auto_orchestrator.event"), payload)
        return True
    except Exception:
        return False
