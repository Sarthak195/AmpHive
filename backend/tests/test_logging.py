"""
Tests for structured logging + correlation ids (TD#28):

- backend/logging_config.py: the JSON formatter emits one parseable JSON
  object per record, carrying the correlation id (plus whatever structured
  `extra={...}` fields a call site attached), and the correlation-id
  ContextVar plumbing (get/set + the logging.Filter that stamps it onto every
  record) behaves correctly.
- backend/main.py: the HTTP correlation-id middleware reads (or generates)
  X-Request-ID, binds it for the duration of the request, and echoes it back
  on the response — so a request can be traced through whatever it logs,
  including a synchronous MQTT publish made from a route handler.

The middleware tests import backend.main, which calls configure_logging() at
module import time (replacing the old logging.basicConfig(INFO)) — the same
wiring the real app runs at startup, exercised here for real.

contextvars aren't isolated between test functions by pytest (they live on
whatever ambient Context the test happens to run in), so every test that
touches the correlation id runs inside a fresh contextvars.Context() to stay
independent of test order/other tests' leftover state.
"""
import contextvars
import json
import logging
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from starlette.responses import Response

from backend.logging_config import (
    UNSET_CORRELATION_ID,
    CorrelationIdFilter,
    JsonFormatter,
    PlainFormatter,
    get_correlation_id,
    set_correlation_id,
)


def _make_record(msg="hello", extra=None, level=logging.INFO):
    record = logging.LogRecord(
        name="amphive.test", level=level, pathname=__file__, lineno=42,
        msg=msg, args=(), exc_info=None,
    )
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


# ============================================================================
# Correlation id ContextVar (get_correlation_id / set_correlation_id)
# ============================================================================

def test_get_correlation_id_defaults_to_dash_in_a_fresh_context():
    result = contextvars.Context().run(get_correlation_id)
    assert result == UNSET_CORRELATION_ID == "-"


def test_set_then_get_correlation_id_roundtrips():
    def _run():
        set_correlation_id("req-abc123")
        return get_correlation_id()

    assert contextvars.Context().run(_run) == "req-abc123"


def test_set_correlation_id_falls_back_to_dash_for_blank_values():
    def _run():
        set_correlation_id("")
        blank = get_correlation_id()
        set_correlation_id(None)
        none_val = get_correlation_id()
        return blank, none_val

    blank, none_val = contextvars.Context().run(_run)
    assert blank == UNSET_CORRELATION_ID
    assert none_val == UNSET_CORRELATION_ID


# ============================================================================
# CorrelationIdFilter
# ============================================================================

def test_filter_stamps_current_correlation_id_onto_record():
    def _run():
        set_correlation_id("filter-test-id")
        record = _make_record()
        CorrelationIdFilter().filter(record)
        return record.correlation_id

    assert contextvars.Context().run(_run) == "filter-test-id"


def test_filter_stamps_dash_when_unset():
    def _run():
        record = _make_record()
        CorrelationIdFilter().filter(record)
        return record.correlation_id

    assert contextvars.Context().run(_run) == UNSET_CORRELATION_ID


def test_filter_returns_true_so_the_record_is_not_dropped():
    record = _make_record()
    assert CorrelationIdFilter().filter(record) is True


# ============================================================================
# JsonFormatter
# ============================================================================

def test_json_formatter_emits_one_parseable_json_object_carrying_correlation_id():
    def _run():
        set_correlation_id("req-json-1")
        record = _make_record(msg="hello world")
        CorrelationIdFilter().filter(record)
        return JsonFormatter().format(record)

    line = contextvars.Context().run(_run)

    # One JSON object per line: no embedded/trailing newline in the emitted
    # text itself (json.dumps escapes any "\n" inside string values, so this
    # holds regardless of message content).
    assert "\n" not in line

    parsed = json.loads(line)
    assert parsed["correlation_id"] == "req-json-1"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "amphive.test"
    assert parsed["msg"] == "hello world"

    # ts is ISO-8601 and parseable.
    datetime.fromisoformat(parsed["ts"])


def test_json_formatter_includes_extra_structured_fields():
    def _run():
        set_correlation_id("req-json-2")
        record = _make_record(
            msg="Telemetry received",
            extra={"gateway_id": "gw-1", "plug_id": 7, "watts": 12.5},
        )
        CorrelationIdFilter().filter(record)
        return JsonFormatter().format(record)

    parsed = json.loads(contextvars.Context().run(_run))
    assert parsed["gateway_id"] == "gw-1"
    assert parsed["plug_id"] == 7
    assert parsed["watts"] == 12.5


def test_json_formatter_defaults_correlation_id_when_no_filter_attached():
    # A formatter used on a record no CorrelationIdFilter touched must not
    # raise -- it falls back to "-" rather than KeyError/AttributeError.
    record = _make_record()
    parsed = json.loads(JsonFormatter().format(record))
    assert parsed["correlation_id"] == UNSET_CORRELATION_ID


def test_json_formatter_stringifies_values_it_cannot_natively_serialize():
    def _run():
        set_correlation_id("req-json-3")
        record = _make_record(extra={"amount": Decimal("12.50")})
        CorrelationIdFilter().filter(record)
        return JsonFormatter().format(record)

    parsed = json.loads(contextvars.Context().run(_run))
    assert parsed["amount"] == "12.50"


def test_json_formatter_does_not_leak_standard_logrecord_bookkeeping():
    # Only the documented fields + extras should appear -- not internals like
    # pathname/lineno/thread/etc.
    record = _make_record()
    parsed = json.loads(JsonFormatter().format(record))
    for internal_field in ("pathname", "lineno", "thread", "process", "args"):
        assert internal_field not in parsed


# ============================================================================
# PlainFormatter (LOG_FORMAT=plain, local dev readability)
# ============================================================================

def test_plain_formatter_includes_correlation_id_level_and_message():
    def _run():
        set_correlation_id("req-plain-1")
        record = _make_record(msg="hello plain")
        CorrelationIdFilter().filter(record)
        return PlainFormatter().format(record)

    line = contextvars.Context().run(_run)
    assert "req-plain-1" in line
    assert "hello plain" in line
    assert "INFO" in line


# ============================================================================
# HTTP correlation-id middleware (backend/main.py)
# ============================================================================

@pytest.mark.asyncio
async def test_middleware_generates_and_echoes_a_request_id_when_none_sent():
    from backend.main import correlation_id_middleware

    request = MagicMock()
    request.headers = {}
    seen_during_request = {}

    async def call_next(_req):
        # Proves the id is bound for the whole request -- visible to
        # whatever the route handler (here, call_next itself) logs.
        seen_during_request["correlation_id"] = get_correlation_id()
        return Response("ok")

    response = await correlation_id_middleware(request, call_next)

    generated = response.headers["x-request-id"]
    assert generated
    assert seen_during_request["correlation_id"] == generated


@pytest.mark.asyncio
async def test_middleware_echoes_a_caller_supplied_request_id():
    from backend.main import correlation_id_middleware

    request = MagicMock()
    request.headers = {"X-Request-ID": "caller-supplied-id"}

    async def call_next(_req):
        return Response("ok")

    response = await correlation_id_middleware(request, call_next)

    assert response.headers["x-request-id"] == "caller-supplied-id"


@pytest.mark.asyncio
async def test_middleware_generates_a_fresh_id_per_request():
    from backend.main import correlation_id_middleware

    async def call_next(_req):
        return Response("ok")

    request_a, request_b = MagicMock(), MagicMock()
    request_a.headers = {}
    request_b.headers = {}

    response_a = await correlation_id_middleware(request_a, call_next)
    response_b = await correlation_id_middleware(request_b, call_next)

    assert response_a.headers["x-request-id"] != response_b.headers["x-request-id"]
