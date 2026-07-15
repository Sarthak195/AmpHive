# Implementation Plan — Clean Multi-Plug Provisioning (backend-pushed plug roster)

**Status:** proposed · **Author:** planning session 2026-07-15 · **Related:** TD#20, SECURITY.md §8.5, IMPLEMENTATION_STATUS item 50

## Goal

Make multi-plug the **explicit** provisioning model. Today a gateway drives up to
`SESSION_NVS_MAX_PLUGS = 4` plugs, but the captive portal collects a single
**"Target Plug IP"** (`plug_ip`) that (a) implies one-plug-per-ESP to the
installer, (b) duplicates data the DB already owns, and (c) goes stale on DHCP
change. The gateway only learns additional plugs **lazily** from the `local_ip`
field of ON/OFF command payloads, plus a throwaway boot "provisional slot"
(`PROVISIONAL_PLUG_ID = 1`) seeded from that single IP so idle telemetry flows
before the first command.

**Target state:** the backend publishes a **retained per-gateway plug roster**
on a new `amphive/gateways/{gw}/config` topic. The gateway subscribes, builds and
reconciles its slot table proactively (add / re-IP / remove), and the captive
portal drops the plug-IP field entirely. Provisioning collects only Wi-Fi + Tapo
account + MQTT password (all other identity is MAC-derived).

## Non-goals (this plan)

- Changing per-gateway broker credentials or ACLs (the new topic is already
  inside the `pattern readwrite amphive/gateways/%u/#` grant — **no ACL change**).
- On-device network scanning / zero-touch discovery — that is the **optional
  Phase 4**, additive on top of the foundation.
- Any DB schema migration for the foundation (columns already exist).

## Hard rollout-ordering constraint (read first)

The boot provisional slot (fw 1.7.1 fix) exists *specifically* so idle telemetry
/ the liveness gate work before the first command. Phase 2 **removes** it and
relies on the retained roster instead. Therefore:

> **Backend (Phase 1) must be built, deployed to the prod VM, AND have published
> a retained roster for every live gateway BEFORE the Phase 2 firmware OTA ships.**

Because the roster is **retained**, once the backend has published it (on plug
create/update or on the gateway's `online` status), a rebooting gateway receives
it immediately on subscribe — so a new-firmware gateway is never roster-less as
long as Phase 1 shipped first. Do not reorder.

Field note: prod gateway **`1cc3abb4fb54`** is on `1.9.0-direct`; it is the OTA
target for Phase 2 verification.

---

## Phase 0 — Discovery findings (Allowed APIs & anti-patterns)

Consolidated from source reads. Treat these as the **only** APIs/paths to use;
do not invent siblings.

### Backend (Python)

- **MQTT manager accessor:** `backend/state.py:18` `mqtt_manager = None`, assigned
  in `backend/main.py:109`. Reach it as `state.mqtt_manager` and **guard for
  None** (`if state.mqtt_manager:`) — precedent `session_start.py:200`,
  `session_lifecycle.py:163`.
- **Retained publish pattern (copy this):** `backend/services/mqtt_manager.py:1367-1370`
  ```python
  self.client.publish(
      f"amphive/gateways/{gateway_id}/assign",
      json.dumps(assignments), qos=1, retain=True,
  )
  ```
  Sync, fire-and-forget (no `wait_for_publish`). This is the **only** existing
  `retain=True` publish; mirror it exactly.
- **Online / reconnect hook:** `_persist_gateway_status` at
  `backend/services/mqtt_manager.py:1049`; the `if status == "online":` block at
  `:1099` fires on **every** reconnect + retained replay (precedent
  `_republish_off_for_orphaned_plugs(gateway_id)` at `:1100`). The transition-only
  flag `became_online` is at `:1071`. Use `status == "online"` (not
  `became_online`) so a gateway that reboots and reconnects always gets a fresh
  retained roster.
- **Subscribe list:** `backend/services/mqtt_manager.py:147-153` (`_on_connect`).
- **Plug-mutating endpoints (roster republish sites):**
  - CREATE `cpo_create_plug` — `backend/routers/cpo.py:453`, `Plug(...)` at `:489`,
    `await db.commit()` at `:499`.
  - UPDATE `cpo_update_plug` — `backend/routers/cpo.py:522`, mutations `:546-581`
    (`max_current_a` at `:573-575`), `await db.commit()` at `:583`.
  - MAINTENANCE `cpo_plug_maintenance` — `backend/routers/cpo.py:608`, commit `:663`
    (status only).
  - Background discovery `_persist_plug_discovery` — `mqtt_manager.py:1303`, commit
    `:1347` (already republishes the `assign` map at `:1367`).
  - **There is NO plug DELETE endpoint** (verified). Removal reconciliation is
    therefore driven only by a plug *disappearing from the roster* — which today
    can only happen via cascade when a gateway is deleted, or (Phase 4) discovery
    churn. Build the firmware remove-path anyway (future-proof), but do not expect
    a delete route to exist.
- **`local_ip` writability:** written only on create (`cpo.py:492`) and discovery
  insert (`mqtt_manager.py:1336`). It is **NOT** in `CpoPlugUpdateRequest`
  (`schemas.py:319`) — operators currently cannot fix a plug's IP after DHCP
  drift. Phase 1 adds it.
- **Command payload keys (exact):** `action`, `max_duration_seconds`, `max_kwh`,
  `session_id` (stringified), `local_ip`, `max_current_a` — built at
  `mqtt_manager.py:1409-1420`. The roster payload reuses `plug_id`, `local_ip`,
  `name`, `max_current_a` naming for consistency.
- **Schemas:** `CpoPlugCreateRequest` `schemas.py:307`; `CpoPlugUpdateRequest`
  `schemas.py:319`; `PlugResponse` `schemas.py:116` (no `local_ip`/`max_current_a`
  exposed to drivers — keep it that way). `PlugRegisterRequest` (`schemas.py:89`)
  is **dead/unbound** — do not use.
- **Test harness:** `backend/tests/test_mqtt_manager.py` + `test_gateway_create.py`
  are **DB-free** (mock paho client + `AsyncMock`/`MagicMock` fake sessions), run
  locally, no conftest DB fixture. Publish-assert idiom (copy from
  `test_mqtt_manager.py:942`, and the retained variant at `:519`):
  ```python
  args, kwargs = mgr.client.publish.call_args
  assert args[0] == "amphive/gateways/gw-1/config"
  assert kwargs.get("retain") is True
  payload = json.loads(args[1])
  ```

### Firmware (C / ESP-IDF)

- **Config globals:** `firmware/main/main.c:30-41` (`target_plug_ip[16]` at `:35`).
- **`save_config_to_nvs` signature:** `firmware/main/main.c:325` (10 args; the
  `plug_ip` arg writes NVS key `"target_plug"` at `:335`). Single call site
  `:504-505`. Portal collects `plug_ip` via `httpd_query_key_value` at `:495`,
  HTML field at `:411`.
- **`load_config_from_nvs`:** `:260-323` (`"target_plug"` read at `:278-279`;
  MAC → `gateway_id`/`device_name`/`mqtt_username` derivation at `:299-316`).
- **`target_plug_ip` runtime consumers to remove:** the fallback in
  `slot_get_locked` at `:177`, and the boot provisional-slot pre-registration at
  `:1282-1295` (+ `PROVISIONAL_PLUG_ID` macro at `:137`).
- **Slot table & reconciliation primitives:** `plug_slot_t` at `:139-154`,
  `plugs[MAX_PLUGS]` `:156`, `plugs_mutex` `:157`; `slot_find_locked` `:164`,
  `slot_get_locked(plug_id, local_ip)` `:176` (already handles add + re-IP via
  `tapo_plug_set_ip` at `:184`, and IP-match adopt at `:197-207`). KLAP driver:
  `tapo_plug_create(plug_id, local_ip)`, `tapo_plug_set_ip`,
  `tapo_plug_reassign_id` (`firmware/main/tapo_protocol.h`).
- **MQTT connect/subscribe:** `MQTT_EVENT_CONNECTED` at `:666-693` — retained
  status publish at `:679`, `esp_mqtt_client_subscribe(mqtt_client,
  command_topic, 1)` (QoS 1) at `:684`. **Add the `.../config` subscribe here.**
- **DATA handler:** `:700-901`. Today it does **not** branch on topic (only one
  topic is subscribed); the topic string is already captured into `char
  topic[256]` at `:717` and used by `parse_plug_id_from_topic` (`:100-106`).
  512-byte payload buffer, oversize/fragmented dropped at `:709-712`, single
  `cJSON_Delete(root)` at `:899`. **Add a `strstr(topic, "/config")` branch**
  before the action dispatch.
- **cJSON idiom (copy this):** `cJSON_GetObjectItemCaseSensitive(root, key)` +
  `cJSON_IsString`/`cJSON_IsNumber` guard, then `->valuestring`/`->valuedouble`
  (`:725-747`, `:754-841`). Arrays: `cJSON_IsArray` + `cJSON_ArrayForEach`.
- **Version source of truth:** `firmware/CMakeLists.txt:5`
  `set(PROJECT_VER "1.9.0-direct")` — bump on every OTA-shipped change; read at
  runtime via `esp_app_get_description()->version`.
- **OTA / signing (do not touch, just obey):** ECDSA-signed images
  (`sdkconfig.defaults:22-26`), key `firmware/secure_boot_signing_key.pem`
  (**gitignored, on this box only — NEVER regenerate**; strands fw≥1.4.0).
  Publish via `deploy/scripts/publish_firmware.ps1` → `gs://amphive-fw/`; trigger
  `POST /api/cpo/gateways/{id}/ota`. Runbook `deploy/docs/ota_image_publishing.md`.
  Dual-OTA partitions preserve NVS across update (`partitions_ota.csv`).

### Anti-patterns to avoid

- ❌ Placing the roster topic outside `amphive/gateways/{gw}/...` (would need a new
  ACL rule; keep it inside `%u/#`).
- ❌ Using `became_online` (transition-only) for the roster republish — a plain
  reboot/reconnect replays `online` without a DB status transition; use
  `status == "online"`.
- ❌ Regenerating the OTA signing key, or shipping the `-unsigned.bin`.
- ❌ Freeing a slot whose `session_active` is true during reconciliation (never
  yank a live billed session).
- ❌ Reordering rollout (firmware before backend) — reintroduces the 1.7.0
  idle-telemetry regression.
- ❌ Adding `local_ip`/`max_current_a` to the driver-facing `PlugResponse`.

---

## Phase 1 — Backend: publish the retained plug roster

**Deploys to prod first. No migration, no ACL change.**

### 1.1 Roster payload contract (new)

Topic `amphive/gateways/{gw}/config`, **retained, QoS 1**. Payload:
```json
{
  "v": 1,
  "plugs": [
    {"plug_id": 7, "local_ip": "10.0.20.7", "name": "Bay 1", "max_current_a": 16.0},
    {"plug_id": 8, "local_ip": "10.0.20.8", "name": "Bay 2", "max_current_a": null}
  ]
}
```
`max_current_a: null` ⇒ firmware uses its `default_plug_cap_a`. An empty
`"plugs": []` is a valid roster (gateway with no plugs → free all non-active
slots).

### 1.2 Add the publisher (mirror the `assign` publish)

In `backend/services/mqtt_manager.py`:
- Add **`publish_plug_roster(self, gateway_id: str, plugs: list[dict])`** — pure
  serialize + publish, copied from the `assign` publish shape at `:1367-1370`
  (retain=True, qos=1). No DB access inside — keeps it unit-testable.
- Add **`async def _publish_roster_for_gateway(self, gateway_id)`** — loads the
  gateway's plugs via `self.db_session_factory` (`select(Plug).where(Plug.gateway_id == gateway_id)`),
  maps to the payload dicts, calls `publish_plug_roster`.

### 1.3 Wire the republish sites

- **Gateway online/reconnect:** in `_persist_gateway_status` (`:1049`), inside the
  `if status == "online":` block (`:1099`), `await self._publish_roster_for_gateway(gateway_id)`.
- **Plug CRUD (routers/cpo.py):** after each commit, load the gateway's plugs and
  call `state.mqtt_manager.publish_plug_roster(gw_id, roster)` guarded by
  `if state.mqtt_manager:` — at `cpo_create_plug` (after `:499`),
  `cpo_update_plug` (after `:583`), `cpo_plug_maintenance` (after `:663`). Factor a
  tiny helper `_roster_for_gateway(db, gateway_id) -> list[dict]` in cpo.py to
  avoid three copies.
- **Discovery path:** in `_persist_plug_discovery` (`:1347`, right after the
  existing `assign` publish), also `await self._publish_roster_for_gateway(gateway_id)`
  so the ESP-consumed roster stays in sync with agent-discovered plugs.

### 1.4 Let operators fix a plug's IP

- Add `local_ip: Optional[str] = None` to `CpoPlugUpdateRequest` (`schemas.py:319`).
- In `cpo_update_plug` (`:546-581`), apply `if req.local_ip is not None: plug.local_ip = req.local_ip` — which then triggers the roster republish from 1.3.

### 1.5 Tests (DB-free, mirror existing patterns)

Add to `backend/tests/test_mqtt_manager.py`:
- `publish_plug_roster` emits retained QoS-1 message to `.../config` with the
  expected `plugs` array (copy assert idiom from `:519`).
- `_publish_roster_for_gateway` loads plugs and calls the publisher (fake session
  returning 2 plugs → 2 entries; `max_current_a=None` passes through).
- A gateway `online` status triggers a roster publish (extend an existing
  `_persist_gateway_status` test).
Add to a cpo router test (mirror `test_gateway_create.py` router-coroutine-direct
style): create/update plug → asserts `state.mqtt_manager.publish_plug_roster`
was called (monkeypatch `state.mqtt_manager` with a `MagicMock`).

### 1.6 Verification checklist

- [ ] `pytest backend/tests/test_mqtt_manager.py -q` green (runs locally, DB-free).
- [ ] `grep -rn "amphive/gateways/%s/config\|/config" firmware/` — still absent
      (firmware untouched this phase).
- [ ] Deploy to VM (`deploy/scripts/deploy.ps1`); on a real gateway `online`,
      confirm a retained message exists:
      `mosquitto_sub -h 8.231.81.12 -p 8883 ... -t 'amphive/gateways/+/config' -v`
      (via the broker; QA creds).
- [ ] Old firmware (1.9.0) keeps working — it ignores the unsubscribed `config`
      topic; ON/OFF still carry `local_ip`. **Back-compat confirmed.**

### 1.7 Anti-pattern guards

- Roster publish must be idempotent & retained; republishing the same set is a
  no-op for the gateway.
- Do not block the request path on publish confirmation (fire-and-forget like
  `assign`).

---

## Phase 2 — Firmware: consume the roster, remove the plug-IP field

**Ships as a signed OTA AFTER Phase 1 is live in prod.**

### 2.1 Subscribe to `.../config`

In `MQTT_EVENT_CONNECTED` (`main.c:666-693`), after the command subscribe at
`:684`, add:
```c
char config_topic[128];
snprintf(config_topic, sizeof(config_topic), "amphive/gateways/%s/config", gateway_id);
esp_mqtt_client_subscribe(mqtt_client, config_topic, 1);   // QoS 1, retained delivery on subscribe
```

### 2.2 Branch DATA on topic + roster handler

In `MQTT_EVENT_DATA` (`:700`), after the `topic`/`data` are captured (`:717`),
branch **before** the action dispatch:
```c
if (strstr(topic, "/config")) { handle_plug_roster(data); break; }
```
Implement `handle_plug_roster(const char *json)`:
- `cJSON_Parse`; read `plugs` via `cJSON_GetObjectItemCaseSensitive` + `cJSON_IsArray`.
- Take `plugs_mutex`. Build a `seen[]` set of roster `plug_id`s. For each entry:
  `cJSON_ArrayForEach`, pull `plug_id` (number), `local_ip` (string),
  `max_current_a` (number or null); call `slot_get_locked(plug_id, local_ip)`
  (handles add + re-IP), then set `slot->max_current_a` (fallback
  `default_plug_cap_a` when null/≤0).
- **Reconciliation / removal:** after the loop, for each `in_use` slot whose
  `plug_id` is **not** in `seen` **and** `!session_active`, free it (mark
  `in_use=false`, destroy its `tapo_plug_t`). **Skip active-session slots** — they
  finalize on their own and get reaped on the next roster.
- `cJSON_Delete`; release mutex. Reuse the `:899` cleanup discipline.

> Confirm/introduce a `tapo_plug_destroy()` for the removal path (grep
> `tapo_protocol.h`); if none exists, add one that frees the KLAP context. If the
> effort is disproportionate, an acceptable v1 is to mark the slot free without
> destroying (small bounded leak, ≤4 slots) and note it as follow-up — but prefer
> the clean destroy.

### 2.3 Remove the captive-portal plug-IP field

- HTML: delete the `Target Plug IP` `<label>/<input name='plug_ip'>` at `main.c:411`.
- POST handler: delete `char plug[16]` (`:491`), the `httpd_query_key_value(...,"plug_ip",...)` (`:495`), and its `url_decode(plug)` (`:500`).
- `save_config_to_nvs`: drop the `plug_ip` parameter (`:325`) and the
  `nvs_set_str(my_handle, "target_plug", plug_ip)` line (`:335`); update the call
  site (`:504-505`).
- Config: remove the `target_plug_ip[16]` global (`:35`) and its load
  (`:278-279`).

### 2.4 Remove the provisional-slot machinery

- Delete `PROVISIONAL_PLUG_ID` (`:137`) and the boot pre-registration block
  (`:1282-1295`).
- In `slot_get_locked` (`:176-191`), remove the `target_plug_ip` fallback at
  `:177` — the source of a slot's IP is now the roster or the command's
  `local_ip`. Keep the "no IP known → warn + return NULL" path (`:188-190`).

### 2.5 Keep command-payload `local_ip` as a refresh/fallback

Leave the ON/OFF `local_ip` handling (`:743-747`) intact — it still re-IPs a slot
and covers the transitional window / a command that arrives before a roster.

### 2.6 Version bump

`firmware/CMakeLists.txt:5` → `set(PROJECT_VER "2.0.0-direct")` (major: the
provisioning contract changed). Update the comment if it names the prior version.

### 2.7 Build, publish, OTA, verify

- Build signed image (needs `firmware/secure_boot_signing_key.pem` on this box).
- `deploy/scripts/publish_firmware.ps1` → uploads to `gs://amphive-fw/amphive-gateway-2.0.0-direct.bin`, verifies anonymous HTTPS fetch, prints the trigger.
- Trigger OTA to `1cc3abb4fb54` (`POST /api/cpo/gateways/1cc3abb4fb54/ota`).

### 2.8 Verification checklist

- [ ] Firmware builds clean & **signed** (image 68 bytes larger than `-unsigned.bin`).
- [ ] `grep -n "target_plug\|plug_ip\|PROVISIONAL_PLUG_ID" firmware/main/main.c` →
      no matches remain.
- [ ] After OTA, serial + MQTT online status reports `2.0.0-direct`
      (`esp_app_get_description()->version`).
- [ ] On connect, the gateway receives the retained roster and logs
      `Tracking plug <id> @ <ip>` for every roster plug (idle telemetry flows for
      all, with **no** command sent).
- [ ] Re-IP test: edit a plug's `local_ip` via `PUT /api/cpo/plugs/{id}` → gateway
      logs the IP change and telemetry continues.
- [ ] Remove test: delete the plug's gateway-cascade or send an emptied roster in
      staging → non-active slot freed; an **active** session's slot survives.
- [ ] Provisioning a fresh unit no longer prompts for a plug IP; charging still
      works end-to-end once the operator adds the plug in the dashboard.

---

## Phase 3 — Documentation

Update source-of-truth docs (per CLAUDE.md: `docs/`, not scattered copies):

- **`docs/SECURITY.md` §8.5 (`:422-446`):** revise the "no on-device roster"
  invariant — there is now a **backend-pushed retained roster** on
  `amphive/gateways/{gw}/config`; still **no secrets on device**, still covered by
  the existing per-gateway ACL (`%u/#`, **no ACL change**). Note the ownership
  check is unchanged. Touch §8.1 (`:355-376`) only to note the portal no longer
  collects a plug IP.
- **`docs/IMPLEMENTATION_STATUS.md`:** append the next **numbered audit item (57)**
  after item 56 (format: `57. **[Resolved YYYY-MM-DD ...] ...** (TD#…, SEC §8.5)`);
  update firmware matrix rows `:81` (control loop / multi-plug) and `:82` (captive
  portal) with the new roster mechanism + fw `2.0.0-direct`.
- **`docs/MQTT_CONTRACT.md`:** add a `config` topic row (`:50-51` area) and the
  payload schema next to the `local_ip` roster contract (`:108-124`).
- **`docs/DATA_MODEL.md`:** in the `plugs` table (`:62-66`) **add the `unique_id`
  column** (present in `models.py:205` but currently undocumented — reconcile this
  inconsistency), and note the `config` roster is derived from `plugs`. Leave the
  `UNIQUE (gateway_id, local_ip)` note (`:253-257`) as-is unless Phase 4 lands.
- **`docs/FIRMWARE.md` (`:65, 100-113`)** and **`docs/ESP32_CONNECTION.md`
  (`:179-274`)**: rewrite the captive-portal section — provisioning collects
  Wi-Fi + Tapo + MQTT password only; the gateway learns plugs from the retained
  roster; delete the "Find the plug's IP / Wrong Target Plug IP" guidance.
- **`docs/API_REFERENCE.md` (`:180`):** note `local_ip` is now editable via
  `PUT /api/cpo/plugs/{id}`.
- **`deploy/docs/gateway_provisioning.md`** (referenced from AMPHIVE_AGENT.md:227):
  update the installer steps.

---

## Phase 4 — (OPTIONAL, additive) zero-touch discovery

Layer on top of the foundation; do not block Phases 1–3 on it. Self-heals DHCP
drift and removes the operator's manual `local_ip` entry.

- **Firmware:** on connect / periodically, subnet-scan the plug VLAN and KLAP-probe
  P110s; publish each `{unique_id, alias, model, ip}` to the **existing**
  `amphive/gateways/{gw}/discovery` topic (retained). Key slots by `unique_id`
  (extend `plug_slot_t` with a `unique_id` field); subscribe to the retained
  `amphive/gateways/{gw}/assign` map to resolve `unique_id → plug_id`.
- **Backend:** `_persist_plug_discovery` (`mqtt_manager.py:1303`) already upserts by
  `(gateway_id, unique_id)` and publishes `assign` — reuse as-is; Phase 1 already
  folds discovered plugs into the `config` roster (1.3). Consider setting
  `local_ip` on the existing-plug discovery branch (today it updates only
  name+model).
- **Migration (optional hardening):** add `UNIQUE (gateway_id, unique_id)` on
  `plugs` (currently app-logic-only; DATA_MODEL.md:256). Next Alembic revision
  after head (~`0021`).

---

## Phase 5 — Final verification

- [ ] Backend suite green locally (`pytest backend/tests/test_mqtt_manager.py backend/tests/test_gateway_create.py -q`) + the new cpo router test.
- [ ] Grep guards: no `target_plug`/`plug_ip`/`PROVISIONAL_PLUG_ID` in firmware;
      `config` topic present in both backend publish + firmware subscribe.
- [ ] Rollout order honored: backend deployed + retained roster observed on the
      broker **before** firmware OTA.
- [ ] `1cc3abb4fb54` OTA'd to `2.0.0-direct`, reports all its plugs from the
      roster, survives a re-IP, and bills a session end-to-end.
- [ ] Docs updated and internally consistent (SECURITY §8.5 ↔ MQTT_CONTRACT ↔
      DATA_MODEL, incl. the `unique_id` reconciliation).

## Open decisions (confirm before executing)

1. **Roster removal without a delete route:** there is no plug-DELETE endpoint, so
   the firmware remove-path is exercised only by gateway-cascade or Phase 4
   discovery churn. Build it now (recommended, cheap) or defer? — plan assumes
   build now.
2. **Version string:** `2.0.0-direct` (major, contract change) vs `1.10.0-direct`
   (feature). — plan assumes `2.0.0-direct`.
3. **Phase 4 scope:** ship foundation only, or commit to zero-touch now? — plan
   treats Phase 4 as optional/later.
