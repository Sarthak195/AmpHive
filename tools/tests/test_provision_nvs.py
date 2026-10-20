"""Pure-logic tests for tools/gateway_provision/provision_nvs.py.

Nothing here runs ESP-IDF or opens a serial port: subprocess.run is mocked
wherever a code path would call out. All values are fakes.
"""

import csv
import os
import subprocess
import sys
from pathlib import Path

import pytest
from gateway_provision import provision_nvs as pn

GW = "1cc3abb4fb54"
REPO = Path(__file__).resolve().parents[2]
SECRET_KEYS = ("wifi_pwd", "tapo_pwd", "mqtt_pwd", "setup_code")


def _secrets(**over):
    s = {"wifi_ssid": "HomeNet", "wifi_pwd": "wifi-pass-123", "mqtt_pwd": "Mq" * 14,
         "tapo_email": "me@example.com", "tapo_pwd": "tapo-pass-9"}
    s.update(over)
    return s


class _Stdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def _files(tmp_path, s):
    """A secrets file holding `s`, and a stand-in nvs_tool.py (main() only checks it exists)."""
    secrets = tmp_path / "s.txt"
    secrets.write_text("".join(f"{k}={v}\n" for k, v in s.items()), encoding="utf-8")
    tool = tmp_path / "nvs_tool.py"
    tool.write_text("")
    return str(secrets), str(tool)


def _fake_idf(vals, tool, calls, esptool_rc=0):
    """subprocess.run stand-in for the generator, nvs_tool and esptool; records every argv."""
    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "esp_idf_nvs_partition_gen" in cmd:
            Path(cmd[5]).write_bytes(b"\xff" * 0x6000)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if tool in cmd:
            dump = "\n".join(f" storage:{k} = b'{v}'" for k, v in vals.items())
            return subprocess.CompletedProcess(cmd, 0, "Page no. 0, Status: Full\n" + dump, "")
        if "esptool" in cmd:
            return subprocess.CompletedProcess(
                cmd, esptool_rc, "Wrote 24576 bytes at 0x00009000\nHard resetting via RTS pin...\n", "")
        raise AssertionError(f"unexpected call: {cmd}")
    return fake_run


def _main_args(secrets, out, tool, *extra):
    return ["--gateway-id", GW, "--secrets", secrets, "--out", out,
            "--nvs-tool", tool, "--idf-python", "py", *extra]


def test_read_kv_skips_comments_and_blanks_and_keeps_values_verbatim(tmp_path):
    p = tmp_path / "s.txt"
    p.write_bytes(b"# comment\r\n\r\nwifi_ssid=My=Net\r\nnoequals\r\n wifi_pwd = x\n")
    assert pn.read_kv(str(p)) == {"wifi_ssid": "My=Net", "wifi_pwd": " x"}


def test_collect_values_mirrors_the_portal_and_derives_the_ids():
    v = pn.collect_values(GW, _secrets(setup_code="k2m4n6p8q9"), {})
    assert v["gateway_id"] == GW and v["mqtt_user"] == GW
    assert v["device_name"] == f"amphive-{GW}"
    assert v["setup_code"] == "k2m4n6p8q9"
    assert "broker_url" not in v and "plug_cap_a" not in v


def test_tapo_credentials_fall_back_to_the_env_file():
    s = _secrets()
    del s["tapo_email"], s["tapo_pwd"]
    v = pn.collect_values(GW, s, {"TAPO_EMAIL": '"me@example.com"', "TAPO_PASSWORD": ' "pw-1" '})
    assert (v["tapo_email"], v["tapo_pwd"]) == ("me@example.com", "pw-1")
    v = pn.collect_values(GW, s, {"TAPO_USERNAME": "u@example.com", "TAPO_EMAIL": "x"})
    assert v["tapo_email"] == "u@example.com"


def test_absent_setup_code_is_left_to_the_firmware_and_optionals_pass_through():
    v = pn.collect_values(GW, _secrets(plug_cap_a="10"), {})
    assert "setup_code" not in v
    assert v["plug_cap_a"] == "10"


@pytest.mark.parametrize("over, fragment", [
    ({"wifi_ssid": ""}, "wifi_ssid is empty"),
    ({"mqtt_pwd": ""}, "mqtt_pwd is empty"),
    ({"tapo_pwd": ""}, "tapo_pwd is empty"),
    ({"wifi_ssid": "x" * 32}, "wifi_ssid too long"),
    ({"mqtt_pwd": "x" * 64}, "mqtt_pwd too long"),
    ({"setup_code": "short"}, "setup_code must be >= 8"),
    ({"setup_code": "x" * 11}, "setup_code too long"),
    ({"wifi_pwd": "1234567"}, "wifi_pwd shorter than 8"),
    ({"plug_cap_a": "abc"}, "plug_cap_a must be"),
    ({"plug_cap_a": "-3"}, "plug_cap_a must be"),
])
def test_check_rejects(over, fragment):
    problems = pn.check(pn.collect_values(GW, _secrets(**over), {}))
    assert any(fragment in p for p in problems), problems


def test_check_accepts_a_full_set_and_an_open_network():
    assert pn.check(pn.collect_values(GW, _secrets(setup_code="k2m4n6p8q9"), {})) == []
    assert pn.check(pn.collect_values(GW, _secrets(wifi_pwd=""), {})) == []


def test_redact_hides_every_secret():
    v = pn.collect_values(GW, _secrets(setup_code="k2m4n6p8q9"), {})
    out = pn.redact("gen said: " + " | ".join(v.values()), v)
    for k in SECRET_KEYS + ("wifi_ssid", "tapo_email"):
        assert v[k] not in out


def test_nvs_offset_and_size_come_from_the_firmware_partition_table(tmp_path):
    assert pn.nvs_partition(str(REPO / "firmware" / "partitions_ota.csv")) == (0x9000, 0x6000)
    assert pn.nvs_partition(pn.DEFAULT_PARTITIONS) == (0x9000, 0x6000)
    p = tmp_path / "p.csv"
    p.write_text("# nvs, data, nvs, 0x1, 0x2\notadata, data, ota, 0xf000, 0x2000\n")
    with pytest.raises(ValueError):
        pn.nvs_partition(str(p))


def test_idf_python_resolution_order(monkeypatch):
    monkeypatch.delenv("IDF_PYTHON", raising=False)
    monkeypatch.delenv("IDF_PYTHON_ENV_PATH", raising=False)
    assert pn.resolve_idf_python(None) == sys.executable
    monkeypatch.setenv("IDF_PYTHON_ENV_PATH", os.path.join("envs", "idf"))
    assert pn.resolve_idf_python(None).startswith(os.path.join("envs", "idf", ""))
    monkeypatch.setenv("IDF_PYTHON", "envpython")
    assert pn.resolve_idf_python(None) == "envpython"
    assert pn.resolve_idf_python("argpython") == "argpython"


def test_nvs_tool_resolution(monkeypatch):
    monkeypatch.delenv("IDF_PATH", raising=False)
    assert pn.resolve_nvs_tool(None) is None
    monkeypatch.setenv("IDF_PATH", "idf")
    assert pn.resolve_nvs_tool(None) == os.path.join(
        "idf", "components", "nvs_flash", "nvs_partition_tool", "nvs_tool.py")
    assert pn.resolve_nvs_tool("given.py") == "given.py"


def test_build_hands_the_generator_the_portal_layout_and_removes_the_csv(tmp_path, monkeypatch):
    vals = pn.collect_values(GW, _secrets(), {})
    seen = {}

    def fake_run(cmd, **kw):
        with open(cmd[4], newline="", encoding="utf-8") as f:
            seen["rows"] = list(csv.reader(f))
        Path(cmd[5]).write_bytes(b"\xff" * 0x6000)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(pn.subprocess, "run", fake_run)
    pn.build(vals, str(tmp_path / "nvs.bin"), 0x6000, "py")
    rows = seen["rows"]
    assert rows[:2] == [["key", "type", "encoding", "value"], ["storage", "namespace", "", ""]]
    assert {r[0] for r in rows[2:]} == set(vals)
    assert all(r[1:3] == ["data", "string"] for r in rows[2:])
    assert not (tmp_path / "nvs.bin.csv").exists()


def test_build_removes_the_csv_even_when_the_generator_cannot_start(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("no such python")

    monkeypatch.setattr(pn.subprocess, "run", boom)
    with pytest.raises(SystemExit):
        pn.build(pn.collect_values(GW, _secrets(), {}), str(tmp_path / "nvs.bin"), 0x6000, "py")
    assert not (tmp_path / "nvs.bin.csv").exists()


def test_generator_errors_are_redacted(tmp_path, monkeypatch):
    vals = pn.collect_values(GW, _secrets(), {})
    monkeypatch.setattr(pn.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, "bad value: " + vals["mqtt_pwd"], ""))
    with pytest.raises(SystemExit) as e:
        pn.build(vals, str(tmp_path / "nvs.bin"), 0x6000, "py")
    assert vals["mqtt_pwd"] not in str(e.value)


def test_main_builds_and_verifies_without_printing_a_secret(tmp_path, monkeypatch, capsys):
    s = _secrets(setup_code="k2m4n6p8q9")
    secrets, tool = _files(tmp_path, s)
    calls = []
    monkeypatch.setattr(pn.subprocess, "run", _fake_idf(pn.collect_values(GW, s, {}), tool, calls))
    pn.main(_main_args(secrets, str(tmp_path / "nvs.bin"), tool))
    printed = capsys.readouterr().out
    assert "storage keys:" in printed
    for k in SECRET_KEYS:
        assert s[k] not in printed
    assert not (tmp_path / "nvs.bin.csv").exists()
    assert not [c for c in calls if "esptool" in c]  # no --write: the device is never touched


def test_main_refuses_an_id_that_is_not_a_mac():
    with pytest.raises(SystemExit) as e:
        pn.main(["--gateway-id", "fakeplug-gw-01", "--secrets", "x", "--out", "y"])
    assert "12-char" in str(e.value)


def test_write_confirmation_needs_a_terminal_and_the_typed_gateway_id(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Stdin(False))
    with pytest.raises(SystemExit) as e:
        pn.confirm_write(GW)
    assert "terminal" in str(e.value)

    monkeypatch.setattr(sys, "stdin", _Stdin(True))
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    with pytest.raises(SystemExit) as e:
        pn.confirm_write(GW)
    assert "nothing written" in str(e.value)

    monkeypatch.setattr("builtins.input", lambda prompt: f" {GW} ")
    pn.confirm_write(GW)  # confirmed: returns normally


def test_inside_repo_tells_the_checkout_from_everything_else(tmp_path):
    assert pn.inside_repo(str(REPO))
    assert pn.inside_repo(str(REPO / "nvs.bin"))
    assert pn.inside_repo(str(REPO / "tools" / "gateway_provision" / "nvs.bin"))
    assert not pn.inside_repo(str(tmp_path / "nvs.bin"))
    assert not pn.inside_repo(str(REPO) + "-sibling" + os.sep + "nvs.bin")  # prefix, not parent


@pytest.mark.parametrize("opt", ["--secrets", "--out"])
def test_main_refuses_a_secrets_or_image_path_inside_the_repo(tmp_path, monkeypatch, opt):
    def no_tools(cmd, **kw):
        raise AssertionError(f"nothing may run before the path check: {cmd}")

    monkeypatch.setattr(pn.subprocess, "run", no_tools)
    paths = {"--secrets": str(tmp_path / "s.txt"), "--out": str(tmp_path / "nvs.bin")}
    paths[opt] = str(REPO / "provision-test-must-not-exist.bin")
    with pytest.raises(SystemExit) as e:
        pn.main(["--gateway-id", GW, "--secrets", paths["--secrets"], "--out", paths["--out"]])
    assert "inside the repo" in str(e.value) and opt in str(e.value)
    assert not (REPO / "provision-test-must-not-exist.bin").exists()


def test_allow_in_repo_lets_an_in_repo_path_through(tmp_path, monkeypatch):
    secrets, tool = _files(tmp_path, _secrets())
    out = str(REPO / "nvs.bin")  # gitignored (nvs*.bin); build is stubbed, so nothing is written
    built = []
    monkeypatch.setattr(pn, "build", lambda vals, o, size, py: built.append(o))
    monkeypatch.setattr(pn, "verify", lambda *a: None)
    pn.main(_main_args(secrets, out, tool, "--allow-in-repo"))
    assert built == [out]


def test_write_flashes_at_the_nvs_offset_of_the_real_partition_table(tmp_path, monkeypatch, capsys):
    s = _secrets()
    secrets, tool = _files(tmp_path, s)
    out = str(tmp_path / "nvs.bin")
    calls = []
    monkeypatch.setattr(pn.subprocess, "run", _fake_idf(pn.collect_values(GW, s, {}), tool, calls))
    # No --partitions: offset and size come from firmware/partitions_ota.csv.
    pn.main(_main_args(secrets, out, tool, "--write", "COM9", "--yes"))
    # Exactly one esptool call, last (after build + verify), writing the image
    # at nvs's offset 0x9000: never at its size (0x6000, inside the
    # bootloader) or anywhere else.
    assert [c for c in calls if "esptool" in c] == [calls[-1]]
    assert calls[-1] == ["py", "-m", "esptool", "--chip", "auto", "--port", "COM9",
                         "--baud", "115200", "write_flash", "0x9000", out]
    printed = capsys.readouterr().out
    assert "WARNING" in printed and "0x9000" in printed and "COM9" in printed
    assert "NVS written" in printed
    for k in ("wifi_pwd", "tapo_pwd", "mqtt_pwd"):
        assert s[k] not in printed


@pytest.mark.parametrize("tty, answer, why", [
    (False, None, "terminal"),         # no terminal to confirm on, and no --yes
    (True, "yes", "nothing written"),  # confirmed with something other than the id
])
def test_write_without_yes_stops_before_esptool(tmp_path, monkeypatch, tty, answer, why):
    s = _secrets()
    secrets, tool = _files(tmp_path, s)
    calls = []
    monkeypatch.setattr(pn.subprocess, "run", _fake_idf(pn.collect_values(GW, s, {}), tool, calls))
    monkeypatch.setattr(sys, "stdin", _Stdin(tty))
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    with pytest.raises(SystemExit) as e:
        pn.main(_main_args(secrets, str(tmp_path / "nvs.bin"), tool, "--write", "COM9"))
    assert why in str(e.value)
    assert not [c for c in calls if "esptool" in c]


def test_write_goes_ahead_once_the_gateway_id_is_typed(tmp_path, monkeypatch):
    s = _secrets()
    secrets, tool = _files(tmp_path, s)
    calls = []
    monkeypatch.setattr(pn.subprocess, "run", _fake_idf(pn.collect_values(GW, s, {}), tool, calls))
    monkeypatch.setattr(sys, "stdin", _Stdin(True))
    monkeypatch.setattr("builtins.input", lambda prompt: GW)
    pn.main(_main_args(secrets, str(tmp_path / "nvs.bin"), tool, "--write", "COM9"))
    assert calls[-1][2] == "esptool"


def test_a_failed_esptool_write_exits_nonzero(tmp_path, monkeypatch):
    s = _secrets()
    secrets, tool = _files(tmp_path, s)
    monkeypatch.setattr(pn.subprocess, "run",
                        _fake_idf(pn.collect_values(GW, s, {}), tool, [], esptool_rc=2))
    with pytest.raises(SystemExit) as e:
        pn.main(_main_args(secrets, str(tmp_path / "nvs.bin"), tool, "--write", "COM9", "--yes"))
    assert "esptool write failed" in str(e.value)
