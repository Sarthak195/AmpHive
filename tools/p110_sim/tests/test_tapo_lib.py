"""Integration tests driving the emulator with the REAL `tapo` PyPI client
library (the one tools/local_tapo_test.py and tools/turn_on.py/turn_off.py
already use) instead of a hand-rolled client. Passing this is strong,
independent evidence that the emulator's KLAP dialect is correct — a bug
that happened to be symmetric between crypto.py and a hand-rolled test
client (test_server_klap.py) would NOT fool this one, since none of this
emulator's code was used to write the `tapo` crate.

Caveat (see README.md "Protocol dialect" and the mission report): the
`tapo` client's `.p110()` auto-negotiates between an AES/"securePassthrough"
transport and KLAP, trying AES first via an unauthenticated POST /app
`component_nego` call. A real P110 in "Third-Party Compatibility" mode
answers that probe with `error_code: 1003`, which is what the client's
`is_aes_supported()` (tapo/src/api/protocol/tapo_protocol.rs) recognizes as
"fall back to KLAP" — so the emulator replies the same way. Everything past
that point (handshake1/handshake2/request) is genuine KLAP, identical to
what firmware/main/tapo_protocol.c speaks.
"""

import asyncio

import pytest

tapo = pytest.importorskip("tapo", reason="tapo PyPI client not installed in this environment")

from conftest import DEFAULT_EMAIL, DEFAULT_PASSWORD  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def test_get_device_info_on_off_and_energy_usage(live_plug):
    live = live_plug(watts=1800.0, jitter=0.0)

    async def scenario():
        client = tapo.ApiClient(DEFAULT_EMAIL, DEFAULT_PASSWORD)
        addr = f"{live.host}:{live.port}"
        device = await client.p110(addr)

        info = (await device.get_device_info()).to_dict()
        assert info["model"] == "P110"
        assert info["device_on"] is False

        await device.on()
        info_on = (await device.get_device_info()).to_dict()
        assert info_on["device_on"] is True

        energy = (await device.get_energy_usage()).to_dict()
        assert energy["current_power"] == 1800000  # mW

        await device.off()
        info_off = (await device.get_device_info()).to_dict()
        assert info_off["device_on"] is False

    _run(scenario())


def test_wrong_credentials_are_rejected(live_plug):
    live = live_plug()

    async def scenario():
        client = tapo.ApiClient(DEFAULT_EMAIL, "not-the-configured-password")
        addr = f"{live.host}:{live.port}"
        with pytest.raises(Exception):
            await client.p110(addr)

    _run(scenario())


def test_multiple_plugs_are_independently_addressable(live_plug):
    """The whole point of the emulator: N plugs, each with its own KLAP
    session and energy state, reachable at distinct host:port pairs on one
    machine — exactly the roster shape the backend pushes on
    amphive/gateways/{gw}/config."""
    live_a = live_plug(plug_id=101, label="a", watts=1000.0, jitter=0.0)
    live_b = live_plug(plug_id=102, label="b", watts=5000.0, jitter=0.0)

    async def scenario():
        client = tapo.ApiClient(DEFAULT_EMAIL, DEFAULT_PASSWORD)
        dev_a = await client.p110(f"{live_a.host}:{live_a.port}")
        dev_b = await client.p110(f"{live_b.host}:{live_b.port}")

        await dev_a.on()
        energy_a = (await dev_a.get_energy_usage()).to_dict()
        energy_b_idle = (await dev_b.get_energy_usage()).to_dict()

        assert energy_a["current_power"] == 1000000
        assert energy_b_idle["current_power"] == 0  # plug B was never turned on

        await dev_b.on()
        energy_b = (await dev_b.get_energy_usage()).to_dict()
        assert energy_b["current_power"] == 5000000

        # Turning plug B off must not affect plug A's relay.
        await dev_b.off()
        info_a = (await dev_a.get_device_info()).to_dict()
        assert info_a["device_on"] is True

    _run(scenario())


def test_two_plugs_can_share_one_host_on_different_ports(live_plug):
    """Exercises the ':port' addressing this emulator relies on for a
    single-host multi-plug bench setup (see firmware/main/session_nvs.h
    PLUG_IP_MAX_LEN and README.md 'Addressing'). Both plugs bind the same
    host with port=0 (OS-assigned) -- the OS guarantees distinct ports for
    two independently-bound listeners, which is exactly the "one IP,
    multiple ports" shape the roster's ":port" suffix addresses.
    """
    host = "127.0.0.1"
    live_a = live_plug(plug_id=201, label="porta", host=host, port=0)
    live_b = live_plug(plug_id=202, label="portb", host=host, port=0)

    assert live_a.host == live_b.host == host
    assert live_a.port != live_b.port

    async def scenario():
        client = tapo.ApiClient(DEFAULT_EMAIL, DEFAULT_PASSWORD)
        dev_a = await client.p110(f"{live_a.host}:{live_a.port}")
        dev_b = await client.p110(f"{live_b.host}:{live_b.port}")
        assert (await dev_a.get_device_info()).to_dict()["model"] == "P110"
        assert (await dev_b.get_device_info()).to_dict()["model"] == "P110"

    _run(scenario())
