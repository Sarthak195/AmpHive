"""Build (and optionally write over USB) an AmpHive gateway's NVS partition.

The USB alternative to the setup portal (AmpHive_Setup_XXXX at
http://192.168.4.1): the same "storage" keys the portal saves, written
straight into flash. See README.md next to this file and
deploy/docs/gateway_provisioning.md ("Provision over USB (no phone)").

WARNING: --write replaces the WHOLE nvs partition and reboots the gateway.
That also erases the crash-recovery session record, the persisted energy
counters and the offline log. Never write while a charging session is active
on the gateway (2026-10-20: this cut a live session, and the rebooted
gateway force-OFFed the plug as UNAUTHORIZED_ON).

Secrets are read from files and never printed (only key names and lengths):
  --secrets  KEY=VALUE file: wifi_ssid, wifi_pwd, mqtt_pwd, optional
             setup_code, tapo_email/tapo_pwd (or use --env), and optional
             broker_url / plug_cap_a
  --env      optional .env with TAPO_USERNAME (or TAPO_EMAIL) / TAPO_PASSWORD,
             used for whichever Tapo value the secrets file leaves out

Needs ESP-IDF's python env (esp-idf-nvs-partition-gen + esptool) and IDF's
nvs_tool.py: --idf-python, else $IDF_PYTHON, else the env from
$IDF_PYTHON_ENV_PATH (set by IDF's export script), else this interpreter;
--nvs-tool, else $IDF_PATH/components/nvs_flash/nvs_partition_tool/nvs_tool.py.

Usage:
  python provision_nvs.py --gateway-id 1cc3abb4fb54 --secrets S --out nvs.bin              # build + verify
  python provision_nvs.py --gateway-id 1cc3abb4fb54 --secrets S --out nvs.bin --write COM5
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PARTITIONS = os.path.join(HERE, "..", "..", "firmware", "partitions_ota.csv")
# firmware/partitions_ota.csv's nvs row; used only if that file can't be read.
FALLBACK_NVS = (0x9000, 0x6000)
NAMESPACE = "storage"  # keys read by load_config_from_nvs() in firmware/main/main.c
# Firmware buffer sizes (main.c): a value must fit INCLUDING the NUL terminator.
LIMITS = {"wifi_ssid": 32, "wifi_pwd": 64, "device_name": 32, "gateway_id": 32,
          "tapo_email": 64, "tapo_pwd": 64, "mqtt_user": 64, "mqtt_pwd": 64,
          "setup_code": 11, "broker_url": 128, "plug_cap_a": 16}
REQUIRED = ("wifi_ssid", "tapo_email", "tapo_pwd", "mqtt_pwd")
OPTIONAL = ("broker_url", "plug_cap_a")  # written only when the secrets file sets them
SECRET_KEYS = ("wifi_pwd", "tapo_pwd", "mqtt_pwd", "setup_code")
GATEWAY_ID_RE = re.compile(r"^[0-9a-f]{12}$")  # the STA MAC the firmware derives

WRITE_WARNING = """\
WARNING: about to replace the gateway's WHOLE NVS partition ({offset:#x}, {size:#x} bytes) on {port}.
  * esptool resets the board into its bootloader to write and reboots it
    afterwards: the gateway drops offline and a charging session on it is cut.
  * NVS is replaced, not merged: the crash-recovery session record, the
    persisted energy counters and the offline log are erased too. On
    2026-10-20 this cut a live session, and the rebooted gateway then
    force-OFFed the plug as UNAUTHORIZED_ON (relay on, no session).
  Check the CPO dashboard first: there must be NO active session on this gateway."""


def read_kv(path: str) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments skipped; value kept verbatim."""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v
    return out


def _unquote(v: str) -> str:
    return v.strip().strip('"')


def collect_values(gateway_id: str, secrets: dict[str, str],
                   env: dict[str, str]) -> dict[str, str]:
    """The "storage" namespace exactly as the setup portal would save it."""
    tapo_email = secrets.get("tapo_email") or _unquote(
        env.get("TAPO_USERNAME") or env.get("TAPO_EMAIL") or "")
    tapo_pwd = secrets.get("tapo_pwd") or _unquote(env.get("TAPO_PASSWORD", ""))
    vals = {
        "wifi_ssid": secrets.get("wifi_ssid", ""),
        "wifi_pwd": secrets.get("wifi_pwd", ""),
        "device_name": f"amphive-{gateway_id}",
        "gateway_id": gateway_id,  # the firmware re-derives it from the MAC anyway
        "tapo_email": tapo_email,
        "tapo_pwd": tapo_pwd,
        "mqtt_user": gateway_id,  # broker username == gateway id
        "mqtt_pwd": secrets.get("mqtt_pwd", ""),
        "setup_code": secrets.get("setup_code", ""),
    }
    for k in OPTIONAL:
        if secrets.get(k):
            vals[k] = secrets[k]
    if not vals["setup_code"]:
        del vals["setup_code"]  # the firmware generates one on its next portal start
    return vals


def check(vals: dict[str, str]) -> list[str]:
    problems = [f"{k} is empty" for k in REQUIRED if not vals.get(k)]
    for k, v in vals.items():
        lim = LIMITS.get(k)
        if lim is not None and len(v.encode()) >= lim:
            problems.append(f"{k} too long ({len(v.encode())} bytes, max {lim - 1})")
    if "setup_code" in vals and len(vals["setup_code"]) < 8:
        problems.append("setup_code must be >= 8 chars (it is the setup AP's WPA2 passphrase)")
    if vals.get("wifi_pwd") and len(vals["wifi_pwd"]) < 8:
        problems.append("wifi_pwd shorter than 8 chars (WPA2 minimum) - check it")
    if "plug_cap_a" in vals:
        try:
            ok = float(vals["plug_cap_a"]) > 0
        except ValueError:
            ok = False
        if not ok:
            problems.append("plug_cap_a must be a positive number of amps")
    return problems


def redact(text: str, vals: dict[str, str]) -> str:
    """Tool output may echo a value back on error; never let a secret through."""
    for k in SECRET_KEYS + ("wifi_ssid", "tapo_email"):
        v = vals.get(k)
        if v:
            text = text.replace(v, "***")
    return text


def nvs_partition(path: str) -> tuple[int, int]:
    """(offset, size) of the `nvs` row in an ESP-IDF partition table CSV."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            cols = [c.strip() for c in line.split("#", 1)[0].split(",")]
            if len(cols) >= 5 and cols[0] == "nvs":
                return int(cols[3], 0), int(cols[4], 0)
    raise ValueError(f"no nvs row in {path}")


def resolve_idf_python(arg: str | None) -> str:
    if arg:
        return arg
    if os.environ.get("IDF_PYTHON"):
        return os.environ["IDF_PYTHON"]
    env_dir = os.environ.get("IDF_PYTHON_ENV_PATH")
    if env_dir:
        sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
        return os.path.join(env_dir, *sub)
    return sys.executable


def resolve_nvs_tool(arg: str | None) -> str | None:
    if arg:
        return arg
    idf = os.environ.get("IDF_PATH")
    if idf:
        return os.path.join(idf, "components", "nvs_flash", "nvs_partition_tool", "nvs_tool.py")
    return None


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except OSError as e:
        sys.exit(f"cannot run {cmd[0]}: {e}")
    except subprocess.TimeoutExpired:
        sys.exit(f"timed out after {kw.get('timeout')} s: {' '.join(cmd[1:4])} ...")


def build(vals: dict[str, str], out: str, size: int, idf_py: str) -> None:
    csv_path = out + ".csv"
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["key", "type", "encoding", "value"])
            w.writerow([NAMESPACE, "namespace", "", ""])
            for k, v in vals.items():
                w.writerow([k, "data", "string", v])
        r = run([idf_py, "-m", "esp_idf_nvs_partition_gen", "generate",
                 csv_path, out, hex(size)])
    finally:
        if os.path.exists(csv_path):
            os.remove(csv_path)  # plaintext secrets: never leave the CSV around
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) != size:
        sys.exit("nvs_partition_gen failed: " + redact(r.stdout + r.stderr, vals)[-400:])


def verify(out: str, idf_py: str, nvs_tool: str, vals: dict[str, str]) -> None:
    """List namespace:key from the built image; the values are never printed."""
    r = run([idf_py, nvs_tool, "-d", "minimal", "--color", "never", out])
    keys = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith(NAMESPACE + ":") and " = " in line:
            keys.append(line.split(" = ", 1)[0].split(":", 1)[1])
    missing = set(vals) - set(keys)
    if r.returncode != 0 or missing:
        sys.exit(f"verification failed (nvs_tool exit {r.returncode}), missing keys: "
                 f"{sorted(missing)}")
    print(f"built {out}; {NAMESPACE} keys: {', '.join(keys)}")
    print("field lengths:", {k: len(v) for k, v in vals.items()})


def confirm_write(gateway_id: str) -> None:
    if not sys.stdin.isatty():
        sys.exit("refusing to write without a terminal to confirm on (or pass --yes)")
    try:
        answer = input(f"Type the gateway id ({gateway_id}) to write it: ")
    except EOFError:
        answer = ""
    if answer.strip() != gateway_id:
        sys.exit("aborted - nothing written")


def write(out: str, port: str, chip: str, baud: int, offset: int, idf_py: str) -> None:
    cmd = [idf_py, "-m", "esptool", "--chip", chip, "--port", port, "--baud", str(baud),
           "write_flash", hex(offset), out]
    r = run(cmd, timeout=180)
    tail = "\n".join(ln for ln in (r.stdout + r.stderr).splitlines() if "%)" not in ln)[-800:]
    print(tail)
    if r.returncode != 0:
        sys.exit("esptool write failed")
    print("NVS written; the gateway is rebooting with the new config.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway-id", required=True,
                    help="the gateway's MAC id, 12 lowercase hex chars (e.g. 1cc3abb4fb54)")
    ap.add_argument("--secrets", required=True, help="KEY=VALUE secrets file (keep it outside the repo)")
    ap.add_argument("--env", help="optional .env holding TAPO_USERNAME/TAPO_PASSWORD")
    ap.add_argument("--out", required=True, help="where to write the partition image (holds the secrets)")
    ap.add_argument("--write", metavar="PORT", help="also flash it to the gateway on this serial port")
    ap.add_argument("--chip", default="auto", choices=["auto", "esp32", "esp32c3"],
                    help="esptool --chip (default: auto-detect)")
    ap.add_argument("--baud", type=int, default=115200,
                    help="esptool baud rate (default 115200: slow but reliable on a flaky cable)")
    ap.add_argument("--idf-python", help="python of the ESP-IDF env (see module docstring)")
    ap.add_argument("--nvs-tool", help="path to ESP-IDF's nvs_tool.py")
    ap.add_argument("--partitions", default=DEFAULT_PARTITIONS,
                    help="partition table CSV to take the nvs offset/size from")
    ap.add_argument("--yes", action="store_true",
                    help="skip the no-active-session confirmation before --write")
    a = ap.parse_args(argv)

    if not GATEWAY_ID_RE.fullmatch(a.gateway_id):
        sys.exit("refusing: --gateway-id must be the 12-char lowercase hex MAC id")
    try:
        offset, size = nvs_partition(a.partitions)
    except (OSError, ValueError) as e:
        offset, size = FALLBACK_NVS
        print(f"note: {e}; using nvs @ {offset:#x}, size {size:#x}")
    idf_py = resolve_idf_python(a.idf_python)
    nvs_tool = resolve_nvs_tool(a.nvs_tool)
    if not nvs_tool or not os.path.isfile(nvs_tool):
        sys.exit("nvs_tool.py not found: set IDF_PATH (run IDF's export script) or pass --nvs-tool")

    vals = collect_values(a.gateway_id, read_kv(a.secrets), read_kv(a.env) if a.env else {})
    problems = check(vals)
    if problems:
        sys.exit("refusing: " + "; ".join(problems))

    build(vals, a.out, size, idf_py)
    verify(a.out, idf_py, nvs_tool, vals)
    print(f"note: {a.out} holds these secrets in plaintext - delete it when you are done.")

    if a.write:
        print(WRITE_WARNING.format(offset=offset, size=size, port=a.write))
        if not a.yes:
            confirm_write(a.gateway_id)
        write(a.out, a.write, a.chip, a.baud, offset, idf_py)


if __name__ == "__main__":
    main()
