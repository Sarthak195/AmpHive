"""Makes the p110_sim package's flat modules (crypto, plug, server, state,
cli) importable as top-level modules from these tests, regardless of the
directory pytest is invoked from — e.g. `pytest tools/p110_sim/tests` from
the repo root, or `pytest .` from inside tools/p110_sim/. There is no
p110_sim __init__.py by design (see README.md "Layout"): the CLI is meant to
be run directly as `python tools/p110_sim/cli.py`, which relies on Python
auto-adding the script's own directory to sys.path — this conftest just
gives the test suite the same path.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import crypto as em_crypto  # noqa: E402
from plug import PlugConfig, SimulatedPlug  # noqa: E402
from server import PlugHTTPServer, make_server  # noqa: E402

DEFAULT_EMAIL = "bench@amphive.test"
DEFAULT_PASSWORD = "s3cret-test-password"


@pytest.fixture
def auth_hash() -> bytes:
    return em_crypto.auth_hash(DEFAULT_EMAIL, DEFAULT_PASSWORD)


class LivePlug:
    """A running emulator instance on an OS-assigned localhost port, torn
    down automatically at the end of the test."""

    def __init__(self, plug: SimulatedPlug, srv: PlugHTTPServer):
        self.plug = plug
        self.srv = srv
        self.host, self.port = srv.server_address[:2]
        self._thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self._thread.start()
        # Give the listener a moment to actually accept before the first request.
        time.sleep(0.05)

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def live_plug(auth_hash: bytes):
    """Factory fixture: live_plug(**plug_config_overrides) -> LivePlug."""
    created: list[LivePlug] = []

    def _make(**overrides) -> LivePlug:
        defaults = dict(
            plug_id=1,
            label="test",
            host="127.0.0.1",
            port=0,  # OS-assigned free port
            watts=1500.0,
            jitter=0.0,
        )
        defaults.update(overrides)
        cfg = PlugConfig(**defaults)
        plug = SimulatedPlug(cfg, auth_hash)
        srv = make_server(plug)
        lp = LivePlug(plug, srv)
        created.append(lp)
        return lp

    yield _make

    for lp in created:
        lp.close()
