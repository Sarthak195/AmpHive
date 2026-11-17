"""U1: the CPO dashboard revenue must use the Earnings page's definition
(services.payouts.sum_completed_session_coins: coins_spent minus approved
dispute refunds), so the two never disagree. DB-free: the route is driven
with a mocked session and the shared helper stubbed."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.database.models import UserRole
from backend.routers.cpo import _analytics


def _scalar(v):
    r = MagicMock()
    r.scalar.return_value = v
    r.scalar_one_or_none.return_value = v
    return r


def _row(*vals):
    r = MagicMock()
    r.first.return_value = tuple(vals)
    return r


@pytest.mark.asyncio
async def test_dashboard_revenue_comes_from_the_shared_earnings_helper(monkeypatch):
    calls = []

    async def fake_sum(db, tenant_id, window_start=None, window_end=None):
        calls.append(window_start)
        # Lifetime (unbounded) vs today's window: distinguishable values.
        return Decimal("25.00") if window_start is None else Decimal("4.00")

    monkeypatch.setattr(_analytics.payout_service, "sum_completed_session_coins", fake_sum)

    db = AsyncMock()
    # plug count, active sessions, gw online, gw total, tenant tz, today, all-time.
    db.execute = AsyncMock(side_effect=[
        _scalar(1), _scalar(0), _scalar(1), _scalar(1), _scalar("UTC"),
        _row(1.0, 1), _row(1.0, 1),
    ])
    user = MagicMock()
    user.tenant_id = 7
    user.role = UserRole.CPO

    out = await _analytics.cpo_analytics_overview(user=user, db=db)

    assert out["all_time"]["revenue_coins"] == 25.0
    assert out["today"]["revenue_coins"] == 4.0
    # F8: the all-time energy (1.0 kWh) feeds a labelled estimate.
    assert out["impact"]["assumptions"]["estimate"] is True
    assert out["impact"]["all_time"]["co2_avoided_kg"] == round(
        1.0 * _analytics.insights.co2_avoided_kg_per_kwh(), 2
    )
    assert None in calls and any(c is not None for c in calls)


def _rows(*rows):
    r = MagicMock()
    r.all.return_value = list(rows)
    return r


def _sql(stmt):
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect())).lower()


@pytest.mark.asyncio
async def test_overview_today_counts_finished_sessions_like_the_revenue(monkeypatch):
    """Today's energy and session count use ended_at, the window the revenue
    figure (sum_completed_session_coins) uses, not started_at."""
    monkeypatch.setattr(
        _analytics.payout_service, "sum_completed_session_coins",
        AsyncMock(return_value=Decimal("0")),
    )
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar(1), _scalar(0), _scalar(1), _scalar(1), _scalar("UTC"),
        _row(1.0, 1), _row(1.0, 1),
    ])
    user = MagicMock()
    user.tenant_id = 7
    user.role = UserRole.CPO

    await _analytics.cpo_analytics_overview(user=user, db=db)

    today_sql = _sql(db.execute.call_args_list[5].args[0])
    assert "charging_sessions.ended_at >=" in today_sql
    assert "charging_sessions.started_at" not in today_sql


@pytest.mark.asyncio
async def test_revenue_chart_nets_refunds_on_the_sessions_own_day():
    """Mon: a 100-coin session with a 40-coin approved refund -> 60. The
    refund query groups by the session's day, never the approval day."""
    from datetime import date

    mon, tue = date(2026, 11, 16), date(2026, 11, 17)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar("UTC"),
        _rows((mon, Decimal("100.00"), 1), (tue, Decimal("30.00"), 2)),
        _rows((mon, Decimal("40.00"))),
    ])
    user = MagicMock()
    user.tenant_id = 7

    out = await _analytics.cpo_analytics_revenue(user=user, db=db, days=30)

    assert out == [
        {"date": "2026-11-16", "revenue_coins": 60.0, "session_count": 1},
        {"date": "2026-11-17", "revenue_coins": 30.0, "session_count": 2},
    ]
    refund_sql = _sql(db.execute.call_args_list[2].args[0])
    assert "session_disputes.status" in refund_sql
    assert "charging_sessions.started_at" in refund_sql
    assert "resolved_at" not in refund_sql
