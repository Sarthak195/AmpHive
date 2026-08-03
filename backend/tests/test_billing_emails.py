"""
services/billing_emails.py — the session-bill and top-up-receipt emails sent
to drivers, and their env kill-switch. No DB needed for most of this file:
send_session_bill's address lookup is exercised against a fake async-context
session (matches test_notifications.py's _UserDB convention), not a real
Postgres.

What's covered:

1. enabled(): BILLING_EMAILS off/0/false/disabled (any case/whitespace) is
   the kill switch; unset or any other value is on.
2. schedule(): drops (closes, never schedules) the coroutine and returns
   None both when the feature is off and when there's no running loop (e.g.
   a sync call site); when enabled with a loop running, it returns a Task
   that actually executes the coroutine.
3. send_topup_receipt(): composes a subject/body containing the amount, new
   balance, credited-by, and (when given) the note; omits the note line
   otherwise.
4. send_session_bill(): looks the driver up via
   backend.database.db.async_session_factory (patched where the function
   imports it, since the import is local to the call) and composes a body
   with energy kWh, coins billed, balance remaining, the plug name, and the
   /activity link; a missing user row is a silent no-send, and a send_email
   failure is swallowed (never raises) per the module's fire-and-forget
   contract.
5. Wiring: a static check (mirrors test_notifications.py's
   test_finalize_notifies_on_stop_reasons) that finalize_charging_session
   schedules the bill email before returning its receipt. The dynamic,
   DB-backed wiring test for the OTHER call site — the CPO top-up route —
   lives in test_offline_topups.py, next to its existing "world" fixture.
"""
from unittest.mock import MagicMock, patch

import pytest

from backend.services import billing_emails

# ===========================================================================
# enabled() / schedule()
# ===========================================================================

@pytest.mark.parametrize(
    "value", ["off", "OFF", " off ", "0", "false", "FALSE", "disabled", "Disabled"]
)
def test_enabled_false_for_kill_switch_values(monkeypatch, value):
    monkeypatch.setenv("BILLING_EMAILS", value)
    assert billing_emails.enabled() is False


@pytest.mark.parametrize("value", ["on", "ON", "1", "true", "yes"])
def test_enabled_true_for_non_killswitch_values(monkeypatch, value):
    monkeypatch.setenv("BILLING_EMAILS", value)
    assert billing_emails.enabled() is True


def test_enabled_defaults_true_when_unset(monkeypatch):
    monkeypatch.delenv("BILLING_EMAILS", raising=False)
    assert billing_emails.enabled() is True


def test_schedule_disabled_closes_coro_and_returns_none(monkeypatch):
    monkeypatch.setenv("BILLING_EMAILS", "off")

    async def _never_runs():
        pass

    coro = _never_runs()
    result = billing_emails.schedule(coro)
    assert result is None
    assert coro.cr_frame is None  # closed, not left dangling (would GC-warn)


def test_schedule_no_running_loop_closes_coro_and_returns_none(monkeypatch):
    """Called from sync code with no event loop running (e.g. a sync test) —
    must not raise, just drop the coroutine. This test is deliberately a
    plain `def`, not async, so there is no running loop during the call."""
    monkeypatch.delenv("BILLING_EMAILS", raising=False)

    async def _never_runs():
        pass

    coro = _never_runs()
    result = billing_emails.schedule(coro)
    assert result is None
    assert coro.cr_frame is None


@pytest.mark.asyncio
async def test_schedule_enabled_with_loop_creates_a_task_that_runs(monkeypatch):
    monkeypatch.delenv("BILLING_EMAILS", raising=False)
    ran = []

    async def _coro():
        ran.append(True)

    task = billing_emails.schedule(_coro())
    assert task is not None
    await task
    assert ran == [True]


# ===========================================================================
# send_topup_receipt()
# ===========================================================================

@pytest.mark.asyncio
async def test_send_topup_receipt_composes_expected_email():
    with patch("backend.services.billing_emails.send_email") as send_mock:
        await billing_emails.send_topup_receipt(
            to_addr="driver@example.com",
            full_name="Dana Driver",
            amount_coins=25.5,
            new_balance=125.5,
            credited_by="cpo@example.com",
            note="cash, pump 2",
        )

    send_mock.assert_called_once()
    to_addr, subject, body = send_mock.call_args.args
    assert to_addr == "driver@example.com"
    assert "25.50" in subject
    assert "25.50" in body
    assert "125.50" in body
    assert "cpo@example.com" in body
    assert "cash, pump 2" in body


@pytest.mark.asyncio
async def test_send_topup_receipt_without_note_omits_note_line():
    with patch("backend.services.billing_emails.send_email") as send_mock:
        await billing_emails.send_topup_receipt(
            to_addr="driver@example.com",
            full_name=None,
            amount_coins=10.0,
            new_balance=10.0,
            credited_by="cpo@example.com",
        )

    _, _, body = send_mock.call_args.args
    assert "Note:" not in body
    assert "Hi there," in body  # full_name=None falls back


@pytest.mark.asyncio
async def test_send_topup_receipt_swallows_send_email_failure():
    with patch(
        "backend.services.billing_emails.send_email",
        side_effect=RuntimeError("smtp down"),
    ):
        await billing_emails.send_topup_receipt(
            to_addr="driver@example.com",
            full_name="Dana Driver",
            amount_coins=10.0,
            new_balance=10.0,
            credited_by="cpo@example.com",
        )  # must not raise


# ===========================================================================
# send_session_bill()
# ===========================================================================

class _FakeUserLookupDB:
    """Async-context session whose execute() returns a canned `.first()`
    row — matches send_session_bill's `(await db.execute(...)).first()`."""

    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *_a, **_k):
        result = MagicMock()
        result.first.return_value = self._row
        return result


_RECEIPT = {
    "session_id": 42,
    "plug_id": 7,
    "plug_name": "Bay 3",
    "energy_kwh": 12.345,
    "coins_spent": 61.73,
    "balance_remaining": 38.27,
    "price_per_kwh": 5.0,
    "duration_sec": 5400,
    "started_at": "2026-08-03T10:00:00+00:00",
    "ended_at": "2026-08-03T11:30:00+00:00",
}


@pytest.mark.asyncio
async def test_send_session_bill_looks_up_email_and_composes_body():
    fake_db = _FakeUserLookupDB(("driver@example.com", "Dana Driver"))
    with patch("backend.database.db.async_session_factory", lambda: fake_db), \
         patch("backend.services.billing_emails.send_email") as send_mock:
        await billing_emails.send_session_bill(9, _RECEIPT)

    send_mock.assert_called_once()
    to_addr, subject, body = send_mock.call_args.args
    assert to_addr == "driver@example.com"
    assert "12.345" in body       # energy kWh
    assert "61.73" in body        # coins billed
    assert "38.27" in body        # balance remaining
    assert "Bay 3" in body        # plug name
    assert "/activity" in body    # link back to the app's own receipt


@pytest.mark.asyncio
async def test_send_session_bill_missing_user_row_does_not_send():
    fake_db = _FakeUserLookupDB(None)
    with patch("backend.database.db.async_session_factory", lambda: fake_db), \
         patch("backend.services.billing_emails.send_email") as send_mock:
        await billing_emails.send_session_bill(9, _RECEIPT)

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_send_session_bill_swallows_send_email_failure():
    fake_db = _FakeUserLookupDB(("driver@example.com", "Dana Driver"))
    with patch("backend.database.db.async_session_factory", lambda: fake_db), \
         patch(
             "backend.services.billing_emails.send_email",
             side_effect=RuntimeError("smtp down"),
         ):
        await billing_emails.send_session_bill(9, _RECEIPT)  # must not raise


# ===========================================================================
# Wiring
# ===========================================================================

def test_finalize_charging_session_schedules_the_bill_email():
    """Static check that finalize_charging_session hands its receipt to
    billing_emails.schedule() before returning — same technique as
    test_notifications.py's test_finalize_notifies_on_stop_reasons, chosen
    over re-running the full DB-backed finalize fixture (test_auth_holds.py)
    just to re-prove wiring already covered dynamically for the other call
    site (see test_offline_topups.py)."""
    import inspect

    from backend.services import session_lifecycle

    src = inspect.getsource(session_lifecycle.finalize_charging_session)
    assert "receipt = {" in src
    assert "billing_emails.schedule(" in src
    assert "billing_emails.send_session_bill(session.user_id, receipt)" in src
