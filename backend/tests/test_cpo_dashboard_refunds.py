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
    assert None in calls and any(c is not None for c in calls)
