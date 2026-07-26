"""
Tests for TapoDirectDriver (backend/services/tapo_direct.py) -- the
direct-control driver that turns a TP-Link Tapo P110 smart plug on/off and
reads its energy usage via the `tapo` Python library (see that module's own
docstring for the WireGuard-bypass architecture this exists for).

DB-free, no real network: `tapo.ApiClient` is patched at the tapo_direct
module's import site with an async stand-in whose `.p110()` call returns a
mocked device -- `.on()` / `.off()` / `.get_energy_usage()` are the only
methods the driver ever awaits on it. If the `tapo` package itself isn't
installed (backend/services/tapo_direct.py imports it unconditionally at
module load time), this whole file skips at collection instead of erroring.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    import backend.services.tapo_direct as tapo_direct_module
    from backend.services.tapo_direct import TapoDirectDriver
except ImportError:
    pytest.skip("tapo package not installed", allow_module_level=True)


def _device():
    """Stand-in for the `tapo` library's per-plug handle: on/off/
    get_energy_usage are all coroutines on the real object."""
    device = MagicMock()
    device.on = AsyncMock()
    device.off = AsyncMock()
    device.get_energy_usage = AsyncMock()
    return device


def _client(device):
    """Stand-in for tapo.ApiClient: p110() is the only call the driver
    awaits on it."""
    client = MagicMock()
    client.p110 = AsyncMock(return_value=device)
    return client


def _driver():
    return TapoDirectDriver("tapo@amphive.test", "hunter2")


@pytest.fixture(autouse=True)
def _no_relay(monkeypatch):
    """Every test here exercises the direct-ApiClient path -- pin
    TAPO_RELAY_URL off so a locally-set env var can't divert calls to the
    HTTP relay branch instead (which would attempt real network I/O)."""
    monkeypatch.setattr(tapo_direct_module, "TAPO_RELAY_URL", None)


# ------------------------------------------------------------- turn_on ------

@pytest.mark.asyncio
async def test_turn_on_calls_client_p110_and_device_on():
    device = _device()
    client = _client(device)

    with patch("backend.services.tapo_direct.ApiClient", return_value=client):
        result = await _driver().turn_on("10.0.0.5")

    assert result is True
    client.p110.assert_awaited_once_with("10.0.0.5")
    device.on.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_turn_on_swallows_device_exception_and_returns_false():
    device = _device()
    device.on.side_effect = RuntimeError("plug unreachable")
    client = _client(device)

    with patch("backend.services.tapo_direct.ApiClient", return_value=client):
        result = await _driver().turn_on("10.0.0.5")

    assert result is False


# ------------------------------------------------------------ turn_off ------

@pytest.mark.asyncio
async def test_turn_off_calls_client_p110_and_device_off():
    device = _device()
    client = _client(device)

    with patch("backend.services.tapo_direct.ApiClient", return_value=client):
        result = await _driver().turn_off("10.0.0.5")

    assert result is True
    client.p110.assert_awaited_once_with("10.0.0.5")
    device.off.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_turn_off_swallows_device_exception_and_returns_false():
    device = _device()
    device.off.side_effect = RuntimeError("plug unreachable")
    client = _client(device)

    with patch("backend.services.tapo_direct.ApiClient", return_value=client):
        result = await _driver().turn_off("10.0.0.5")

    assert result is False


# -------------------------------------------------------- get_energy_usage --

@pytest.mark.asyncio
async def test_get_energy_usage_calls_client_and_returns_usage_dict():
    usage = MagicMock()
    usage.to_dict.return_value = {"current_power": 1500, "today_energy": 320}
    device = _device()
    device.get_energy_usage.return_value = usage
    client = _client(device)

    with patch("backend.services.tapo_direct.ApiClient", return_value=client):
        result = await _driver().get_energy_usage("10.0.0.5")

    assert result == {"current_power": 1500, "today_energy": 320}
    client.p110.assert_awaited_once_with("10.0.0.5")
    device.get_energy_usage.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_get_energy_usage_swallows_device_exception_and_returns_none():
    device = _device()
    device.get_energy_usage.side_effect = RuntimeError("timeout")
    client = _client(device)

    with patch("backend.services.tapo_direct.ApiClient", return_value=client):
        result = await _driver().get_energy_usage("10.0.0.5")

    assert result is None


# ------------------------------------------------------- client is cached ---

@pytest.mark.asyncio
async def test_client_created_once_and_reused_across_calls():
    """_get_client() caches the ApiClient instance on the driver -- a
    second on/off call must not re-authenticate."""
    device = _device()
    client = _client(device)
    driver = _driver()

    with patch("backend.services.tapo_direct.ApiClient", return_value=client) as ApiClientMock:
        await driver.turn_on("10.0.0.5")
        await driver.turn_off("10.0.0.5")

    ApiClientMock.assert_called_once_with("tapo@amphive.test", "hunter2")
