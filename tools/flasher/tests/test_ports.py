"""Unit tests for port ranking/selection logic (amphive_flasher.ports).

No real serial ports involved anywhere — PortInfo is plain data.
"""

from amphive_flasher.ports import (
    PortDecisionKind,
    PortInfo,
    choose_port,
    rank_ports,
)


def make_port(device, vid=None, pid=None, description="", manufacturer=None):
    return PortInfo(device=device, vid=vid, pid=pid, description=description, manufacturer=manufacturer)


def test_rank_ports_empty():
    assert rank_ports([]) == []


def test_espressif_native_usb_ranks_best():
    p_native = make_port("COM3", vid=0x303A, pid=0x1001, description="USB JTAG/serial debug unit")
    p_bridge = make_port("COM4", vid=0x10C4, pid=0xEA60, description="Silicon Labs CP210x")
    p_unknown = make_port("COM5", vid=0xDEAD, pid=0xBEEF, description="Mystery device")

    ranked = rank_ports([p_unknown, p_bridge, p_native])

    assert [r.port.device for r in ranked] == ["COM3", "COM4", "COM5"]
    assert ranked[0].tier == 0
    assert ranked[1].tier == 1
    assert ranked[2].tier == 2


def test_known_uart_bridge_vids_all_recognized():
    for vid in (0x10C4, 0x1A86, 0x0403):
        ranked = rank_ports([make_port("COM9", vid=vid, pid=1)])
        assert ranked[0].tier == 1


def test_unknown_vid_ranks_lowest_but_still_included():
    ranked = rank_ports([make_port("COM6", vid=0x9999, pid=1)])
    assert len(ranked) == 1
    assert ranked[0].tier == 2


def test_port_with_no_vid_still_included():
    ranked = rank_ports([make_port("COM7", vid=None, pid=None, description="Bluetooth link")])
    assert len(ranked) == 1
    assert ranked[0].tier == 2


def test_choose_port_none_found():
    decision = choose_port([])
    assert decision.kind == PortDecisionKind.NONE_FOUND
    assert decision.port is None


def test_choose_port_auto_when_single_best_candidate():
    ports = [
        make_port("COM3", vid=0x303A, pid=1),
        make_port("COM5", vid=0x9999, pid=1),  # unknown, lower tier, shouldn't be picked
    ]
    decision = choose_port(ports)
    assert decision.kind == PortDecisionKind.AUTO
    assert decision.port.device == "COM3"


def test_choose_port_ambiguous_when_two_ports_tie_for_best_tier():
    ports = [
        make_port("COM3", vid=0x10C4, pid=1, description="CP210x #1"),
        make_port("COM4", vid=0x1A86, pid=1, description="CH340 #1"),
    ]
    decision = choose_port(ports)
    assert decision.kind == PortDecisionKind.AMBIGUOUS
    assert {r.port.device for r in decision.candidates} == {"COM3", "COM4"}


def test_choose_port_ambiguous_when_only_unknown_ports_and_multiple():
    ports = [make_port("COM3"), make_port("COM4")]
    decision = choose_port(ports)
    assert decision.kind == PortDecisionKind.AMBIGUOUS


def test_choose_port_explicit_overrides_everything():
    ports = [make_port("COM3", vid=0x303A, pid=1)]
    decision = choose_port(ports, requested_device="COM9")
    assert decision.kind == PortDecisionKind.EXPLICIT
    assert decision.port.device == "COM9"


def test_choose_port_explicit_matches_enumerated_port_metadata():
    ports = [make_port("COM3", vid=0x303A, pid=1, description="Native USB")]
    decision = choose_port(ports, requested_device="COM3")
    assert decision.kind == PortDecisionKind.EXPLICIT
    assert decision.port.vid == 0x303A


def test_choose_port_explicit_with_unlisted_port_still_works():
    # User knows better than the enumeration (e.g. a port that only shows up
    # once opened, or a Bluetooth-backed virtual COM port pyserial mis-scores).
    decision = choose_port([], requested_device="COM42")
    assert decision.kind == PortDecisionKind.EXPLICIT
    assert decision.port.device == "COM42"
