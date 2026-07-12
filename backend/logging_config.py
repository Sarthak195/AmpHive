"""
Structured logging (TD#28).

Installs a JSON-lines formatter on the root logger so every log line from
every `amphive.*` logger (amphive.api, amphive.mqtt, amphive.db, ...) is one
parseable JSON object, carrying a request correlation id so an HTTP request
can be traced through to the MQTT command / session it triggered.

Usage (backend/main.py):
    from backend.logging_config import configure_logging
    configure_logging()   # replaces logging.basicConfig(level=logging.INFO)

The HTTP middleware in main.py calls set_correlation_id() per request (from
the incoming X-Request-ID header, or a generated one) before invoking the
route handler. Because it's a contextvars.ContextVar, the value is visible to
everything awaited within that request's asyncio task — including a
synchronous MQTT publish made directly from a route handler — without having
to thread an id through every function signature. Code running outside that
task (paho-mqtt's own callback thread, background services like the session
reaper) sees the default "-" unless it sets its own id.

Deliberately dependency-free: a ~30-line custom logging.Formatter covers the
one JSON shape we need, so this doesn't pull in python-json-logger or
structlog for it.
"""
import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

# Default correlation id for log records emitted with none set (background
# tasks, startup/shutdown code, the paho-mqtt callback thread, etc.).
UNSET_CORRELATION_ID = "-"

# Attributes present on every stdlib LogRecord (logging/__init__.py's
# `makeRecord`), used to tell a caller's `extra={...}` fields apart from the
# record's own bookkeeping when flattening a record to JSON. Includes
# `message`/`asctime`, which stdlib Formatter.format() lazily adds to the
# record in place — relevant if some other handler's formatter (e.g. a test
# framework's log capture) touches the same record before ours does.
_STANDARD_LOG_RECORD_FIELDS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "correlation_id",
    "message", "asctime",
}

_correlation_id_var: ContextVar[str] = ContextVar(
    "amphive_correlation_id", default=UNSET_CORRELATION_ID
)


def set_correlation_id(correlation_id: Optional[str]) -> None:
    """Bind a correlation id to the current context (asyncio task / thread).
    Falls back to UNSET_CORRELATION_ID for None/empty so a blank header can't
    disable correlation for the rest of the request."""
    _correlation_id_var.set(correlation_id or UNSET_CORRELATION_ID)


def get_correlation_id() -> str:
    """Read the correlation id bound to the current context ("-" if unset)."""
    return _correlation_id_var.get()


class CorrelationIdFilter(logging.Filter):
    """Stamps `record.correlation_id` from the current context on every
    record that passes through a handler this filter is attached to."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        return True


def _json_safe(value):
    """Best-effort JSON coercion for an extra field's value. Most extras are
    already primitives (ids, strings, floats); anything json can't serialize
    natively (Decimal, datetime, enums, ...) is stringified rather than
    dropped or left to blow up json.dumps."""
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts (ISO-8601 UTC), level, logger, msg,
    correlation_id, plus any structured `extra={...}` fields the call site
    attached (e.g. gateway_id, plug_id, session_id)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", UNSET_CORRELATION_ID),
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_FIELDS or key in payload or key.startswith("_"):
                continue
            payload[key] = _json_safe(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str)


class PlainFormatter(logging.Formatter):
    """Human-readable single-line format for local dev (LOG_FORMAT=plain)."""

    def __init__(self):
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)s [%(correlation_id)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def configure_logging() -> None:
    """
    Install a structured formatter on the root logger, replacing the old
    `logging.basicConfig(level=logging.INFO)`. Every `amphive.*` module
    logger propagates here unchanged.

    Env:
      LOG_LEVEL  — default INFO (DEBUG/INFO/WARNING/ERROR/CRITICAL)
      LOG_FORMAT — "json" (default, one object per line) or "plain" (for
                   local console readability)

    Idempotent: safe to call more than once (e.g. test setup) — clears any
    handlers a previous call (or basicConfig) installed on the root logger
    first, so lines aren't duplicated or double-formatted.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = os.getenv("LOG_FORMAT", "json").lower()

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CorrelationIdFilter())
    handler.setFormatter(PlainFormatter() if fmt == "plain" else JsonFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
