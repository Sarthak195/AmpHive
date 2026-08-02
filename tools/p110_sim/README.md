# P110 emulator — multi-plug bench without hardware

A software stand-in for **real TP-Link Tapo P110 smart plugs**, speaking the
exact same local KLAP protocol `firmware/main/tapo_protocol.c` speaks to a
real plug over the LAN. Unlike `tools/fake_plug.py` (which fakes the whole
*gateway* over MQTT and never touches a real ESP32), this emulator sits at
the *plug* layer: point a **real** ESP32-C3 gateway at it and the firmware
cannot tell the difference from a real P110.

The point: the owner has only one physical P110, but a gateway must serve
**multiple** plugs, and that multi-plug path (TD#20, `MAX_PLUGS=4`) has never
been validated against more than one real plug. This tool emulates N plugs
so the real firmware/backend/gateway stack can be exercised end-to-end.

## Layout

```
tools/p110_sim/
├── crypto.py    KLAP v2 crypto (auth_hash, session derive, seal/unseal)
├── plug.py      SimulatedPlug — state machine + JSON-RPC method dispatch
├── server.py    per-plug threaded HTTP server (KLAP handshake/request)
├── state.py     JSON-file state persistence (energy/relay across restarts)
├── cli.py       argparse CLI — the thing you actually run
└── tests/       pytest suite (unit + integration, see "Tests" below)
```

No package `__init__.py` on purpose: run the CLI directly
(`python tools/p110_sim/cli.py ...`) — Python auto-adds the script's own
directory to `sys.path`, so the flat `import crypto` / `import plug` style
inside these modules just works, with no `pip install -e` step.

Dependencies: stdlib (`http.server`, `socketserver`, `argparse`, `hashlib`,
`hmac`) plus `cryptography` for AES-128-CBC/PKCS7 — the same library
`tools/klap_probe.py` already uses, already in `backend/requirements*.txt`.

## Protocol dialect (what was reverse-engineered / confirmed)

Ground truth: `firmware/main/tapo_protocol.c` (a real KLAP v2 driver,
validated against a real P110 fw 1.1.3 via `tools/klap_probe.py`) and
`docs/FIRMWARE.md §4`.

- **Transport:** plain HTTP, no TLS, `POST http://<ip>/app/...`. The
  firmware builds the URL with a bare `"http://%s/..."` substitution
  (`tapo_protocol.c:235,268,342`) — no port is ever added, so a real P110 is
  always contacted on port 80.
- **Auth variant: KLAP v2** (SHA1/SHA256-derived auth hash), **not** the
  older v1 (MD5) variant and **not** RSA-based "securePassthrough" —
  confirmed by `klap_probe.py`'s auto-detection against real hardware and
  mirrored exactly in `tapo_protocol.c`'s `klap_handshake()`
  (`tapo_protocol.c:230-306`).
- **Handshake:**
  1. `POST /app/handshake1`, body = `local_seed` (16 random bytes). Plug
     replies `remote_seed(16) || server_hash(32)` where
     `server_hash = SHA256(local_seed‖remote_seed‖auth_hash)`, plus
     `Set-Cookie: TP_SESSIONID=...`. `auth_hash = SHA256(SHA1(email)‖SHA1(password))`
     — the Tapo account credentials, collected once by the captive portal
     and shared across every plug on the gateway (`tapo_protocol.c:415-441`).
  2. `POST /app/handshake2`, body = `SHA256(remote_seed‖local_seed‖auth_hash)`
     (32 bytes), with the `TP_SESSIONID` cookie. 200 = session established.
  3. Session key material, all `SHA256(prefix‖local_seed‖remote_seed‖auth_hash)`:
     `key = ...("lsk"...)​[0:16]`, `iv = ...("iv"...)[0:12]`,
     `seq0 = be32_signed(...("iv"...)[28:32])`, `sig = ...("ldk"...)[0:28]`.
  4. `POST /app/request?seq=N` (N increments per request), body =
     `SHA256(sig‖be32(seq)‖ciphertext) || AES-128-CBC/PKCS7(json, key, iv‖be32(seq))`.
     The **response** re-uses the *same* `seq`/IV to encrypt/decrypt — request
     and response share one IV per exchange (`tapo_protocol.c:308-365`; this
     emulator's `crypto.KlapSession.seal()` is deliberately symmetric for
     exactly this reason — see its docstring).
  5. Session re-handshakes hourly (`KLAP_SESSION_TTL_MS`) or on any `403`.
- **JSON-RPC methods the firmware calls** (`tapo_protocol.c:502-612`):
  `get_device_info` (→ `device_on`, `overheat_status`, `overcurrent_status`),
  `get_energy_usage` (→ `current_power` mW, plus **real** `current_ma` /
  `voltage_mv` when the plug reports them — the firmware prefers these over
  its nominal/derived fallback), `set_device_info {"device_on":bool}`. The
  firmware does **not** call `get_current_power` (this emulator implements
  it anyway, cheaply, for completeness / library compatibility). All
  responses are the standard Tapo envelope `{"error_code":0,"result":{...}}`
  — the firmware's own parser is a dumb `strstr` scan
  (`json_int`/`json_true`/`json_status_abnormal` in `tapo_protocol.c`), so it
  doesn't care about nesting, but this emulator matches the real shape
  anyway for fidelity against `tapo`-lib and a real device.
- **Roster addressing** (`amphive/gateways/{gw}/config`, see
  `docs/MQTT_CONTRACT.md`): each entry is `{"plug_id":int,"local_ip":str,
  "max_current_a":float|null}`. `local_ip` used to be bare-IP-only
  (`PLUG_IP_MAX_LEN` was 16 = `"255.255.255.255"` + NUL). See "Addressing"
  below for what changed.

## Emulator fidelity

- **Crypto/handshake:** implements the formulas above exactly
  (`crypto.py`), independently re-derived (not copy-pasted) from
  `tapo_protocol.c`/`klap_probe.py` for the pytest suite's cross-check value.
- **State:** relay on/off, energy accumulation (trapezoidal-equivalent —
  simple `power × dt` integration while the relay is ON), all persisted to
  one JSON file (`--state-file`) so a restart resumes where it left off.
- **Realistic telemetry shape:** `current_ma`/`voltage_mv` are **measured**
  (mirroring what a real P110 reports), derived from the configured watts
  with `power_factor < 1` so `current != power / voltage` — matching the
  documented real-hardware behavior (`docs/MQTT_CONTRACT.md`'s note on
  `current`, and the same modeling choice `tools/fake_plug.py` makes on the
  MQTT-fake side). Small mains-voltage jitter (±0.5%) is independent of the
  load-watts jitter (`--jitter`).
- **`device_info`** includes the full field set a real P110 reports
  (`device_id`, `fw_ver`, `mac`, base64 `nickname`/`ssid`, etc.) — needed for
  the real `tapo` client library's strict deserialization, not just the 3
  fields the firmware actually reads.

## What's verified — and what isn't

1. **Firmware dialect (KLAP v2), independently confirmed twice:**
   - A hand-rolled client re-implementing the same formulas from scratch
     (not sharing code with `crypto.py`) round-trips handshake + get/set
     calls against a live emulator instance (`tests/test_server_klap.py`).
   - This mirrors `tools/klap_probe.py`, which is the script the team
     already validated against **real P110 hardware** (fw 1.1.3) before
     porting the protocol to C. Same formulas, same field names, same wire
     shape.
2. **The real `tapo` PyPI client library** (`tests/test_tapo_lib.py`) also
   round-trips `get_device_info`/`on`/`off`/`get_energy_usage` against the
   emulator. This needed one extra thing the firmware itself never touches:
   the `tapo` crate's `ApiClient.p110()` **always** probes the newer
   unencrypted AES/"securePassthrough" transport first (an unauthenticated
   `POST /app` `component_nego` call) before falling back to KLAP. Its exact
   fallback rule, read straight from the crate's source
   (`tapo/src/api/protocol/tapo_protocol.rs::is_aes_supported`):
   `error_code == 0` → use AES; `error_code == 1003` → **not supported, use
   KLAP**; anything else → fatal, no fallback. A real P110 in "Third-Party
   Compatibility" mode answers `1003` there, so `server.py`'s bare `/app`
   handler does the same (see the comment at its `elif path == "/app":`
   branch) — this is the *only* part of the emulator that exists purely for
   the `tapo` library's benefit; the firmware never sends that request.
3. **Caveat — what is NOT independently hardware-verified by this task:**
   the emulator's crypto was cross-checked against two independent client
   implementations (a hand-rolled one and the real `tapo` crate) but **not**
   re-run against a real physical P110 in this session — that ground truth
   comes transitively from `klap_probe.py`/`tapo_protocol.c` already having
   been validated against real hardware previously. The strongest remaining
   check is the bench procedure below: point a **real gateway** at the
   emulator and confirm it authenticates and drives sessions.

## Addressing: one plug per LAN IP, or one plug per port?

A real P110 only ever listens on port 80, so the *natural* way to run
several plugs is several IPs on the bench machine (see "no-firmware-change
alternative" below). But provisioning secondary IPs is extra bench setup,
so this emulator's default CLI mode (`--host`/`--base-port`) runs several
plugs on **one IP at different ports** — which needed a small firmware
change, since the roster's `local_ip` field was bare-IP-only.

**What changed (`firmware/main/session_nvs.h`, `firmware/main/tapo_protocol.c`):**
`PLUG_IP_MAX_LEN` was 16 bytes (`"255.255.255.255"` + NUL — no room for a
port suffix). The HTTP URL builders in `tapo_protocol.c` already do a plain
`"http://%s/..."` substitution, so a `"host:port"` string works as a URL
host:port with **zero** other code changes — the only thing that needed to
grow was the buffer. Bumped to 22 bytes (`"255.255.255.255:65535"` + NUL).
Purely additive and backward-compatible: existing bare-IP roster entries
(≤15 chars) are unaffected, the backend's `plugs.local_ip` column
(`String(45)`) and Pydantic schemas already accept arbitrary strings with no
format validation (checked — no backend change needed at all), and the NVS
session blob's self-describing size check
(`session_nvs.c`'s `sz != expected` guard) already makes a `session_params_t`
layout change across an OTA safe by the same precedent every prior field
addition to that struct relied on (OTA is refused while a session is
active, so there's never a live session to lose across the upgrade).

Build-verified with ESP-IDF v5.3.3 (`idf.py set-target esp32c3 && idf.py
build`) — compiles clean, produces a normal-sized signed image. **Not
flashed to a real gateway as part of this change** — OTA to field units is
operator-gated (see `docs/DEPLOYMENT.md` / `AGENTS.md`); the bench procedure
below calls out exactly when a real gateway would need this build.

**No-firmware-change alternative** (if you'd rather not OTA the bench
gateway): give the bench machine one secondary IP per plug and run each
emulator instance on port 80 of its own IP (`--hosts`, `--port 80`).

- Windows: `netsh interface ip add address "Ethernet" 192.168.1.101 255.255.255.0`
  (repeat per extra IP; `netsh ... show config` to find your adapter name;
  `netsh interface ip delete address "Ethernet" 192.168.1.101` to remove).
  Binding port 80 needs an elevated (Administrator) shell.
- Linux: `sudo ip addr add 192.168.1.101/24 dev eth0` (repeat per IP; `sudo
  ip addr del 192.168.1.101/24 dev eth0` to remove). Binding port 80 needs
  root or `sudo setcap 'cap_net_bind_service=+ep' $(which python3)`.

Either way, the roster (`plugs.local_ip`) just needs to match whatever
addressing mode you picked — `"192.168.1.50:9441"` for the port mode, or
`"192.168.1.101"` for the secondary-IP mode.

## Running it

```bash
# Single host, 3 plugs on incrementing ports (needs the firmware change above
# on the gateway you point at it):
python tools/p110_sim/cli.py --count 3 --host 0.0.0.0 --base-port 9440 \
    --email you@example.com --password 'your-tapo-third-party-password' \
    --watts 1500,3300,0 --start-kwh 0,12.4,0 \
    --state-file tools/p110_sim/state/bench.json

# Secondary-IP mode, no firmware change needed:
python tools/p110_sim/cli.py --hosts 192.168.1.101,192.168.1.102,192.168.1.103 \
    --port 80 --email you@example.com --password '...'
```

`--email`/`--password` are the Tapo account credentials the emulator will
accept — they don't have to be real Tapo-cloud credentials (the emulator
never talks to the cloud), but they **must** be exactly what you configure
in the gateway's captive portal, since that's what derives `auth_hash` on
the firmware side.

Key flags (`--help` for the full list): `--jitter` (load noise, default
2%), `--voltage`/`--power-factor` (default 230V / 0.95), `--drop-rate` /
`--drop-rate-map PLUGID=RATE` (flaky-plug simulation), `--reset-counter
PLUGID@SECONDS[@VALUE_KWH]` (scheduled full-counter/power-cycle reset),
`--daily-reset-counter PLUGID@SECONDS` (scheduled `today_energy`-only
rollover, simulating the P110's own midnight reset — see scenario 7 below).
Ctrl-C stops all plugs cleanly.

## Bench procedure (real gateway + emulator)

1. **Start the emulator** with 3 plugs (single-host/port mode shown; see
   above for the secondary-IP alternative):
   ```bash
   python tools/p110_sim/cli.py --count 3 --host <bench-machine-LAN-IP> \
       --base-port 9440 --email bench@example.com --password 'bench-pw' \
       --watts 1800,3600,0 --start-kwh 0,0,5
   ```
2. **Flash the bench gateway** with a firmware build that includes this
   change (`idf.py build` from `firmware/`, per the top-level build/flash
   steps in `docs/FIRMWARE.md §7`) — only needed for the port-addressing
   mode; skip if using secondary IPs. Set the gateway's captive-portal Tapo
   email/password to match `--email`/`--password` above.
3. **Register the gateway and 3 plugs** via the CPO API (same pattern
   `tools/fake_plug.py` uses for its own registration, just pointed at real
   IPs/ports instead):
   ```bash
   # gateway (gateway_id must match the real device's derived id, shown on
   # the unit / in the captive portal serial log)
   curl -X POST $API_BASE/api/cpo/gateways -H "Authorization: Bearer $TOKEN" \
        -d '{"gateway_id":"<gw-mac>","name":"P110 sim bench"}'

   # one plug per emulator instance — local_ip is "host:port" (port mode) or
   # a bare secondary IP (IP mode)
   curl -X POST $API_BASE/api/cpo/plugs -H "Authorization: Bearer $TOKEN" \
        -d '{"gateway_id":"<gw-mac>","name":"Sim plug 1",
             "local_ip":"<bench-ip>:9440","plug_model":"tapo_p110"}'
   # ...repeat for plugs 2 and 3 (ports 9441/9442, or the other secondary IPs)
   ```
   Each plug create/update republishes the retained roster
   (`_publish_gateway_roster` in `backend/routers/cpo/_plugs.py`) — no
   manual MQTT step needed.
4. **Power on / reconnect the gateway.** On connect it subscribes to
   `amphive/gateways/{gw}/config` (retained) and immediately builds its
   3-plug slot table — expect idle telemetry (`status":"available"`,
   `"watts":0`) for **all three** plugs within one `telemetry_interval_ms`
   (10 s default), even before any session starts. Watch it with
   `mosquitto_sub -t 'amphive/gateways/<gw-mac>/telemetry'` or the CPO
   portal's live view.
5. **Start a session** on each plug from the driver/CPO UI (or
   `POST /api/sessions/start`). Expect: relay ON on the *correct* plug only
   (KLAP session keyed per-plug — a command for plug 2 must never actuate
   plug 1 or 3), `watts` matching that plug's `--watts` config (± jitter),
   `kwh` climbing from `0.0000`, independent per-plug billing. Starting
   sessions on plugs 1 and 2 simultaneously (leaving 3 idle) is the core
   multi-plug regression this whole tool exists to exercise.
6. **Counter-reset scenario** (exercises the flaky-reconnect path a plug
   power-cycle produces): add `--reset-counter 1@120@0.5` to the CLI (plug 1
   resets its energy counter to 0.5 kWh 120 s after the emulator starts,
   and its KLAP session is invalidated — forcing the gateway to
   re-handshake, exactly like a real power-cycle). **Caveat:** AmpHive's
   firmware computes **billed** `energy_kwh` as its **own** driver-side
   monotonic integrator from `current_power` samples (`tapo_protocol.c`) —
   it never reads a cumulative counter *from* the plug for billing. So this
   scenario does **not** by itself reproduce the backend's
   `ENERGY_COUNTER_RESET_DROP_KWH` detection path
   (`backend/services/mqtt/telemetry.py`), which triggers on the *gateway's*
   session-relative `kwh` regressing — that needs a **gateway**
   reboot/NVS-recovery gap, not a plug-side event. What this scenario *does*
   exercise faithfully: a forced KLAP re-handshake mid-session (transient
   `get_telemetry` failure → that plug's telemetry sweep is skipped for one
   cycle, per `firmware/main/main.c`'s `telemetry_task`, then resumes
   normally) — i.e. genuine plug-power-cycle resilience, not the
   billing-offset code path — **and** (fw ≥ 2.4.0) the plug's
   `today_energy`/`month_energy` counters both dropping together, which
   `tapo_plug_reconcile_idle_baseline()` reads as a full reset (never an
   `UNMETERED_CONSUMPTION` report) rather than consumption. Watch the
   gateway's serial log or MQTT `/logs` topic for the re-handshake.
7. **Offline/unmetered-consumption scenario** (fw ≥ 2.4.0 — the owner-
   reported incident this feature closes: someone manually toggles the plug
   while the gateway is fully unreachable and it goes unbilled): with the
   emulator and a real bench gateway both running and idle (no session on
   plug 1), **kill the gateway's WiFi or power it off** for at least one
   `telemetry_interval_ms` (default 10 s), then, while it's down, point
   `tools/klap_probe.py` at the emulator (`python tools/klap_probe.py
   <bench-ip>:9440 bench@example.com bench-pw`) and toggle the plug ON, wait
   a few seconds, then OFF again — standing in for a physical button / Tapo
   app session with nobody watching. Bring the gateway back up. Expect: the
   first telemetry frame after reconnect carries non-zero `today_kwh`/
   `month_kwh` deltas versus what it last saw, and — because the SAME KLAP
   session `tapo_plug_reconcile_idle_baseline()` uses to detect this survived
   the outage on the plug side (the emulator doesn't reset sessions unless
   `--reset-counter` fires) — an `{"error":"UNMETERED_CONSUMPTION",...}`
   alarm on `amphive/gateways/<gw>/alarms` with an `estimated_kwh` close to
   what `klap_probe.py`'s toggle actually drew. `--daily-reset-counter
   1@<seconds>` additionally exercises the "today rolled over mid-gap, month
   kept counting" fallback branch of the same reconciliation. No firmware or
   sim code is needed purely at the **simulator** layer to prove the
   counters behave this way — see `tools/p110_sim/tests/test_plug.py`'s
   `test_manual_toggle_during_a_polling_gap_is_reflected_on_reconnect`.
8. **Flaky-plug scenario:** add `--drop-rate 0.2` (or `--drop-rate-map
   2=0.5` for just plug 2) and confirm the gateway's telemetry for that
   plug becomes intermittent (missing sweeps) rather than the gateway
   crashing or wedging, and that it recovers cleanly once a request finally
   lands.
9. **State persistence check:** stop the emulator (Ctrl-C) mid-session,
   restart it with the same `--state-file`, and confirm the resumed energy
   counter and relay state match what was last persisted (write-through on
   every relay change and, while a plug is drawing power, on the polling
   cadence too).

## Tests

```bash
C:\Users\Sarthak\Documents\AmpHive\.venv\Scripts\python.exe -m pytest tools/p110_sim/tests -q
```

37 tests: pure crypto unit tests (`test_crypto.py`, no network), pure
plug-state-machine unit tests (`test_plug.py`, no network, clocks
monkeypatched — including the 2026-08 today/month independent-counter and
manual-toggle-during-a-gap fidelity tests), a hand-rolled-client-vs-live-server
integration suite (`test_server_klap.py` — handshake, on/off/energy, wrong
credentials → hash mismatch, missing session → 403, drop-rate, the `/app`
AES-probe-decline path, and the scheduled counter reset), and the real `tapo`
library integration suite (`test_tapo_lib.py` — see "What's verified" above).
`tools/` isn't in this repo's `ruff`/`mypy` CI scope (checked
`pyproject.toml`/`.github/workflows/ci.yml` — only `backend/` is), so these
tests don't run in CI; run them locally as above after touching this
directory.
