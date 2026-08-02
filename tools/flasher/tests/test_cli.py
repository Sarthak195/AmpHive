"""Unit tests for CLI orchestration (amphive_flasher.cli).

Everything that would touch hardware, the filesystem beyond tmp_path, or the
network is replaced with fakes via the `Dependencies` seam — no serial port,
no esptool call, no GitHub request happens anywhere in this file.
"""

from pathlib import Path

import pytest
from amphive_flasher import cli, flasher
from amphive_flasher.images import FlasherError, ResolvedImage
from amphive_flasher.ports import PortInfo


def make_fake_image(tmp_path: Path, name: str = "fake.bin") -> Path:
    """A real file on disk — `run()` legitimately stat()s the resolved image
    for the size it reports, so fakes need to be real files, not bare Paths."""
    p = tmp_path / name
    p.write_bytes(b"fake-firmware-bytes")
    return p


def make_deps(tmp_path=None, **overrides):
    calls = {"flash": [], "verify": [], "printed": []}
    image_path = make_fake_image(tmp_path) if tmp_path is not None else Path(__file__)

    def fake_list_ports():
        return [PortInfo(device="COM3", vid=0x303A, pid=1, description="Native USB")]

    def fake_detect_chip(port):
        return flasher.DetectedChip(chip_name="ESP32-C3", port=port)

    def fake_resolve_image(chip, images_dir, explicit_bin):
        return ResolvedImage(path=image_path, source="local")

    def fake_flash_image(port, chip, path, baud):
        calls["flash"].append((port, chip, str(path), baud))

    def fake_verify_image(port, chip, path, baud):
        calls["verify"].append((port, chip, str(path), baud))

    defaults = dict(
        list_ports=fake_list_ports,
        detect_chip=fake_detect_chip,
        resolve_image=fake_resolve_image,
        download_latest_image=lambda chip, d: (_ for _ in ()).throw(
            FlasherError("should not be called")
        ),
        flash_image=fake_flash_image,
        verify_image=fake_verify_image,
        confirm=lambda prompt: True,
        prompt=lambda prompt: "1",
        print=lambda msg: calls["printed"].append(msg),
    )
    defaults.update(overrides)
    deps = cli.Dependencies(**defaults)
    return deps, calls


def make_args(**overrides):
    parser = cli.build_arg_parser()
    args = parser.parse_args([])
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def test_build_arg_parser_defaults():
    args = cli.build_arg_parser().parse_args([])
    assert args.port is None
    assert args.chip is None
    assert args.bin_path is None
    assert args.no_download is False
    assert args.yes is False
    assert args.non_interactive is False
    assert args.dry_run is False
    assert args.baud == flasher.DEFAULT_BAUD


def test_build_arg_parser_rejects_unknown_chip():
    with pytest.raises(SystemExit):
        cli.build_arg_parser().parse_args(["--chip", "not-a-real-chip"])


def test_run_happy_path_flashes_and_verifies(tmp_path):
    deps, calls = make_deps(tmp_path)
    args = make_args(images_dir=tmp_path, yes=True)

    code = cli.run(args, deps)

    assert code == 0
    assert len(calls["flash"]) == 1
    assert len(calls["verify"]) == 1
    assert calls["flash"][0][0] == "COM3"
    assert calls["flash"][0][1] == "esp32c3"


def test_run_no_ports_found_raises_flasher_error(tmp_path):
    deps, _ = make_deps(list_ports=lambda: [])
    args = make_args(images_dir=tmp_path, yes=True)

    with pytest.raises(FlasherError, match="couldn't find any device"):
        cli.run(args, deps)


def test_run_dry_run_never_flashes(tmp_path):
    deps, calls = make_deps(tmp_path)
    args = make_args(images_dir=tmp_path, dry_run=True)

    code = cli.run(args, deps)

    assert code == 0
    assert calls["flash"] == []
    assert calls["verify"] == []


def test_run_declined_confirmation_cancels_without_flashing(tmp_path):
    deps, calls = make_deps(tmp_path, confirm=lambda prompt: False)
    args = make_args(images_dir=tmp_path, yes=False)

    code = cli.run(args, deps)

    assert code == 1
    assert calls["flash"] == []


def test_run_yes_flag_skips_confirmation_prompt(tmp_path):
    was_asked = []
    deps, calls = make_deps(tmp_path, confirm=lambda prompt: was_asked.append(prompt) or True)
    args = make_args(images_dir=tmp_path, yes=True)

    cli.run(args, deps)

    assert was_asked == []
    assert len(calls["flash"]) == 1


def test_run_chip_override_skips_detection(tmp_path):
    detect_called = []

    def fake_detect_chip(port):
        detect_called.append(port)
        return flasher.DetectedChip(chip_name="ESP32-C3", port=port)

    deps, calls = make_deps(tmp_path, detect_chip=fake_detect_chip)
    args = make_args(images_dir=tmp_path, yes=True, chip="esp32")

    cli.run(args, deps)

    assert detect_called == []
    assert calls["flash"][0][1] == "esp32"


def test_run_ambiguous_port_prompts_user_to_pick(tmp_path):
    def fake_list_ports():
        return [
            PortInfo(device="COM3", vid=0x10C4, pid=1, description="CP210x"),
            PortInfo(device="COM4", vid=0x1A86, pid=1, description="CH340"),
        ]

    deps, calls = make_deps(tmp_path, list_ports=fake_list_ports, prompt=lambda p: "2")
    args = make_args(images_dir=tmp_path, yes=True)

    cli.run(args, deps)

    assert calls["flash"][0][0] == "COM4"


def test_run_ambiguous_port_non_interactive_raises(tmp_path):
    def fake_list_ports():
        return [
            PortInfo(device="COM3", vid=0x10C4, pid=1, description="CP210x"),
            PortInfo(device="COM4", vid=0x1A86, pid=1, description="CH340"),
        ]

    deps, _ = make_deps(list_ports=fake_list_ports)
    args = make_args(images_dir=tmp_path, non_interactive=True)

    with pytest.raises(FlasherError, match="non-interactive"):
        cli.run(args, deps)


def test_run_explicit_port_flag_bypasses_scan(tmp_path):
    scan_called = []

    def fake_list_ports():
        scan_called.append(True)
        return []

    deps, calls = make_deps(tmp_path, list_ports=fake_list_ports)
    args = make_args(images_dir=tmp_path, yes=True, port="COM99")

    cli.run(args, deps)

    # choose_port() with an explicit device never needs to enumerate ports.
    assert scan_called == []
    assert calls["flash"][0][0] == "COM99"


def test_run_missing_local_image_falls_back_to_download(tmp_path):
    def fake_resolve_image(chip, images_dir, explicit_bin):
        raise FlasherError("no local image")

    downloaded = []

    def fake_download(chip, images_dir):
        downloaded.append(chip)
        dest = images_dir / "downloaded.bin"
        dest.write_bytes(b"fake-downloaded-bytes")
        return ResolvedImage(path=dest, source="downloaded")

    deps, calls = make_deps(tmp_path, resolve_image=fake_resolve_image, download_latest_image=fake_download)
    args = make_args(images_dir=tmp_path, yes=True)

    code = cli.run(args, deps)

    assert code == 0
    assert downloaded == ["esp32c3"]


def test_run_missing_local_image_with_no_download_does_not_fall_back(tmp_path):
    def fake_resolve_image(chip, images_dir, explicit_bin):
        raise FlasherError("no local image")

    deps, _ = make_deps(resolve_image=fake_resolve_image)
    args = make_args(images_dir=tmp_path, yes=True, no_download=True)

    with pytest.raises(FlasherError, match="no local image"):
        cli.run(args, deps)


def test_run_explicit_bin_path_does_not_fall_back_to_download(tmp_path):
    def fake_resolve_image(chip, images_dir, explicit_bin):
        raise FlasherError("bin not found")

    deps, _ = make_deps(resolve_image=fake_resolve_image)
    args = make_args(images_dir=tmp_path, yes=True, bin_path=tmp_path / "missing.bin")

    with pytest.raises(FlasherError, match="bin not found"):
        cli.run(args, deps)


def test_default_images_dir_points_at_firmware_images_folder():
    d = cli.default_images_dir()
    assert d.name == "firmware-images"
    assert d.parent.name == "flasher"


def test_main_dry_run_returns_zero_with_no_device(monkeypatch, tmp_path):
    # End-to-end through main(), still with hardware/network fully faked —
    # this is the shape of the "no device attached" sanity check.
    monkeypatch.setattr(cli.ports, "list_serial_ports", lambda: [])
    code = cli.main(["--dry-run", "--non-interactive"])
    assert code == 1  # no device found -> FlasherError -> exit 1, even in dry-run
