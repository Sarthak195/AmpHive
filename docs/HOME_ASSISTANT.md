# Home Assistant and evcc (read-only meter)

AmpHive exposes one read-only endpoint for home-energy tools:
`GET /api/me/meter`. You authenticate it with a **personal read-only API
token** that you create in the app. Use it to put live power, current, voltage
and session energy into Home Assistant dashboards, or to give evcc a charge
meter.

This integration is read-only by design. There is no switch, no start/stop, no
write scope and no MQTT discovery. Starting or stopping a charge from outside
the app would bypass the wallet hold, circuit admission and reservations (see
[SAFETY.md](../SAFETY.md)). Home Assistant can also read the same P110 through
its native Tapo integration. This endpoint adds what only AmpHive knows: the
billed session energy and the plug's AmpHive status.

## 1. Create a token

- Driver: **Account > API tokens** at `https://amphive.app/account`.
- Operator (CPO or admin): **Settings > API tokens** on the operator host.

Give the token a name (for example "Home Assistant") and press **Create
token**. The token looks like `amph_` followed by 43 characters, and it is
shown **once**. Copy it straight into your secrets file. AmpHive stores only a
SHA-256 hash of it, so a lost token cannot be shown again. Revoke it and create
a new one.

Rules:

- Up to 5 active tokens per account.
- A token is accepted **only** by `GET /api/me/meter`. Every other endpoint
  answers 401 to it.
- Each token is limited to 60 requests per minute (429 with `Retry-After`
  above that). A 30 s poll uses 2.
- Revoke a token from the same panel. Log out everywhere, a password reset and
  closing the account also revoke every token. A disabled account's tokens
  stop working at once.
- A read-only `cpo_viewer` demo login cannot create tokens.

## 2. What the meter returns

```http
GET /api/me/meter
Authorization: Bearer amph_<secret>
```

```json
{
  "generated_at": "2026-11-18T12:00:00.123456+00:00",
  "plugs": [
    {
      "plug_id": 2,
      "name": "Bay 1",
      "status": "occupied",
      "online": true,
      "power_w": 2210.4,
      "current_a": 9.62,
      "voltage_v": 229.8,
      "reading_at": "2026-11-18T11:59:58.401000+00:00",
      "session": {
        "id": 80,
        "started_at": "2026-11-18T11:00:00+00:00",
        "energy_kwh": 1.838
      }
    }
  ]
}
```

- **Which plugs.** An operator (cpo, admin or cpo_viewer) gets every plug of
  their organisation. A driver gets only the plug of their own **active**
  session, so `plugs` is an empty list when the driver is not charging.
- **`status`** is the AmpHive plug status: `available`, `occupied`, `offline`
  or `maintenance`.
- **`online`** means the plug's gateway is reachable right now.
- **`power_w`, `current_a`, `voltage_v`** come from the latest telemetry
  frame. They are `null` when there is no reading yet (for example just after a
  backend restart or a session start) or when the last reading is more than
  90 s old. `reading_at` still shows when that last reading arrived.
- **`session`** is `null` when the plug has no active session. `energy_kwh` is
  the billed energy so far. It only ever goes up during a session, which suits
  a `total_increasing` sensor.
- **Status codes:** 401 for a missing, unknown or revoked token, 429 over the
  rate limit.

Quick check from a shell:

```sh
curl -s -H "Authorization: Bearer amph_<secret>" https://amphive.app/api/me/meter
```

## 3. Home Assistant (RESTful integration)

Store the whole header value, including the word `Bearer`, in `secrets.yaml`:

```yaml
# secrets.yaml
amphive_meter_auth: "Bearer amph_<secret>"
```

One `rest:` block polls once and feeds every sensor. With `scan_interval: 30`
that is 2 requests a minute. Set `plug_id` to your plug's id (the value of
`plug_id` in the curl output above).

```yaml
# configuration.yaml
rest:
  - resource: https://amphive.app/api/me/meter
    headers:
      Authorization: !secret amphive_meter_auth
    scan_interval: 30
    timeout: 10
    sensor:
      - name: "AmpHive charger power"
        unique_id: amphive_charger_power
        unit_of_measurement: "W"
        device_class: power
        state_class: measurement
        value_template: >
          {% set p = value_json.plugs | selectattr('plug_id', 'eq', 2) | list | first | default(none) %}
          {{ p.power_w if p is not none and p.power_w is not none else 'unavailable' }}
        availability: >
          {% set p = value_json.plugs | selectattr('plug_id', 'eq', 2) | list | first | default(none) %}
          {{ p is not none and p.power_w is not none }}

      - name: "AmpHive charger current"
        unique_id: amphive_charger_current
        unit_of_measurement: "A"
        device_class: current
        state_class: measurement
        value_template: >
          {% set p = value_json.plugs | selectattr('plug_id', 'eq', 2) | list | first | default(none) %}
          {{ p.current_a if p is not none and p.current_a is not none else 'unavailable' }}
        availability: >
          {% set p = value_json.plugs | selectattr('plug_id', 'eq', 2) | list | first | default(none) %}
          {{ p is not none and p.current_a is not none }}

      - name: "AmpHive charger voltage"
        unique_id: amphive_charger_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state_class: measurement
        value_template: >
          {% set p = value_json.plugs | selectattr('plug_id', 'eq', 2) | list | first | default(none) %}
          {{ p.voltage_v if p is not none and p.voltage_v is not none else 'unavailable' }}
        availability: >
          {% set p = value_json.plugs | selectattr('plug_id', 'eq', 2) | list | first | default(none) %}
          {{ p is not none and p.voltage_v is not none }}

      - name: "AmpHive session energy"
        unique_id: amphive_session_energy
        unit_of_measurement: "kWh"
        device_class: energy
        state_class: total_increasing
        value_template: >
          {% set p = value_json.plugs | selectattr('plug_id', 'eq', 2) | list | first | default(none) %}
          {{ p.session.energy_kwh if p is not none and p.session is not none else 0 }}
```

Notes:

- The session energy sensor drops to 0 between sessions. `total_increasing`
  treats that drop as a meter reset and keeps accumulating correctly, so it can
  go in the Energy dashboard as an individual device.
- Driver accounts only see a plug while they are charging, so the power,
  current and voltage sensors show as unavailable at other times. An operator
  token sees the plug all the time.
- Do not set `scan_interval` below 2 s: the token allows 60 requests a minute.

## 4. evcc (custom meter)

evcc's `custom` meter reads each value through the `http` plugin. Put the token
in the `Authorization` header and pick the plug with `jq`. Missing values read
as 0. This fragment configures a power meter for plug 2:

```yaml
# evcc.yaml
meters:
  - name: amphive
    type: custom
    power:
      source: http
      uri: https://amphive.app/api/me/meter
      headers:
        - Authorization: Bearer amph_<secret>
      jq: '[.plugs[] | select(.plug_id == 2) | .power_w // 0] | first // 0'
      cache: 10s
      timeout: 10s
```

`energy` is deliberately left out. evcc expects a lifetime meter reading
there, and `session.energy_kwh` drops back to 0 between AmpHive sessions, so
it would corrupt evcc's own session-energy accounting. Only `power` is
required.

Reference it wherever evcc expects a meter, for example as the `meter:` of a
charger, or under `site.meters.aux` / `ext` for monitoring only. Each value is
its own request, so one value at evcc's default 30 s interval stays well under
the 60/min token limit. AmpHive is single-phase and reports one current and one
voltage. evcc's `currents`/`voltages` need three phase entries, so they are
left out here.

## 5. Security notes

- Treat the token like a password. Anyone with it can read the plugs it
  covers (power, status and active-session energy). It cannot change
  anything.
- The backend never logs tokens or `Authorization` headers. Responses are
  `Cache-Control: no-store`.
- Revoke unused tokens. The panel shows when each was last used (updated at
  most once a minute).
