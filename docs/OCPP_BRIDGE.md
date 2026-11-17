# OCPP 1.6J bridge (read-only)

`tools/ocpp_bridge/` is a laptop-side tool. It makes the AmpHive plugs that a
token can see show up in a standard OCPP back office (a CSMS) as OCPP 1.6J
charge points. AmpHive itself stays on its own MQTT contract with the gateways;
the bridge only translates the meter feed into OCPP for display.

Nothing about it runs on the production VM: no migration, no new service, no
new dependency (it uses `websockets` from `backend/requirements.lock.txt` and
`httpx` from `backend/requirements-dev.txt`, both already installed by
`pip install -r backend/requirements.lock.txt -r backend/requirements-dev.txt`).

## What it does

- Polls `GET /api/me/meter` every `OCPP_POLL_SECONDS` (default 10) with a
  personal read-only token (`Authorization: Bearer amph_...`).
- For every plug in the response, opens one outbound WebSocket to
  `<OCPP_CSMS_URL>/<charge point id>` with subprotocol `ocpp1.6`. The charge
  point id is `<OCPP_CP_PREFIX><plug_id>` (default `AMPHIVE-7` for plug 7), with
  a single connector, number 1.
- Sends `BootNotification` (vendor `AmpHive`, model `Tapo P110 bridge`) and
  sends nothing else until the CSMS answers `Accepted`. On `Pending` or
  `Rejected` it retries after the returned interval. `Heartbeat` then runs at
  the interval the CSMS chose.
- Sends `StatusNotification` (errorCode `NoError`) whenever the mapped status
  changes, and once after every (re)connect.
- When a session appears: `StartTransaction` (idTag `OCPP_ID_TAG`, default
  `AMPHIVE`; `meterStart` = the virtual register in Wh; timestamp = the
  session's `started_at`). During the session: one `MeterValues` per poll.
  When the session is gone: `StopTransaction` with `meterStop` and reason `Local`.
- Reconnects with exponential backoff (1 s doubling to 60 s, reset after a
  successful boot). Any OCPP call left unanswered for 30 s drops the
  connection, which then reconnects. Only one call is outstanding at a time.

OCPP-J framing is hand-rolled: `[2,id,action,payload]`, `[3,id,payload]`,
`[4,id,code,description,details]`, with a fresh UUID per call.

## Read-only guarantees

- The only request the bridge makes to AmpHive is `GET /api/me/meter`. It
  never starts, stops or changes anything, and the token is read-only anyway.
- Every command the CSMS sends is refused and never reaches AmpHive:

| CSMS call | Bridge answer |
|-----------|---------------|
| RemoteStartTransaction, RemoteStopTransaction, ChangeAvailability, Reset, ChangeConfiguration, ClearCache, DataTransfer, TriggerMessage | `{"status": "Rejected"}` |
| UnlockConnector | `{"status": "NotSupported"}` |
| GetConfiguration | read-only keys only: HeartbeatInterval, MeterValueSampleInterval, NumberOfConnectors, MeterValuesSampledData, AuthorizeRemoteTxRequests (all `readonly: true`) |
| anything else | CallError `NotImplemented` |

- If the CSMS answers `StartTransaction` with an idTag status other than
  `Accepted`, the bridge logs a warning and keeps mirroring: it cannot (and
  must not) stop a real AmpHive session.
- The token is read from the environment, never hard-coded, never logged and
  hidden from the config's repr. The websockets logger (which prints handshake
  headers, including Basic auth, at DEBUG) is held at INFO even with `-v`.
- A 401 from the meter API stops the bridge with exit code 3 (fail closed).
- `AMPHIVE_API_URL` must be `https://` (plain `http://` only for localhost), so
  the token never travels in clear.

## Status mapping

Checked in this order, first match wins:

| AmpHive meter data | OCPP status |
|--------------------|-------------|
| `status` is `offline`, or `online` is false (gateway offline) | Unavailable |
| `status` is `maintenance` | Unavailable |
| session present and `power_w` >= 10 W | Charging |
| session present and `power_w` below 10 W or null | SuspendedEV |
| `status` is `available` | Available |
| `status` is `reserved` | Reserved |
| anything else (for example `occupied` with no visible session) | Unavailable |
| plug missing from the response (a driver token after its session) | Unavailable |

The status never decides whether a transaction is open: that depends only on
whether the plug has a session. A gateway that drops offline mid-session shows
Unavailable while the transaction and its MeterValues carry on.

## Meter values and the virtual register

A Tapo plug has no cumulative energy register, so the bridge keeps one per
charge point: register (Wh) = persisted base + the current session's billed
`energy_kwh` x 1000. `meterStart` is the base; `meterStop` is the base plus the
last energy seen; after the CSMS acknowledges the stop, the base advances by
that energy. The next session's `meterStart` equals the previous `meterStop`.

Each `MeterValues` carries `transactionId` and these sampled values (context
`Sample.Periodic`), omitting any that the meter reports as null:

| Measurand | Unit | Source |
|-----------|------|--------|
| Energy.Active.Import.Register | Wh | virtual register |
| Power.Active.Import | W | `power_w` |
| Current.Import | A | `current_a` |
| Voltage | V | `voltage_v` |

The base and the session-to-transactionId mapping live in a local JSON state
file (`OCPP_STATE_FILE`, default `~/.amphive-ocpp-bridge.json`), written
atomically. A restart therefore continues an open transaction (no second
`StartTransaction`, no double counting) and closes one whose session ended
while the bridge was down, even if that plug no longer appears in the
response. Only a successful (200) meter response can end a transaction: a
timeout, 5xx or connection error leaves everything as it was.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `AMPHIVE_METER_TOKEN` | required | personal read-only token (`amph_...`) |
| `OCPP_CSMS_URL` | required | CSMS base WebSocket URL (`ws://` or `wss://`); the charge point id is appended |
| `AMPHIVE_API_URL` | `https://amphive.app` | AmpHive origin |
| `OCPP_CP_PREFIX` | `AMPHIVE-` | charge point id prefix |
| `OCPP_POLL_SECONDS` | `10` | meter poll period |
| `OCPP_ID_TAG` | `AMPHIVE` | idTag sent in Start/StopTransaction |
| `OCPP_BASIC_PASSWORD` | unset | enables HTTP Basic auth (OCPP 1.6 security profile 1) |
| `OCPP_BASIC_USER` | the charge point id | Basic auth user name |
| `OCPP_STATE_FILE` | `~/.amphive-ocpp-bridge.json` | register and transaction state |

Run from the repo root: `python -m tools.ocpp_bridge` (add `-v` for debug
logs). Exit codes: 2 bad configuration, 3 token rejected.

## Demo on a laptop

Use an open-source CSMS running locally; nothing here touches the VM.

**SteVe** (OCPP 1.6J, <https://github.com/steve-community/steve>):

1. Follow its README to start it with `docker compose up -d` (set the web UI
   credentials first, as the README requires). The UI is on
   `http://localhost:8180/steve/manager`.
2. Charge points must be registered before they can connect: Data Management,
   Charge Points, Add, with ChargeBox ID `AMPHIVE-<plug_id>` for each plug.
   Add an OCPP tag `AMPHIVE` (or your `OCPP_ID_TAG`) under OCPP Tags.
3. Run the bridge:

   ```bash
   export AMPHIVE_METER_TOKEN=amph_...   # your personal read-only token
   export OCPP_CSMS_URL=ws://localhost:8180/steve/websocket/CentralSystemService
   python -m tools.ocpp_bridge
   ```

4. The charge point turns up as connected with its connector status; start a
   real session in AmpHive and watch the transaction and its meter values. The
   operation pages (Remote Start and friends) all come back Rejected.

**CitrineOS** (<https://github.com/citrineos/citrineos-core>): start it with
the launcher its README describes. Its websocket servers are listed in
`apps/ocpp-server/src/assets/websocket-servers.json`, each with a port,
security profile and protocol; point `OCPP_CSMS_URL` at one that speaks
`ocpp1.6` (profile 0 without auth, or profile 1 with `OCPP_BASIC_PASSWORD`).
Register or auto-commission the station as its docs describe.

## Limits

- Display only. It cannot be used to authorize, start, stop or limit charging,
  and that is deliberate. It is not a central system: AmpHive does not accept
  OCPP chargers, and a full CSMS stays out of scope.
- Granularity is the poll period (default 10 s). If a session ends while the
  bridge is down, `meterStop` uses the last energy the bridge saw, which can
  be slightly below the final billed energy.
- The register is virtual and per state file: deleting the file restarts it at
  0, and two bridges on different machines each keep their own.
- `StopTransaction` uses the time the bridge noticed the end, not the
  session's exact end time (the meter endpoint does not expose it).
- OCPP 1.6J only, security profile 0 or 1 (Basic auth). Use `wss://` when the
  CSMS is not on the same machine. No profile 2/3 client certificates, no
  OCPP 2.0.1.
- A driver token sees only the plug of its own active session, so its charge
  point appears with the session and shows Unavailable once it ends.
