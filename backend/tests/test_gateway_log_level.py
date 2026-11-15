"""Severity parsing of forwarded firmware log lines (services/mqtt/logs.py).

fw >= 2.8.0 replays lines it held through an MQTT outage as
"[offline] <original line>", so the level letter is no longer the first
character (or the first after a colour escape). Those lines must keep their
warning/error severity, or the admin/CPO filters hide exactly the lines that
explain an outage.
"""
from unittest.mock import MagicMock

import pytest

from backend.services.mqtt.logs import MQTTLogsMixin


@pytest.mark.parametrize(
    ("line", "level"),
    [
        ("E (123) TAG: boom", "E"),
        ("W (123) TAG: careful", "W"),
        ("\033[0;31mE (123) TAG: boom\033[0m", "E"),
        ("[offline] W (123) TAG: careful", "W"),
        ("[offline] \033[0;33mW (123) wifi: dropped\033[0m", "W"),
        ("[offline] \033[0;31mE (123) mqtt: tls\033[0m", "E"),
        ("[offline] ", ""),
        ("", ""),
        ("I (5) TAG: fine", "I"),
    ],
)
def test_log_line_level(line, level):
    assert MQTTLogsMixin._log_line_level(line) == level


class _Mgr(MQTTLogsMixin):
    def __init__(self):
        self.db_session_factory = object()
        self.event_loop = object()
        self._persist_gateway_log = MagicMock(return_value="coro")


@pytest.mark.parametrize(
    ("line", "severity"),
    [
        ("[offline] \033[0;33mW (9) wifi: dropped\033[0m", "warning"),
        ("[offline] \033[0;31mE (9) mqtt: tls\033[0m", "error"),
        ("[offline] I (9) TAG: info", "info"),
    ],
)
def test_offline_replay_keeps_its_severity(monkeypatch, line, severity):
    seen = []
    monkeypatch.setattr(
        "backend.services.mqtt.logs.asyncio.run_coroutine_threadsafe",
        lambda coro, loop: seen.append(coro),
    )
    mgr = _Mgr()
    mgr._handle_gateway_log("gw-1", line)
    mgr._persist_gateway_log.assert_called_once_with("gw-1", severity, line)
    assert seen == ["coro"]
