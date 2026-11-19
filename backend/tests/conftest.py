"""Shared test fixtures."""
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _operator_alerts_isolated(monkeypatch):
    """operator_alerts keeps module-level cooldown state and binds notify() and
    send_email() at import. Reset the state and stub both so no test leaks a
    cooldown into another or reaches a real bell, push or SMTP server. Tests
    that exercise the module re-patch these themselves."""
    from backend.services import operator_alerts as oa

    oa._reset_state()
    monkeypatch.setattr(oa, "notify", AsyncMock())
    monkeypatch.setattr(oa, "send_email", MagicMock())
    yield
    oa._reset_state()
