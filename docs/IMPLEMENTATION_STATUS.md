# AmpHive — Implementation Status & Discrepancies

*Verified against source on 2026-07-20; the firmware OTA-status rows and the
2026-08-11 auth/MQTT entries (§0.0007) re-checked against source on 2026-08-21.
This page reconciles the aspirational
product specs ([requirements.md](../requirements.md), [features_list.md](../features_list.md))
with what the code actually does. Legend: ✅ works · 🟡 partial · 🟦 stub/mock ·
❌ not implemented.*

---

**Production state (2026-11-19, 10:53Z):** backend and frontend both `0047a2b` (PR #153, wave 3 on top of wave 2 `3e7d26c` and wave 1 `63f41b7`), alembic head `0050_charge_routines`. Wave 3 deployed 2026-11-19: backend 10:46Z, frontend 10:53Z, DB backup `amphive-20261119-104547.dump`. Rollback: backend `./rollback_backend.sh 0047a2b` (tag `pre-0047a2b`, same image as `rollback-0047a2b-m0050`), frontend `./rollback_frontend.sh 0047a2b`; see DEPLOYMENT.md "Features wave 3". The read-only demo viewer account is seeded (one `cpo_viewer` on tenant 1; credentials kept outside the repo). State before wave 3, at 08:52Z: backend and frontend both `3e7d26c`, alembic head `0047_promo_codes`. Backups: `amphive-20261119-072134.dump`, `amphive-20261119-084706.dump`. Rollback: backend `./rollback_backend.sh 3e7d26c` (tag `pre-3e7d26c`, same image as `rollback-3e7d26c-m0047`), frontend `./rollback_frontend.sh 3e7d26c`; see DEPLOYMENT.md "Features wave 2". Details in sections 0d and 0e below.

Wave 3 (0f below) is deployed on top of that earlier state (alembic `0048` to `0050`).

## 0h - 2026-11-19 wave 4 R2 features (built, not deployed)

Branch `release/2026-11-19-r2`, at `4fd4a70`, stacked on R1 (`release/2026-11-19-r1` at `8402c41`, PR #156, open), so a deploy of R2 ships R1 as well (R1 has no migration). Seven lane branches merged with `--no-ff` in this order: `feat/w4-dur-notice-state`, `feat/w4-c1-idle-fee`, `feat/w4-f8v-vehicle-email`, `feat/w4-f6r-ready-by`, `feat/w4-c9-member-price`, `feat/w4-c8-approval`, `feat/w4-c2-ocpp-csms`. Every lane wrote its migration against `0050_charge_routines`; the integrator rechained them (`ccbd096`) into one line with a single head and no branch point:

`0050_charge_routines` -> `0051_durable_notice_state` -> `0052_idle_fee` -> `0053_vehicle_profile` -> `0054_member_price_invites` -> `0055_reservation_approval` -> `0056_ocpp_charge_points`

Integration commits on top of the merges: `ccbd096` (rechain), `5fb3193` (cross-lane contracts and table classification), `63169da` (approval vs opening hours, always-push notice), `4fd4a70` (viewer route walk). No Dockerfile, compose, nginx or Caddy change, no new dependency.

| Lane | What it adds | Migration | Detail |
|---|---|---|---|
| DUR durable notice state | The F1 "looks full" notice and the C7 `nearly_done` / `power_dropped` notices are claimed once per session on `charging_sessions` (`full_notified_at`, `nearly_done_notified_at`, `power_dropped_notified_at`), so a restart no longer repeats them. The F3 offline / back-online state lives on `gateways` (`offline_notified_at`, `last_operator_alert_at`), decided under the gateway row lock; the pending "still offline" re-check is re-armed at startup (`operator_alerts.rearm_offline_rechecks()`, called from the lifespan right after `MQTTManager.start()`). Ordering race closed: an online stored first suppresses both messages, and a per-gateway lock delivers "back online" only after the offline fan-out. The per-(gateway, plug, event) safety-fault cooldown is rebuilt after a restart from the `gateway_events` audit rows (`alert_safety_fault(..., event_id=)` gets the row `services/mqtt/alarms.py` just committed). The driver of the session a cutoff finalized gets only the cutoff notice (`session_stopped`, always pushed), not also the `gateway_fault` bell. | `0051_durable_notice_state` (5 nullable timestamptz columns) | Backend only. |
| C1 idle fee | Opt-in per tariff (`idle_grace_min` of at least 15, `idle_fee_per_min`, `idle_fee_cap`; all 0 = off). Terms are copied onto the session at start. After F1 judges the car full, a session with a fee gets one always-pushed `idle_fee_notice` in place of `charge_full`. Idle time counts only on live telemetry with the relay ON below `CHARGE_FULL_IDLE_W`; a relay-ON live frame at or above it after the "looks full" signal voids the fee for that session for good (`idle_seconds = -1`). Finalize settles it once as an `idle_fee` ledger line (whole minutes past grace times rate, at most the cap, at most what the hold has left, clamped to the wallet), included in `coins_spent`, so disputes refund it and payouts and invoices include it. Auto-stop-when-full sessions never pay it. Terms also show in the start dialog and the weekly routine form; the live session screen hides them on auto-stop sessions. | `0052_idle_fee` (3 tariff columns, 6 session columns, `tx_type` value `idle_fee`) | [IDLE_FEE.md](IDLE_FEE.md). |
| F8V vehicle profile and monthly email | `GET`/`PUT /api/me/vehicle` (per-driver km per kWh, petrol price, petrol km per litre, opt-in `monthly_insights_email`). The driver dashboard, the receipt CO2 line and the email use the driver's figures; CPO series keep the defaults. The email is opt-in (default off): every `INSIGHTS_EMAIL_EVERY_N_TICKS` (default 15) reaper ticks, `services/insights_email.send_monthly_insights` mails each opted-in driver the previous UTC calendar month once, after 09:00 local (`pricing.DEFAULT_TZ`) on the 1st or later. Claim-first idempotency (`users.insights_email_month`); an empty month is claimed and skipped; a failed send restores the old month and retries on the next pass, and the third failure gives the month up. A silent no-op without `SMTP_HOST`. | `0053_vehicle_profile` (4 `users` columns) | Account > Vehicle and insights panel. |
| F6R ready-by planner | `GET /api/plugs/{id}/ready-by` (display-only plan) and `POST /api/sessions/queue/ready-by`. Picks the cheapest single contiguous window that ends by T and creates ONE scheduled queued charge through `POST /api/sessions/queue` (`not_before` = window start, `max_duration_seconds` tightened to the deadline). No PAUSED state, no split window, no re-energizing; an energy or duration limit is mandatory. `services/ready_by.py` reuses the C3 estimate (power basis, `walk_cost`, plug-hours clamp). "Ready by" choice in ChargeSetupModal. | none | Tests: `backend/tests/test_ready_by.py`, `ChargeSetupModal.test.jsx`. |
| C9 per-member price and email invites | `group_memberships.price_per_kwh_override`: a flat coins-per-kWh price that beats the plug, group and tenant tariffs and their slots for that member on that private group's plugs only. Frozen at session start (`charging_sessions.member_price_override = true`, `rate_valid_until` NULL): tariff edits and the reprice hooks skip such a session and the hold is sized at the override, including on a limits PATCH. The plug list, plug detail, tariff preview and C3 estimate show the override to that member; the public map keeps showing the tariff. A CPO can email a single-use 7-day invite to a private group (digest-only token bound to the invitee email); the invitee accepts at `/groups?invite=<token>` while signed in with a verified account whose email matches. Sends are capped per CPO (`GROUP_INVITES_PER_DAY`, 20) and platform-wide (`GROUP_INVITES_GLOBAL_PER_DAY`, 50), and only a tenant that owns a claimed hardware gateway may send (an OCPP-backed gateway does not count). Audited: `group.member_price`, `group.invite`, `group.invite_revoke`, `group.invite_accept`. | `0054_member_price_invites` (2 columns, table `group_invites`, 3 indexes) | CPO Groups > Members, Groups page. |
| C8 reservation approval and no-show fee | Per-plug `reservation_mode` `instant` or `approval`. An approval-mode booking is `pending` until the tenant CPO approves (`booked`) or declines (`declined`); `pending` blocks overlapping bookings and the per-user cap but never authorizes or blocks a start, and lapses lazily at `start_at`. Optional per-plug `no_show_fee_coins` (default 0, capped by `RESERVATION_NO_SHOW_FEE_MAX_COINS`), snapshotted at booking and charged only by the reaper sweep `reap_reservation_no_shows_once`, once per reservation (UNIQUE `ledger_transactions.reservation_id`), never when the plug or gateway may have been offline or out of service, at most the available balance. The CPO bell `reservation_needs_approval` goes through `notify()`, so quiet hours apply to it. | `0055_reservation_approval` (3 enum values, 5 columns, 1 partial index) | [RESERVATION_APPROVAL.md](RESERVATION_APPROVAL.md). |
| C2 OCPP 1.6J central system | A development and portfolio module, flag `OCPP_CSMS_ENABLED`, **OFF by default and OFF in production**, no proxy route. With the flag off neither `routers/ocpp.py` nor `services/ocpp_adapter.py` is imported (pinned by `test_ocpp_csms.py`). When on: `WS /ocpp/{charge_point_id}` (subprotocol `ocpp1.6`, HTTP Basic), Core profile only; charge points register as ordinary plugs on a backing gateway that is never marked live and are billed through the same admission, hold and ledger path. It never sends a CALL (no RemoteStart or RemoteStop). Validated only against a simulator. Hand-written, no `ocpp` library. | `0056_ocpp_charge_points` (3 tables, empty while the flag is off) | [OCPP_CSMS.md](OCPP_CSMS.md). |

**Cross-lane wiring done at integration (the code, not the lane notes, is the contract):**

- **Hold (C1 + C9).** C9 owns how the rate and `max_rate` resolve (`member_price_override()` first, then `resolve_rate_window`; `max_rate` is the override when there is one). C1 owns the hold expression in `begin_active_session`: with `max_kwh` set the hold is `min(available, energy_cost(max_kwh, max_rate) + idle_fee_cap)`, with no energy limit it is the available balance. An auto-stop-when-full session reserves no idle cap (it can never pay one). `PATCH /api/sessions/{id}/limits` resizes the hold (only for a session that has an energy limit and a non-NULL hold) at the session's own frozen override rate and keeps the session's copied idle cap in the sum, floored at the cost already accrued. The OCPP StartTransaction runs the same `begin_active_session`, so it gets the override and the idle hold too (pinned in `test_ocpp_csms.py`).
- **Member price in ready-by (F6R + C9).** `plan_ready_by` prices a member override as one flat rate for the whole charge, exactly as the C3 estimate and the start path do; the idle-fee terms still come from the tariff, and `GET /api/plugs/{id}/ready-by` returns them as `idle_fee`.
- **Idle terms on the member preview (C1 + C9).** `GET /api/plugs/{id}/tariff-preview` keeps `idle_fee` for a member-priced plug (the session still snapshots the tariff's terms) next to the flat price, an empty `slots` list and no cheapest window.
- **New user tables classified (C9, C2 with closure and export).** `group_invites` is operator-side and retained: on closure the address is rewritten to the tombstone address and a still-pending invite is revoked, in the same commit; it is not in the data export. `ocpp_id_tags` is exported (key `ocpp_id_tags`: id, operator tenant, created_at; never a tag) and hard-deleted on closure. Closure also cancels `pending` reservations, not only `booked`. Pinned by `_USER_TABLES` in `test_account_closure.py`. The four F8V `users` columns are exported (`vehicle_profile`, `monthly_insights_email`, `insights_email_month` in the `account` block) and reset on closure (with `insights_email_failures`), and each `group_memberships` entry carries the C9 `price_per_kwh_override`; every `users` column is classified in `_USER_COLUMNS` (cross-review fix).
- **Approval obeys current opening hours (C8 + C4).** Approving a `pending` request re-checks the plug's opening hours for the whole slot (409, the request stays `pending`, decline it instead); declining never checks hours. Narrowing hours still does not cancel existing bookings (see TODO). Since the cross review, approval also refuses (409, still `pending`) a plug in `MAINTENANCE` and a requester who has since lost access to the plug's private group, as a direct booking would.
- **Cross-review fixes (C8, C1, C9 + C2).** The no-show sweep skips a holder who could not have started: account disabled or closed, or no longer a member of the plug's private group (removed or left). `/refunds` section 4 now describes the no-show fee and how to contest it (support email, then an admin balance adjustment; there is no self-service refund). The receipt and the Activity session detail show the idle fee as its own "Idle fee (included)" line. An idle fee is voided (`idle_seconds = -1`) when `notify()` could not record its warning. A Gateway row that backs an OCPP charge point no longer satisfies the invite rule "the tenant owns a gateway".
- **Booking notices (C8 + C7).** `reservation_approved` is in `ALWAYS_PUSH_TYPES` (a booking change); `reservation_declined` is sent at severity `warning`, which always pushes; `reservation_no_show_fee` and `idle_fee_notice` are always pushed too. `test_notification_prefs.py` pins the wave-4 money and booking notices against every toggle off plus quiet hours.
- **Viewer route walk (F10).** The every-write-route walk now also pins `PUT /api/me/vehicle`, `POST /api/sessions/queue/ready-by`, `PUT /api/cpo/groups/{group_id}/members/{user_id}/price`, `POST` and `DELETE` of the group invites, `POST /api/groups/invites/accept`, `POST /api/cpo/reservations/{id}/approve` and `/decline`, `PUT /api/cpo/plugs/{id}` and `POST`/`PUT /api/cpo/tariffs` as 403 `read_only_demo`. The flag-gated OCPP write routes are checked by mounting the OCPP router alone (`test_viewer_gets_403_on_every_ocpp_write_route`).

**Known limits** (each is also an open item in [TODO.md](TODO.md)):

- DUR: the detector's idle timers are still in memory (a restart delays detection by up to `CHARGE_FULL_MINUTES`); a crash between the claim and the send loses that one notice (a DB error on the claim fails open and sends); delivery ordering relies on the single backend process; an offline still inside the debounce window at a restart, with no earlier alert on record, is never reported; an offline alert outstanding at the 0051 deploy gets no "back online" (the columns start NULL); after a restart the fault cooldown counts from the newest earlier row of that fault (even one whose alert was suppressed), so a constantly recurring fault can wait up to about 2x `OPERATOR_ALERT_COOLDOWN_SEC` for its next page.
- C1: a car-side pause longer than the grace (scheduled charging, battery conditioning) is billed as idle (disputable); a car left after the session ends is never charged; the void needs a live frame; the looks-full banner can still show the "unplug within N min" hint on a voided session; a routine or scheduled run snapshots the plug's idle terms when it starts, so a fee added after the routine was saved is not shown again; SessionMonitor shows the plug's current terms, not the session's copy; an unlimited session that spends its whole hold on energy collects no fee; the GST invoice has one energy line while its total includes the idle fee; a crash between the notice claim and `notify()` loses the warning while the fee still accrues (a `notify()` that returns a failure voids the fee); `frontend/src/utils/legal.js` `LAST_UPDATED` was not bumped.
- F8V: months stay UTC buckets (the 09:00 trigger is local, the two agree around the 1st for IST); a crash between the claim and the SMTP send loses that month's mail (never duplicates); the receipt makes one extra `GET /api/me/vehicle`.
- F6R: the plan assumes constant power, so the run stops at the deadline with the energy target short when the car draws less. The finish can slip past T: the limit counts from the actual start, and a scheduled row may start up to `SCHEDULE_START_GRACE_MIN` (30 min) after `not_before` plus one reaper tick, so the run can end about 31 min after T. The planner does not avoid other drivers' reservations or schedules (the scheduled path refuses with the usual 409). T is capped at 24 h ahead.
- C9: `group_invites.group_id` is ON DELETE CASCADE, so deleting a group resets both send counts; the caps are check-then-insert and per CPO user; invites are not in the data export; addresses outside the masker's character class (non-ASCII local part, quotes) cannot be invited by email (the access code still works); an override set while the group was private keeps applying if the group later becomes public (clearing is always allowed); the Signup page's Google button lands on Home, not on the invite.
- C8: the no-show fee is a driver ledger debit only (not in CPO earnings, payouts or GST invoices); the driver copy hardcodes 15 minutes; "offline in the window" is inferred from current gateway liveness and events (any unknown signal means skip, the safe direction); a holder who schedules a charge inside their own booking is not fined, but lazy expiry still frees the slot at `start_at` plus grace; a start refused by admission or a failed ON publish still counts as a no-show unless a plug or gateway check fires; the opening-hours skip checks the hours at sweep time; the fee has no self-service refund (support plus an admin balance adjustment); removing a member does not cancel their bookings on the group's plugs or tell them (the fee is skipped); a `pending` request that lapses unanswered expires with no notice to the driver.
- C2: simulator-only; no RemoteStart or RemoteStop, so a backend-side stop bills and frees the session but cannot switch the charger off (energy delivered after that is not billed); a late StopTransaction on a session the backend already closed is logged with the unbilled kWh, not billed; no default energy cap on an OCPP hold; a charger that stays connected but never sends StopTransaction keeps the session open until the driver's Stop, a time limit or a disconnect; admission is copied from the walk-up route into `_admit_and_begin` (a new walk-up gate must be added there too); the new modules are not in the mypy files list. Full list in [OCPP_CSMS.md](OCPP_CSMS.md).

Migration line: `0051..0056`; 4 new tables (`group_invites`, `ocpp_charge_points`, `ocpp_id_tags`, `ocpp_transactions`, so 40 tables at head); 4 new enum values (`tx_type`: `idle_fee`, `reservation_no_show_fee`; `reservation_status`: `pending`, `declined`), added in the one upgrade transaction, which PG 12+ allows because no later revision uses a new value. Schema detail in [DATA_MODEL.md](DATA_MODEL.md).

CI (local): pending

**Deploy (not done):** a migration deploy like wave 3 (DB backup first, rollback image = the live code plus the six new migration files tagged `pre-<new sha>`, backend and frontend together). The new optional env vars all default safely and are not in the compose files, so they run at their defaults; OCPP stays off. Details in [DEPLOYMENT.md](DEPLOYMENT.md) "Wave 4 R2".

## 0g - 2026-11-19 wave 4 R1 fixes (built, not deployed)

Branch `release/2026-11-19-r1`, cut from `main` at `0a28b2d` (identical code to production `0047a2b`). Four fix lanes merged with `--no-ff`, in this order, with no conflicts: `fix/w4-fx1-privacy-export`, `fix/w4-fx2-hours`, `fix/w4-fx3-notices`, `fix/w4-fx4-tests-nits`. No migration (alembic head stays `0050_charge_routines`), no env vars, no new dependencies, no nginx, Dockerfile, compose or Caddy change.

- **FX1 privacy and export (backend only).** `GET /api/auth/me/export` also lists charge routines, promo redemptions, plug reviews and photos the user wrote, queued charges and capacity requests (additive; `export_format` stays `amphive.user-data-export.v1`). Account closure also deletes the user's plug reviews and plug photos (stored image objects deleted best-effort in a background task after the response; rejected photos are skipped because their object was deleted at rejection) and clears `users.notification_prefs`. Public review authors show the first word of a real name, else "AmpHive driver"; a Google signup without a name claim stores an empty `full_name`. A `cpo_viewer` can read an existing invoice of its own tenant (never issues one; the HTML Bill To name is initials and the email masked), and only ever sees approved plug photos.
- **FX2 opening hours.** An outage-queue row that expires while the plug is closed says "The charger was closed until your request expired." Editing a plug's hours re-checks its enabled routines (`services/routines.py disable_closed_routines`, the same `closed_weekdays` rule as routine create): a routine whose start is now closed on a selected day is turned off in the same request, its pending row cancelled, and its owner gets one `routine_disabled`. The CPO Chargers `hoursPatch` compares windows field by field, so a JSONB key order no longer re-sends unchanged hours.
- **FX3 start notices and push toggles.** Each start sends exactly one start notice: direct `session_started`, queued or scheduled `queued_charge_started`, routine `routine_started` (or `queued_charge_started` if the routine was deleted or unlinked before settle; in-memory fallback, lost only by a restart inside that tick). A queued, scheduled or routine auto-start's single start notice is sent (or stashed for the routine settle pass) in a `finally` around the `started_session_id` link commit, so a failure of that commit after the plug went live still notifies the driver. `gate_notified` re-arms on the next materialize pass that finds the queued-charging gate on, whether or not it queues a row. New suppressible push toggles: `rate_changed`, `plug_available`, `reservation_booked`, `reservation_started`, `queued` (all on by default); the allowlist and the toggles stay disjoint.
- **FX4 tests and nits.** C10: the firmware check passes with the wording "newer than the newest release (X)" when the gateway is ahead of every active release; the checklist component ignores any response that is not from the latest request (sequence guard). F11: the CPO Chargers page has a Share network status button that shows a printable QR for `<driver origin>/map` (`qrcode.react`, already a dependency). New tests for the estimate route's validation, the estimate history query (DB-gated), the routine row lock and the unique-index savepoint branch (DB-gated).

Integration on the merged tree:

- **FX2 x FX3:** a routine an hours edit turns off sends nothing more after its one start notice: its cancelled or expired row is silent at settle (a disabled routine's failure never re-sends `routine_disabled`), and an occurrence that already started keeps exactly one `routine_started`, with no reaper fallback on top. Pinned by `test_hours_edit_disabled_routine_sends_nothing_after_its_one_start_notice`. No code change was needed.
- **FX3 toggles x existing always-push types:** none of the five new toggles is a type FX2 or existing code relies on being pushed (`routine_disabled`, `routine_skipped`, `queued_charge_started`, `queued_charge_expired`, `queued_charge_failed`, `reservation_cancelled`, `reservation_ahead` have no toggle); `test_allowlist_names_the_safety_billing_and_routine_types` pins it.
- **Viewer:** the every-write-route walk asserts 403 `read_only_demo` on 74 write routes on the merged tree. The changed invoice GET returns only the tenant's own existing invoice to a viewer (JSON carries no name or email; the HTML Bill To goes through `redact_name_for` and `redact_for`).
- **Known limit found at integration:** a CPO hours edit (plug row, then routine rows) and a materialize tick (routine row, then plug row FOR UPDATE) take locks in opposite order; on an exact collision Postgres aborts one as a deadlock (by analysis: default autoflush writes the plug UPDATE before the routine query; not reproduced). Rare, recorded in docs/TODO.md.

CI commands on the merged tree (local, no database): backend 1691 passed / 251 DB-gated skips (they run only in CI), tools 107 passed, frontend 961 passed (99 files), ruff, mypy and eslint clean, build OK.

**Deploy (not done):** thin backend layer and frontend rebuild via the usual path, backend first (an old backend answers 422 to a prefs PUT carrying a new toggle key, since `PushToggles` is `extra=forbid`). The backend runs a single uvicorn process, so there is one reaper and the in-memory start-notice fallback cannot be split across workers. Rollback: the previous images; an older backend reading prefs saved with the new keys falls back to defaults (quiet hours off) until the user saves again, with no data lost.

## 0f - 2026-11-19 features wave 3 (deployed 2026-11-19)

Branch `release/2026-11-17-w3`, cut from `release/2026-11-17-w2` at `590293e` (waves 1 and 2 plus their review fixes), so a deploy of it ships all three waves. Five branches merged with `--no-ff` in this order: `feat/c10-selftest`, `feat/c3-estimate`, `feat/c7-notif-prefs`, `feat/c4-plug-hours`, `feat/c5-routines`. One conflict (`frontend/src/pages/Account.jsx`: the C7 Push preferences panel and the C5 Charge routines card both kept). Migrations rechained into one line: `0047_promo_codes` -> `0048_notification_prefs` -> `0049_plug_hours` -> `0050_charge_routines` (single head, no branch point, checked offline with Alembic's `ScriptDirectory`).

Integration fixes on the merged tree:

- **C5 + C7:** a routine occurrence sends exactly one start notice, the mandatory `routine_started` from `settle_routines`. The queued-start sweep looks up whether a routine owns the row and then sends neither `queued_charge_started` nor C7's `session_started` (`begin_active_session(announce_start=False)`). `routine_started` and `routine_disabled` are in C7's non-suppressible allowlist. If the routine is deleted or unlinked between the sweep's start and settle, the reaper falls back to `queued_charge_started`, so every auto-start sends exactly one start notice (wave 4).
- **C5 + C4:** a routine row on a closed plug is an ordinary scheduled row: it stays WAITING (no start, no notice) until the plug opens or its TTL lapses, and an EXPIRED row counts as one routine failure. The sweep looks at it once per 60 s tick, and the routine's date stamp stops it from queueing the occurrence again. Pinned by `test_closed_plug_occurrence_waits_then_expiry_counts_one_failure`.
- **C3 + C4:** the estimate applies the start path's clamp (`clamp_start_duration`) at the estimated start, so `duration_min`, `energy_kwh`, `finish_at` and the cost end at the closing time and at `max_session_minutes`. A plug that is closed at the estimated start answers 409 with the same detail as the start (the modal then hides the estimate line).
- **C7 + F1:** once F1 has judged the session fully charged (that frame or earlier), neither `nearly_done` nor `power_dropped` is sent for it (`ChargeFullDetector.has_fired`). `power_dropped` already never fired on a drop to idle, which is F1's territory.
- **C4 import cycle:** `routers/plugs.py` imported `routers.cpo._common` at module top, and `routers.cpo` imports `routers/plugs.py`, so importing the plugs router first (C3's tests) failed. The import now sits inside `_hours_fields`.
- **Viewer:** the C10 selftest check `label` (it carries the plug name) now also goes through `redact_for` for a `cpo_viewer`. The every-write-route walk asserts 403 `read_only_demo` on 74 write routes, including the new `PUT /api/me/notification-prefs` and `POST/PATCH/DELETE /api/me/routines*` and the changed `PUT /api/cpo/plugs/{id}`, `PATCH /api/sessions/{id}/limits` and `POST /api/reservations` (pinned by `test_wave3_write_routes_are_in_the_viewer_walk`).

| Change | What changed | Deploy notes |
|---|---|---|
| C10 gateway commissioning checklist | `GET /api/cpo/gateways/{id}/selftest` plus a "Commissioning checklist" with Re-check at the top of the CPO gateway diagnostics drawer (CpoGateways Logs button; the drawer is now titled Diagnostics). Read-only, computed from existing rows and events (`services/gateway_selftest.py`). The Wi-Fi signal is the value reported at the last boot, not live (the firmware has no live RSSI report). | None (no migration). |
| C3 pre-start estimate | Display only: `GET /api/plugs/{id}/estimate` (`services/estimate.py`). The power basis is the caller's average over their last completed session on the plug (30 days, at least 5 min, capped at rated power), else `effective_plug_cap` x 230 V. Cost walks tariff windows with `services/pricing.py` slot logic and billing's `close_out_segment` / `session_cost`, so it matches billing across a tariff change; no fee or GST is added because tariffs are GST-inclusive. Respects the C4 plug-hours clamp. ChargeSetupModal shows a debounced, labelled Estimate line. The vehicle garage from the original spec was cut (one car; max kW never binds on a 16 A plug). Tests: `backend/tests/test_charge_estimate.py`, `ChargeSetupModal.test.jsx`. | None (no migration). |
| C7 notification preferences and quiet hours | Per-user push toggles for `session_started`, `nearly_done`, `power_dropped`, `rate_changed`, `plug_available`, `reservation_booked`, `reservation_started` and `queued` (all on by default; the last five added in wave 4), plus quiet hours (window `[start, end)`, may cross midnight) in the account's tenant zone; drivers have no tenant, so theirs use the default zone (Asia/Kolkata). The gate is inside `notify()`: the feed row and the live socket event are always delivered, only Web Push is gated. The non-suppressible allowlist (severity critical or warning; `session_stopped`, `topup_credited`, `topup_refunded`, `promo_redeemed`, `orphan_off`, `charger_offline`, `unmetered_consumption`, `gateway_offline`, `gateway_online`, `gateway_fault`, `charge_full`, `routine_started`, `routine_disabled`, `queued_charge_started`, and, wave 4 R2 (built, not deployed), `idle_fee_notice`, `reservation_approved` and `reservation_no_show_fee`) always pushes, even in quiet hours. New notices: `session_started` (once, from `begin_active_session` after the ON publish succeeds; direct starts only: a queued or scheduled auto-start sends only `queued_charge_started` and a routine occurrence only `routine_started`), `nearly_done` (once at 90% of `max_kwh`), `power_dropped` (once: below 50% of the session peak but at or above `CHARGE_FULL_IDLE_W` for 3 continuous minutes, after peaking at `CHARGE_FULL_ACTIVE_W` or more). Neither of the last two is sent on a frame that auto-stops the session or after F1 judged the car full. Dedupe is in memory and resets on restart. Account page "Push preferences" panel. See [NOTIFICATION_PREFS.md](NOTIFICATION_PREFS.md). | Migration `0048_notification_prefs`. |
| C4 plug opening hours, max session length, amenities, access note | Weekly hours in tenant local time (NULL = 24x7) (`services/plug_hours.py`). A closed plug refuses new starts (409, in `begin_active_session` under the plug lock, a typed `PlugClosedError`) and new bookings (409). Queued and scheduled rows on a closed plug stay WAITING until it opens or the row's TTL expires; the sweep catches `PlugClosedError` and leaves the row WAITING without a notice, so a sweep that passes its own hours check just before closing time never fails the row. A start's duration limit is clamped to end by the closing time and by `max_session_minutes`; a raised limit (`PATCH /api/sessions/{id}/limits`) is capped the same way but never below the session's current limit. Running sessions are never stopped at closing time. CPO edits it in Chargers > Edit > Hours and access. Drivers see Open now/Closed with the change time, amenities and the note on the plug card, the Home lookup and the reviews modal, plus an Any hours / Open now / Open 24x7 filter. A closed plug with `schedule_available` offers "Schedule" on the card and "Schedule a start" in the Home lookup; both open the charge setup on "At a time" with no "Now" option. See [PLUG_HOURS.md](PLUG_HOURS.md). | Migration `0049_plug_hours`. |
| C5 charge routines | Recurring weekly charges, materialized by the session reaper as ordinary scheduled queued charges, so there is no second start path. UI: the Charge routines card on `/account`. Inert unless queued or scheduled charging is enabled for the plug. When it is off for the plug, each occurrence is stamped and skipped: the routine stays enabled, the driver gets one `routine_skipped` ("Charge routine paused") notice per outage, and the routine resumes by itself when the gate is back on. `routine_disabled` means only three consecutive failed occurrences. `POST`, and a `PATCH` that turns a routine back on, still answer 409 `queue_disabled` while the gate is off. On a plug with opening hours (C4), `POST` and a `PATCH` that changes the days or start time or turns the routine on answer 409 `plug_closed` when the plug is closed at that time on any selected day. Creating a routine, re-enabling it, or editing its weekdays or start time skips an occurrence whose start already passed, so it first runs at its next occurrence. See [CHARGE_ROUTINES.md](CHARGE_ROUTINES.md). | Migration `0050_charge_routines`. |

CI commands on the merged tree after the 2026-11-19 fix round (local, no database): backend 1633 passed / 243 DB-gated skips (they run only in CI), tools 106 passed, frontend 956 passed (98 files), ruff, mypy and eslint clean, build OK.

**Deployed 2026-11-19** (PR #153, merge `0047a2b`; also the OCPP bridge Python 3.11 cancel fix and the CI timeouts) by the manual migration path: backend 10:46Z (alembic `0047_promo_codes` to `0050_charge_routines` at boot, healthy in about 12 s; the `charge_routines` table and the 5 new columns present; a 150 s soak covering the 60 s routine reaper showed 0 tracebacks, errors or warnings), frontend 10:53Z (thin kit). DB backup before it: `amphive-20261119-104547.dump`. Rollback: backend `./rollback_backend.sh 0047a2b` (tag `pre-0047a2b`, same image as `rollback-0047a2b-m0050`: the `3e7d26c` code plus the three 0048-0050 migration files, so it boots on 0050); `live-3e7d26c`, `pre-3e7d26c` and every older `pre-*` image can NOT boot on 0050. Frontend `./rollback_frontend.sh 0047a2b` (`3e7d26c` dist). Logged-in read-only smoke in production passed: notification prefs, routines, an estimate on plug 1, and the commissioning checklist (all six checks pass on the real gateway). Demo viewer seeded the same day (see [CPO_VIEWER.md](CPO_VIEWER.md)).

Deploy plan as written: a migration deploy (`0048` to `0050` in one boot on top of prod `3e7d26c` at `0047`; waves 1 and 2 were deployed 2026-11-19), so the deploy kit's thin-layer script (which refuses `backend/migrations/versions/*`) does not apply as is. Same path as wave 2: DB backup, rollback image (live image plus only the three new migration files) tagged first, backend layer carrying the migration files (applied at boot by `init_db`), then the frontend dist-only thin layer; backend and frontend ship together. No new env var and no manual VM step. Details in [DEPLOYMENT.md](DEPLOYMENT.md) "Features wave 3".

---

## 0e - 2026-11-17 features wave 2 (built, deployed 2026-11-19)

**Deployed 2026-11-19** (PR #152, merge `3e7d26c`, includes the two `cpo_viewer` masking rounds) by the manual migration path: backend 08:48Z (alembic `0043_email_ci_unique` to `0047_promo_codes` at boot, healthy in about 15 s, 0 tracebacks), frontend 08:52Z (thin kit). Backups: `amphive-20261119-072134.dump` (before wave 1) and `amphive-20261119-084706.dump` (before wave 2). The read-only demo viewer is deployed but not seeded (owner decision). F11: `/api/status` reports `no_public_chargers` in production because the owner's plug is not public; expected.

Branch `release/2026-11-17-w2`, cut from `release/2026-11-17-w1` at `42f6c85` (wave 1 plus its fix round), so it carries wave 1 too. Four branches merged with `--no-ff` in this order: `feat/f1-charge-complete`, `feat/f7-read-token` (stacked on `feat/f10-viewer-role`), `feat/f12-promo-codes`, `feat/ocpp-bridge`. One conflict (`backend/services/rate_limit.py`, F7 and F12 each added limiters; both kept). Migrations rechained into one line: `0043_email_ci_unique` -> `0044_session_auto_stop_full` -> `0045_cpo_viewer_role` -> `0046_api_tokens` -> `0047_promo_codes` (single head, no branch point, checked offline with Alembic's `ScriptDirectory`). Integration fixes: promo write buttons carry `data-write` so the read-only viewer does not see them, and `GET /api/cpo/promos` masks the issuer email and note for a viewer like every other `/api/cpo/*` read. The F10 every-write-route test now covers the F1, F7 and F12 write routes (pinned by `test_wave2_write_routes_are_in_the_viewer_walk`). CI commands on the merged tree: backend 1436 passed / 235 DB-gated skips (they run only in CI), tools 100 passed, frontend 934 passed (95 files), ruff, mypy and eslint clean, build OK. One review fix round followed (F1 stop retry, F7 revocation on an admin disable or role change and on the legacy logout, F12 sub-cent 400, viewer gateway log text hidden, four OCPP bridge fixes, a `pg_enum` drift check after `alembic upgrade head` and a meter-to-bridge contract test). CI on Postgres at `648c490`: backend 1678 passed / 1 skipped, tools 106 passed, frontend 934 passed, coverage 95%.

| Change | What changed | Deploy notes |
|---|---|---|
| F1 charge-complete detection | The telemetry path notices when the car stops drawing power (peak >= `CHARGE_FULL_ACTIVE_W` once, then < `CHARGE_FULL_IDLE_W` for `CHARGE_FULL_MINUTES` after a `CHARGE_FULL_GRACE_MIN` startup grace, real live samples only), sends one "Your car looks fully charged" notification (bell and push), and, only when the driver ticked "Stop automatically when the car is full", stops the session via `finalize_charging_session` with reason `auto-stopped: charge complete` (the same call as the driver stop); a failed stop is retried on later idle frames, at most once per 60 s. Detector timing is in memory (a restart delays detection); since migration 0051 (wave 4 R2, built, not deployed) the notice itself is claimed once per session on `charging_sessions.full_notified_at`, which is also the durable "judged full" marker the C1 idle fee reads. See [CHARGE_COMPLETE.md](CHARGE_COMPLETE.md). | Migration `0044_session_auto_stop_full`. Optional `CHARGE_FULL_*` env vars. |
| F10 read-only CPO viewer | New role `cpo_viewer` opens one tenant's CPO console read-only: every non-GET/HEAD/OPTIONS request returns 403 `read_only_demo` (`POST /api/auth/logout` exempt). Emails and phone numbers are masked and private group access codes are hidden in `/api/cpo/*` responses and the sessions CSV. Gateway log text is hidden from the viewer (firmware lines can carry the Wi-Fi name). The frontend shows a "Read-only demo" banner and hides write controls (`[data-write]`). A demo login is created with `python -m backend.seed_cpo_viewer`. See [CPO_VIEWER.md](CPO_VIEWER.md). | Migration `0045_cpo_viewer_role` (enum value, downgrade is a no-op). Optional seed command after deploy. |
| F7 personal read-only API token + `GET /api/me/meter` | Hashed read-only tokens (max 5 per user, 60 requests/min each), revoked on log out everywhere, the legacy log out, password reset, Google takeover, an admin disable or role change, and account closure, and listed in the data export. Panel in Account and CPO Settings. Home Assistant REST sensor and evcc custom meter guide in [HOME_ASSISTANT.md](HOME_ASSISTANT.md). No MQTT discovery, switch or write scope by design. | Migration `0046_api_tokens`. Optional `API_TOKEN_RATE_LIMIT`. |
| F12 promo codes | CPO issues, lists and disables codes on the Earnings page; drivers redeem on the Wallet page. Funded from the tenant's offline top-up pool, checked at redemption; no pool reservation, no referral credit. The `cpo_topup` ledger label is now "Credit added by operator" (cash and promo credits share the type). | Migration `0047_promo_codes`. Optional `PROMO_REDEEM_RATE_LIMIT`, `PROMO_REDEEM_ACCOUNT_RATE_LIMIT`. |
| OCPP 1.6J bridge | `tools/ocpp_bridge`, laptop-side, read-only: presents each plug as a virtual OCPP charge point (status, transactions, meter values) to any OCPP central system. Built and unit-tested against a fake CSMS, and pinned to the real `meter_plug` output by `backend/tests/test_meter_ocpp_contract.py`; not yet run against a live `GET /api/me/meter` or a real CSMS. Every CSMS command is refused. See [OCPP_BRIDGE.md](OCPP_BRIDGE.md). | Nothing to deploy. Needs F7 live first. |

Deploy: this is a migration deploy (0043 to 0047 in one boot), so the deploy kit's thin-layer script (which refuses `backend/migrations/versions/*`) does not apply as is. Use the migration path the 0c batch used for 0043: DB backup, rollback image tagged first, backend layer carrying the four migration files (applied at boot by `init_db`), then the frontend dist-only thin layer; backend and frontend ship together. No manual VM step is required. Details in [DEPLOYMENT.md](DEPLOYMENT.md) "Features wave 2".

---

## 0d - 2026-11-17 features wave 1 (built, deployed 2026-11-19)

**Deployed 2026-11-19** (PR #151, merge `63f41b7`) with the thin kit: backend 07:24Z, frontend 07:32Z, no migration. F5 host cron installed the same day (see DEPLOYMENT.md).

Seven feature branches merged with `--no-ff` onto `38b887a` on `release/2026-11-17-w1` (F8 carries F2). No merge conflicts; the shared files (`backend/main.py`, `frontend/src/App.jsx`, `backend/tests/test_driver_gap_endpoints.py`) merged cleanly with every route and nav entry kept. Backend suite 1236 passed / 222 DB-gated skips, tools 79 passed, frontend 898 passed (91 files), ruff, mypy and eslint clean, build OK. No migration (alembic stays at 0043), no nginx, compose, Dockerfile or dependency change. The `3eff918` CPO dashboard fix it carries is already live: PR #150 merged as `a27841d` and its backend was deployed on 2026-11-17 17:44Z as a thin layer (backup `amphive-20261117-174047.dump`, rollback `./rollback_backend.sh a27841d`), so prod was backend `a27841d`, frontend `ac00d46`, alembic `0043` at that time.

| Change | What changed | Deploy notes |
|---|---|---|
| F2 per-session power curve | `GET /api/sessions/{id}/telemetry` plus the `SessionPowerCurve` inline-SVG component on `SessionReceipt`, `SessionMonitor` (live append, no refetch) and the CPO session detail modal; marks the kWh limit when set. Readings are selected by plug and session window and bucketed in SQL to about 120 points (existing `idx_telemetry_plug_recorded`). No recharts on driver pages. | Backend and frontend thin layers. |
| F8 insights | Driver Dashboard lazy Insights card (12-month energy bars, inline SVG, plus all-time estimated CO2 avoided and petrol-equivalent), CPO Dashboard CO2-avoided tile, SessionReceipt CO2 line. All figures are labelled estimates and show the factors (`services/insights.py`). Months are UTC (a driver has no tenant timezone). | Backend and frontend thin layers. Five optional env vars (see API_REFERENCE env table); defaults apply. |
| F3 operator alerts | `services/operator_alerts.py`. After the `GATEWAY_OFFLINE_NOTIFY_DELAY_SEC` debounce, a gateway still offline bells and pushes the tenant's CPOs and all admins (any session state), at most once per gateway per `GATEWAY_OFFLINE_ALERT_COOLDOWN_SEC` (default 10800 s), with a "Gateway back online" follow-up only if an offline alert was sent. If the cooldown suppresses an offline alert while the last message was "Gateway back online", one re-check fires at cooldown expiry and sends "Gateway still offline" if the gateway is still OFFLINE, so a real outage after a flap is always reported. The operator alert fires before the driver "Charger connection lost" notice, and users with an ACTIVE session on that gateway are excluded from it (no double bell for an owner who charges from a CPO or admin account). `UNAUTHORIZED_ON`, `THERMAL_CUTOFF`, `OVERCURRENT_CUTOFF` and `OTA_FAILED` bell the tenant's CPOs (type `gateway_fault`), deduplicated per (gateway, plug, event) per `OPERATOR_ALERT_COOLDOWN_SEC` (default 3600 s). Optional plain-text email to `OPERATOR_ALERT_EMAIL`. Kill switch `OPERATOR_ALERTS=off`. Known limits: it cannot alert when the app itself is down or evicted (see the F5 host cron). **Updated by wave 4 R2 (DUR, migration 0051, built, not deployed):** the offline state (`gateways.offline_notified_at`, `last_operator_alert_at`) is durable and only counts while the gateway has not been seen since, the pending re-check is re-armed at startup, the per-(gateway, plug, event) fault cooldown is rebuilt from the `gateway_events` rows after a restart (residual: a recurring fault can wait up to about 2x the cooldown once per restart), and an owner who charges from an operator account gets only the cutoff notice, not also the `gateway_fault` bell. Residuals are listed in section 0h. Tests: `backend/tests/conftest.py` autouse fixture resets `operator_alerts` state and stubs its notify and `send_email` in every test. No frontend change. | Backend thin layer. Optional env vars; set `OPERATOR_ALERT_EMAIL` on the VM to get mail (SMTP already configured). |
| F4 diagnostics UI (TD#28, TD#26) | Frontend only. CpoGateways "Logs" drawer (`components/GatewayLogsPanel.jsx`) on `GET /api/cpo/gateways/{id}/logs`; AdminGateways Fleet/Logs tab on `GET /api/admin/gateway-logs`; new CPO page `/cpo/audit` (`pages/cpo/CpoAudit.jsx`) on `GET /api/cpo/audit`, in the CpoLayout Operations nav group. | Frontend thin layer (new lazy chunk `CpoAudit-*.js`). |
| F5 admin health console | `/admin/health` page (30 s poll while the tab is visible) over `GET /api/admin/health` (`services/sysinfo.py`); reaper and telemetry-flush last-run stamps. Host cron alerter `deploy/scripts/check_host_health.sh` (memory, disk, `/api/health`, with cooldown), see `deploy/docs/host_health_alerts.md`. The cron was installed on the VM on 2026-11-19 (every 5 min). | Backend and frontend thin layers. Optional manual VM step: install the cron (DEPLOYMENT.md "Host health alerts"). |
| F6 cheapest-window start | `tariff-preview` returns `cheapest_window`; ChargeSetupModal offers a one-tap "Cheapest: HH:MM at Rs X/kWh (save ~Rs Y)" for schedulable plugs that fills the scheduled-start time. Backend: `services/pricing.py cheapest_window`; tests `backend/tests/test_cheapest_window.py`. The "ready by T" planner was dropped (see TODO.md). | Backend and frontend thin layers. |
| F11 public status | `GET /api/status` (`routers/status.py`, public, 60 s in-process cache, `STATUS_RATE_LIMIT`) plus a collapsed Network status panel on `/map` (lazy fetch, 60 s poll while open). | Backend and frontend thin layers. Optional `STATUS_RATE_LIMIT`. |

Deploy order: backend thin layer first, then the frontend thin layer. No migration, so the thin-layer kit applies. The only manual VM step is the optional F5 host cron.

---

## 0c - 2026-11-16 batch (PR #148, merged as `ac00d46`, DEPLOYED 2026-11-16)

Twelve branches merged with `--no-ff` onto `ad8fb9a`. Backend suite 1183 passed / 221 DB-gated skips, frontend 870 passed, ruff and eslint clean. Integration fixes: two merge conflicts (`_invoices.py` imports, `_gateways.py` create-route dependency, resolved as admin-only plus the existing in-body tenant check) and two test reconciliations for U4 versus the admin-only gateway create (`test_cpo_tenant_scope.py`, `test_gateway_create.py`).

| Change | What changed | Deploy notes |
|---|---|---|
| U0 reservation | Queued/scheduled auto-start passes the owner's covering reservation to `begin_active_session`, so it ends FULFILLED and linked (`services/session_reaper.py`). | Backend thin layer. |
| U1 dashboard revenue | CPO dashboard today and all-time `revenue_coins` use `payouts.sum_completed_session_coins` (net of approved refunds, same as Earnings). Known limits: today keyed on `ended_at`; the 30-day revenue chart is unchanged. | Backend thin layer. |
| U2 top-up watermark | Offline top-up `created_at` is set from the app clock under the tenant row lock, floored at the watermark read under that lock, so each top-up is deducted exactly once. | Backend thin layer. |
| U3 invoice rate | Invoice lines show an effective rate (total / energy). API line item gains `effective_rate_coins_per_kwh` (old field kept); CSV gains a trailing column; HTML header and footnote changed. No migration. | Backend thin layer. |
| U4 tenant-less scope | Tenant-scoped `/api/cpo` routes use `routers/cpo/_common.require_cpo_tenant` (400 before any query for a caller with no tenant). A tenant-less admin or CPO now gets 400 instead of empty lists or unclaimed inventory. | Backend thin layer. |
| U5 disputes | `GET /api/cpo/disputes` returns `user_email`, `session_cost_coins`, `plug_name` (additive); `CpoDisputes` no longer reads the 200-session lookup. | Backend first, then frontend. |
| U6 telemetry flush | A data error in a flush retries the batch row by row in savepoints, drops and counts bad rows (`_bad_rows_dropped`); connection errors still re-buffer. | Backend thin layer. |
| F10 unmetered alarm | The backend cross-check ignores the session's trailing slice (capped, `UNMETERED_SESSION_TAIL_SLACK_SEC`, default 15 s). Firmware's own one-shot report keeps the same tail gap (needs fw change plus OTA). | Backend thin layer, no new env var. |
| Email case-insensitive unique | Migration `0043_email_ci_unique` adds `uq_users_email_lower` on `lower(email)` (idempotent, old unique kept). Prod had 0 case duplicates on 2026-11-16. | Migration runs on boot via `init_db`; downgrade drops the index; image rollback is safe. |
| Gateway claim-only | `POST /api/cpo/gateways` is admin-only; the CPO manual-register form is removed. | Backend and frontend thin layers, no VM or env steps. |
| Non-root containers | Backend and agent images run as uid/gid 10001; backend gets `cap_drop ALL` and `no-new-privileges`. Code only. | INFRA DEPLOY, not the normal path: full image rebuild via `deploy.ps1`, hand-apply the compose half on the VM, chown the `firmware_images` volume to 10001, DB backup and snapshot first, tag `pre-nonroot` rollback images. See DEPLOYMENT.md "Non-root containers". |
| Instant open | Service-worker app shell (cached index.html and hashed assets, revalidated on every open, kill switch) plus uid-tagged localStorage data snapshots rendered dimmed as "last saved" until refreshed. Follow-up: nginx.conf `/sw.js` comment still says push only. | Frontend thin layer. See DEPLOYMENT.md "Service worker and instant open". |

Deploy order: backend first, then the frontend thin layer. The backend carries migration 0043, which the deploy kit's thin-layer script refuses (it rejects `backend/migrations/versions/*` and expects alembic unchanged), so ship it on the migration path with a DB backup first; it runs at boot via `init_db`. The non-root change is a separate infra deploy and does not block the rest.

---

## 0 - 2026-11-15 fix batch

**Production state (2026-11-16, 15:12Z):** prod == `origin/main` at `ac00d46` (PR #148, the "0c" batch below). Backend: thin layer on the `e7bbcae` image plus a non-root layer (uid 10001, `cap_drop: ALL`, `no-new-privileges`); frontend: dist-only thin layer. Alembic head `0043_email_ci_unique`, gateway firmware `2.9.0-direct`. Backup before the deploy: `amphive-20261116-145613.dump`. Rollback images: `amphive_backend:rollback-ac00d46-m0043` (the e7bbcae code plus the 0043 migration file; use this one, because `pre-ac00d46` cannot boot on a database at 0043), `amphive_backend:pre-nonroot` (the batch image before the non-root layer), `amphive_frontend:pre-ac00d46`; the pre-non-root compose file is `~/amphive-relay/docker-compose.relay.yml.pre-nonroot`. Prod check: a repeat open is served by the service worker (index.html in 12 ms, all assets from Cache Storage, first contentful paint 0.56 s on Slow 4G with 4x CPU throttling).

*Earlier on 2026-11-16:* prod was `e7bbcae` (PR #146, thin backend + frontend layers on the `6a42265` images; rollback tags `pre-e7bbcae`), alembic head `0042_revoked_sessions`, gateway firmware `2.9.0-direct`. PR #146: the app renders from the cached user while `/api/auth/me` revalidates (route chunk and data calls no longer wait for `/me`), and the hourly telemetry prune range-scans `idx_telemetry_plug_recorded` instead of scanning the whole table. Host changes the same day (not in the repo, see DEPLOYMENT.md "Memory and disk on the 1 GB VM"): `vm.swappiness=10` and `google-osconfig-agent` disabled. Live check of session 128 confirmed fw 2.9.0 fast mode: first fast reading 2.6 s after start, then every 1.5 s (the plug's own value refreshes about every 3 s).

*Earlier on 2026-11-15:* prod was `825f27e` (the bug-fix release, alembic `0040_plug_photos`), then `6a42265` (features, alembic `0042_revoked_sessions`).

- Ops housekeeping: `stop_grace_period: 60s` on the postgres `db` service in the relay, prod and dev compose files (the live VM compose needs the same line by hand, see DEPLOYMENT.md).
- Docs and deploy instructions now name the new GCP project (`project-6e290ffd-5954-4d48-aa9`), compute SA `626015685685-compute@developer.gserviceaccount.com`, IP `8.235.4.66` and the `-2` buckets; dated historical records are unchanged.
- New known-limit items (U0-U6, F10, firmware SET_INTERVAL wake, boot round trips) added to TODO.md.
- JWT library swapped from python-jose to PyJWT (`backend/services/auth.py`). Claims, HS256 and the
  `token_version` check are unchanged; tokens issued before the swap still decode
  (`backend/tests/test_jwt_compat.py`). Removes python-jose, ecdsa, rsa and six from the lock, and the
  `--ignore-vuln PYSEC-2026-1325` from the CI pip-audit step.
- **Login copy (C4, C5):** a wrong password on `/login` now shows the server's
  "Invalid email or password." instead of "Authentication expired"
  (`api/client.js` no longer treats a 401 from `/api/auth/login` itself as an
  expired session). A `403 account_disabled` says the account is disabled
  instead of offering "verify your email" and a resend button.
- **`GET /api/auth/me` returns `auth_provider` and `created_at` (C6):** the
  Account page now hides the close-account password prompt for a Google-only
  account and shows "Member since".
- **Google sign-in failures land on `/login` (C19):** every callback error
  (cancelled consent, expired state, disabled account, different Google
  account, token failures) redirects to `/login?google_error=<code>` instead of
  a raw JSON page. `Login.jsx` maps known codes to fixed copy; unknown codes
  get a generic line, so a crafted link cannot put text on the page.
- **Unverified passwords are provisional (C7):** registering again with an
  unverified email replaces the password and name and sends a new link
  (verified emails still get the 400). The first Google link to any existing
  password account, verified or not, still drops its password, bumps
  `token_version`, sets `auth_provider='google'` and marks an unverified
  account verified. Residual in [SECURITY.md](SECURITY.md) §8.6.

Session page (two driver reports on the live session page):

- **Reloading a running session took about 10 s to look live.** Three causes
  stacked. (1) `SessionContext.switchSession` seeded the monitor with zeros and
  ignored the `energy_kwh`/`cost_coins` that `/api/sessions/active` returns; it
  now renders those at once, and the page shows a loading placeholder (not
  "No active charge") until the restore settles. (2) On reload the old stream
  task's teardown sent `SET_INTERVAL 10000` before the new page subscribed, and
  the firmware sleeps out the whole 10 s before honouring the next `1000`. The
  last listener is now released after `TELEMETRY_SLOWDOWN_GRACE_SEC` (default
  30 s), so a reload never slows the gateway. (3) The socket and restore effects
  keyed on the `user` object, so every `refreshUser()` (stop, limit edit, login)
  reconnected the socket and zeroed the monitor; they key on the user id now.
  Also `set_plug_telemetry_interval` always publishes, so a gateway that
  rebooted to its 10 s default is told 1 s again on the next page open.
- **"Low balance" near the end of a kWh-limited session with plenty of credit.**
  With a kWh limit the hold is sized to the limit's cost whenever the wallet
  can afford it, and both the monitor banner and the `low_balance` notification
  measured that hold, so they fired as the driver's own limit approached. Both
  now stay quiet when reaching `max_kwh` at the current rate fits inside the
  hold (the Limit banner already shows that progress), and warn as before when
  the wallet capped the hold or there is no kWh limit. `PATCH /limits` returns
  the resized `hold_coins` so the client compares against the new hold. A
  limit-bound hold runs out on the same frame the kWh limit is reached, so that
  stop is now labelled `auto-stopped: energy limit reached` ("your limit was
  reached") instead of "balance used up". Hold sizing, auto-stop thresholds and
  billing are unchanged.
- **Still open:** the firmware poll loop's `vTaskDelay` is not woken by
  `SET_INTERVAL`, so after a real idle period (no viewer for over 30 s) the
  first fresh frame can still take up to 10 s; fixing it needs a firmware
  change and an OTA.

Session live updates (driver live session page, keeping up while the app stays
open; `fix/session-live-updates`, built on `fix/session-page`):

- **Live telemetry died after any socket reconnect.** The new sid is in no
  rooms and the client only subscribed once. `SessionContext` now re-emits
  `subscribe_session` on every `connect`, and on a reconnect re-reads
  `/api/sessions/active` and the wallet.
- **A backend auto-stop left no bill.** On the `completed` frame (limit,
  balance or hold exhaustion, the reaper, a reservation clear-out) the page now
  refocuses a still-charging sibling, or shows the bill from
  `GET /api/sessions/{id}`, and refreshes the wallet. A driver Stop that leaves
  a sibling charging puts the bill (kWh and cost) in the toast.
- **Queued charges were frozen while the app stayed open.** A
  `queued_charge_*` or `session_stopped` notification re-syncs the active
  sessions (an auto-started queued charge takes focus when nothing else is
  live), and the Dashboard "Up next" card is on a 30 s poll and refetches on
  queue and reservation notifications.
- **Opening the page while the gateway is at its 10 s idle cadence.** The
  REST-row or cached-frame numbers show at once; for the first 15 s after a
  (re)subscribe a missing, stale or old frame shows a small "Updating…" pill
  instead of the "Live readings paused" banner, which now also appears when no
  frame at all has arrived 15 s after subscribing. Power shows "-" until the
  first frame instead of a fabricated 0. Frames are merged over the REST row,
  so the charger name no longer disappears, and a late frame for another plug
  is ignored.
- Remaining: the firmware still sleeps out its current 10 s interval before
  honouring `SET_INTERVAL 1000`, so live power can lag up to about 10 s after
  the page opens when the driver was away longer than the 30 s grace.

Device events (plug status, telemetry persist and live stream):

- **Session end keeps an out-of-service plug out of service (C1).**
  `finalize_charging_session` now locks and re-reads the plug and frees it
  only from OCCUPIED. AVAILABLE stays AVAILABLE; MAINTENANCE and OFFLINE
  (an operator change made mid-session, or auto-maintenance) persist. The
  `plug_status` emit and the "notify me when free" fan-out run only when
  the final status is AVAILABLE; the capacity fan-out always runs, since
  the ended session frees circuit current either way. For
  THERMAL_CUTOFF / OVERCURRENT_CUTOFF, auto-maintenance now runs BEFORE the
  post-cutoff finalize, so watchers are no longer told a faulted plug is free.
- **Telemetry persist no longer deadlocks against finalize (F17).**
  `_persist_telemetry` wrote the plug row before its session
  `SELECT ... FOR UPDATE`, so autoflush locked plug then session while
  finalize locks session then plug (prod deadlocks 2026-10-25 and
  2026-10-28). The plug writes now follow the session lock; no retry loop.
- **Live kWh and cost follow billed energy (C16).** The TelemetryStore is
  fed the session's monotonic billed energy (REC-01 reset offset included)
  instead of the raw wire counter, so a gateway counter regression no
  longer makes the live display read below the final bill.
- **Finished sessions are not streamed (C15).** `subscribe_session` on a
  session that is no longer ACTIVE sends one `completed` frame from the
  session's own row and joins no room, so it cannot show the plug's next
  driver's live figures (see [SECURITY.md](SECURITY.md) section 4).

CPO money and console:

- **Invoices after a refund (C11)** - `issue_invoice_for_session` now nets the
  session's APPROVED dispute refunds off the invoice at first issue. Before, a
  dispute approved before the driver first opened the invoice left nothing for
  `adjust_invoice_for_session_refund` to adjust, so the invoice reported (and
  the CPO GST list/CSV overstated) the full pre-refund total. A fully refunded
  session now issues a 0.00 invoice, the same shape the adjust path produces.
- **CPO dashboard "today" is tenant-local (C12)** - `GET /api/cpo/analytics/overview`
  "today" and the daily revenue/energy series (`/analytics/revenue`,
  `/analytics/energy`) now use the tenant's `timezone` (same resolver as pricing,
  default `Asia/Kolkata`) instead of the UTC day, which rolled over at 05:30 IST.
  Follow-up 2026-11-17 (deployed 2026-11-17 in a27841d): "today" energy and session count use
  `ended_at` like the revenue, and the revenue series nets approved dispute
  refunds on each refunded session's own day.
- **Plug edit can reset to inherit (C13)** - `PUT /api/cpo/plugs/{id}` treats an
  explicit JSON `null` for `queued_charging_enabled` / `auto_start_delay_min` as
  "clear the override back to the tenant default"; an omitted key still leaves it
  unchanged (`0` still clears the delay).
- **Reservations Day view stays fresh (C14)** - the Day timeline refetches on
  every switch to it, so a cancel made in the List view is no longer drawn
  stale. Known limit: the Day fetch is still the newest 200 reservations, so a
  tenant with more than 200 in total sees empty oldest days (one reservation in
  prod today; add from/to params to `GET /api/cpo/reservations` if that changes).

Plug photos and reviews:

- **Plug photos: private bucket, gated image route (PR #127).** The photo
  bucket stays private (no `allUsers` binding). Bytes are served by
  `GET /api/plugs/{id}/photos/{photo_id}/image`: approved photos to anyone with
  `Cache-Control: public, max-age=86400`; pending and rejected photos only to a
  CPO of the plug's tenant or an admin (`private, no-store`), 404 for everyone
  else. Every photo `url` and `thumbnail_url` is that relative route. The CPO
  page loads images with the Bearer token and shows them from `blob:` URLs, so
  the CSP `img-src` (all copies in `frontend/nginx.conf`, the Caddyfile example
  and `deploy-relay.sh`) now allows `blob:`; the uploader's own pending photo
  shows a placeholder.
- **Photo uploads up to 8 MiB actually reach the backend.** nginx allows 9m on
  `/api/plugs/<id>/photos` (nested in the `/api/` block) and the body-size
  middleware allows 8 MiB on that POST; the rest of the API keeps 1 MiB.
- **Review edits work.** Re-submitting a review no longer 500s: the handler no
  longer reads ORM attributes that the IntegrityError rollback expired.
- **Photo takedown.** A CPO can reject an approved photo (removes it from the
  map and deletes the object), and re-rejecting a rejected photo retries a
  failed delete. Approve stays pending-only. Rejected rows show a "Removed"
  placeholder instead of a broken image.

---

## 0b. 2026-11-15 feature batch

Merged on `release/2026-11-15-features` on top of the 2026-11-15 fix release
(`825f27e`). One subsection per feature branch.

### Cloudflare front door for amphive.app / cpo.amphive.app (LIVE since 2026-09-28, DNS cut over)

Moving `amphive.app` + `cpo.amphive.app` behind Cloudflare's free HTTP proxy
to cut latency for Indian users (origin is a `us-west1` e2-micro, ~240 ms RTT
to India). `mqtt.amphive.app` and `mail.amphive.app` stay DNS-only / grey
cloud: MQTT 8883 and SMTP/IMAP can't traverse the CF HTTP proxy. DNS is
now served by Cloudflare's nameservers (`harlee`/`ridge.ns.cloudflare.com`,
cut over from name.com 2026-09-28; the old name.com records were left in
place, so switching the nameservers back is the rollback).

**Caddy side:** Global `trusted_proxies` now lists
Cloudflare's published edge ranges + `client_ip_headers CF-Connecting-IP`,
and every `reverse_proxy` sets `header_up X-Forwarded-For {client_ip}`
(`deploy/config/Caddyfile.example`, `deploy/relay/deploy-relay.sh` both
updated; live Caddyfile hand-edited to match, verified with `caddy validate`
+ reload, semantics tested on caddy v2.11.4 in a throwaway container). From a
Cloudflare peer the upstream XFF collapses to the vouched `CF-Connecting-IP`
value only (a spoofed XFF is discarded); from an untrusted peer
`CF-Connecting-IP` is ignored and XFF falls back to the real peer IP.
`backend/services/rate_limit.py`'s `TRUSTED_PROXY_HOPS=2` is unchanged
(Caddy still appends one hop, frontend-nginx the other), only its topology
comments were updated. Cloudflare dashboard: SSL/TLS Full (strict), Always
Use HTTPS OFF (Caddy already redirects; keeps ACME HTTP-01 on :80 flowing
through CF untouched), Rocket Loader / Email Obfuscation / Bot Fight Mode
OFF (conflict with the strict CSP / would JS-challenge API clients),
WebSockets ON (Socket.io). GCP firewall on 80/443 was **deliberately not**
locked to Cloudflare's ranges: the origin IP stays directly reachable
because `mail.amphive.app`'s Let's Encrypt HTTP-01 renewal needs :80 open
directly, not routed through Cloudflare. See
[DEPLOYMENT.md](DEPLOYMENT.md#cloudflare-front-door-amphiveapp-cpoamphiveapp-added-2026-09-28)
and [SECURITY.md](SECURITY.md#6-operational-notes) for the full write-up.

**Verified after cutover (2026-09-28):** `.app` registry delegates to
Cloudflare; apex + `cpo` resolve to Cloudflare anycast, `mqtt`/`mail` to the
VM. HTTPS 200 on both hosts via the edge (the Universal SSL edge cert took a
few minutes to appear after activation; expect a short TLS-handshake-failure
window on a fresh zone), HSTS/CSP pass through, `/assets/*` `cf-cache-status:
HIT` after the first fetch, `/api/*` `DYNAMIC` + `no-store`, Socket.io
handshake offers the websocket upgrade, Google OAuth redirect intact, broker
TLS on 8883 direct. Client IP: a marked request via Cloudflare and one direct
to the origin both reached frontend-nginx with `X-Forwarded-For` = the real
client IP. Measured from an Indian connection (edge colo MRS, Marseille; Indian
traffic on the free plan is often not served from an Indian colo): median fresh
connection page TTFB 728 → 582 ms, API 733 → 559 ms, main JS bundle total
2925 → 954 ms (edge-cached).

### fw 2.9.0-direct, SET_INTERVAL wakes the poll loop (source only)

`firmware/main/main.c`: the telemetry task now sleeps in `ulTaskNotifyTake`
with the interval as the timeout, and `SET_INTERVAL` notifies it when the value
changes, so a session's 1 s cadence starts at once instead of after the current
idle sleep (up to 10 s). Timing audit: the energy integrator uses the measured
tick delta and the duration watchdog `now_seconds()`, so an early sweep cannot
miscount energy or time. `PROJECT_VER` is `2.9.0-direct`. The software gateway
(`agent/amphive_agent/core.py`) gets the same wake, plus a gap clamp that allows
the interval the cut-short sleep used (`agent/test_poll_wake.py`).
**Not yet built:** there is no signed 2.9.0 image yet (build with ESP-IDF
v5.3.3, target esp32, as for 2.8.0). The field gateway stays on 2.8.0-direct. See [FIRMWARE.md](FIRMWARE.md) (§3 and §8).

### Per-device logout (`feat/per-device-logout`)

Log out now signs out only this app in this
browser. Every sign-in (password login, email verification, Google
callback) mints a random `sid` claim that `/me` renewals carry unchanged;
`POST /api/auth/logout` inserts it into the new `revoked_sessions` table
(migration `0042_revoked_sessions`, after the off-peak
`0041_queued_not_before`). `get_current_user` (401,
same detail as a `token_version` mismatch), `get_current_user_optional`
(anonymous, same as a mismatch there), Socket.io `connect` and the socket
re-auth all reject a revoked sid with one PK lookup. A token minted before
this has no sid and its logout falls back to the global bump
(`scope:"all"`). New `POST /api/auth/logout-everywhere` keeps the global
`token_version` bump, offered behind a confirm step on the driver Account
page and on CPO Settings (the only place cpo/admin users can reach it,
their console being on the other origin). Every existing `token_version`
bump is unchanged. Rollback runbook step and residuals in
[SECURITY.md](SECURITY.md) §2. Tests:
`backend/tests/test_per_device_logout.py` (DB-free plus DB-gated),
`frontend/src/components/LogoutEverywhere.test.jsx`.

### Backup failure and staleness alerts (`feat/backup-alerts`)

`deploy/scripts/backup_db.sh` mails the operator (`ALERT_TO`, default
`support@amphive.app`, via the host `sendmail`) when the nightly backup exits
non-zero, naming the failed step and the log tail. The new
`deploy/scripts/check_backup_fresh.sh` (cron, 01:00 UTC) mails when the
last-success marker is older than `MAX_AGE_HOURS` (default 26), which catches a
backup that never ran. Both run on the VM they watch, so a down VM or a broken
Postfix sends nothing. Tests: `deploy/scripts/test_backup_alerts.sh`. Install
steps in [deploy/docs/db_backup_restore.md](../deploy/docs/db_backup_restore.md);
the live VM script differs and is merged by hand.

### Scheduled (off-peak) start (`feat/offpeak-start`)

The driver plugs in now and picks a start time; the backend energizes the plug
then. Built on the queued-charge machinery (`QueuedCharge.not_before`, migration
`0041_queued_not_before`, on top of `0040_plug_photos`). API in
[API_REFERENCE.md](API_REFERENCE.md#queued-charges-servicessession_reaperpy-queue-a-charge-during-an-outage),
design record in
[proposals/queued-charge-offline-plug.md](proposals/queued-charge-offline-plug.md#scheduled-off-peak-start).

- Start modal: "Start: Now | At a time" on a schedulable plug, time entered and
  shown in the tenant zone; a time or energy limit is required (8h preselected).
- Sweep: not due -> WAITING; due -> the existing debounce, caps, balance floor and
  hold; someone else on the socket or a covering booking -> FAILED; flag turned
  off -> FAILED. Every auto-start (outage rows too) now replays the gateway
  liveness gate, and STARTED is written in the same commit as the session claim.
- Start route: 409 `plug_scheduled` for other drivers; the owner's start cancels
  their own schedule after it succeeds. The reconnect orphan-OFF is never held for
  a scheduled row.
- **Deviation from the settled decision "expires_at = not_before + queue TTL":** a
  scheduled row expires `min(queue_ttl_min, SCHEDULE_START_GRACE_MIN = 30)` after
  `not_before`, so a dark plug or a late gateway does not start an unattended peak
  charge hours later. Set `SCHEDULE_START_GRACE_MIN` >= `queue_ttl_min` to revert.
- Not built: "start at the next cheaper rate" (the setup modal only gets the next
  price boundary later today, not the tariff's slot list).

Known limits (one-user project, so multi-driver items are documented, not fixed):
24 h max lookahead; a schedule blocks other drivers' manual starts on that plug
and other drivers see the plug AVAILABLE until they get the 409; bookings made
after the schedule are not refused (the schedule FAILs at due time); a Wi-Fi blip
right before `not_before` re-baselines `powered_since` and delays the start by the
debounce; a fault (MAINTENANCE) is only reported at due time; the relay is not
verified OFF while waiting (a Tapo-app press charges unbilled until reconcile, as
for any idle plug); no zero-draw auto-stop, so a car that never draws runs to its
limit; outage rows still do not re-check the queued-charging flag at auto-start;
tenant time depends on the prod image shipping tzdata (unverified).

---

## 0a. 2026-10-20 connection noise: debounced offline notices, gated orphan bells, fw 2.8.0 diagnostics

A 45-day forensic pass over the prod logs (one user, who is driver, operator
and admin at once) found that most of the bells were noise, and why:

- **F1** — the ESP32 gateway reset itself about once per powered hour: 68-70
  of 79 reconnects were chip resets, and nothing recorded why.
- **F2** — the backend had no debounce on gateway offline/online. Every
  sub-second reconnect rang "Charger connection lost" (during a session) or
  "Plug auto-recovered" (the `orphan_off` bell, when idle): 96 of 139
  notifications (69%), with 22 of 33 offline->online pairs reversing within
  1 s.
- **F3** — the 300 s stale-session reaper ended 12 of 26 sessions.
- **F4** — about 6 plug-side outages in 55 days where the gateway's KLAP/HTTP
  polls failed silently, and a failed `get_device_info` was reported as
  relay OFF.
- **F5** — no reset reason, no Wi-Fi disconnect reason, no MQTT error
  handler, and forwarded log lines were dropped while MQTT was down.
- **F11** — the per-frame "Telemetry received" INFO line was 99% of all
  backend log lines.

**What changed in the backend** (branch `fix/connection-noise`; no migration,
alembic head stays `0038`):

- **Debounced offline side effects (F2).** A real ONLINE->OFFLINE transition
  still flips `gateways.status` at once, so `gateway_is_live` and every start
  gate stay truthful. The driver notice and the `plug_connectivity`
  `gateway_online:false` push now wait `GATEWAY_OFFLINE_NOTIFY_DELAY_SEC`
  (default 120 s) in a per-gateway task (`services/mqtt/status.py`
  `_deferred_offline_notice`). That task re-reads the gateway in a fresh DB
  session and only acts if it is still OFFLINE. Any `online` for that gateway
  in the meantime, a retained replay included, cancels the task and logs
  "Gateway back within offline-notice window - notice suppressed". A second
  offline replaces a pending task, `MQTTManager.stop()` cancels whatever is
  pending, and `<= 0` restores the old immediate behaviour. The online
  connectivity push is unchanged.
- **Orphan-OFF bell gating (F2).** On reconnect every plug without an ACTIVE
  session still gets its OFF, exactly as before (REC-06 ordering and the
  queued-charge hold untouched). But the CPO `orphan_off` bell now also needs
  the plug's last-known `plugs.current_power_w >= ORPHAN_ALERT_MIN_POWER_W`
  (default 5 W), read in the same snapshot query. An idle plug's OFF only logs
  at INFO. A relay found ON with no session is still covered by the
  firmware's own `UNAUTHORIZED_ON` critical alarm. The bell text now says how
  much the plug was last drawing.
- **Stale reaper (F3).** Prod sets `SESSION_STALE_TIMEOUT_SEC=1200` through
  `.env`. The code default stays 300 s.
- **Log volume (F11).** "Telemetry received" is DEBUG now. Every frame still
  lands in `telemetry_readings`.
- **fw 2.8.0-direct diagnostics (F1/F4/F5).** `/alarms` now understands
  `GATEWAY_BOOT` (info: firmware, reset reason and code, boot count, Wi-Fi
  RSSI/attempts/last disconnect reason, minimum free heap), `PLUG_UNREACHABLE`
  (warning: N polls in a row without a complete reading) and `PLUG_RECOVERED`
  (info: down for ~N s). Each is folded into a readable `detail`. Every value is treated as
  untrusted (coerced, sanitized, range-checked), and every alarm's `detail` is
  now capped at the 255-character column. Before, an absurd
  `UNMETERED_CONSUMPTION` estimate could overflow the column and drop the
  event. None of the three finalizes, enters maintenance or bells.
  `GATEWAY_BOOT` and `PLUG_RECOVERED` are forensic records, not alerts, so
  they are stored already acknowledged (`_PRE_ACKED_EVENT_TYPES`; why that
  matters is in the frontend note below). The "Gateway alarm" log line
  carries the detail, so the backend log alone shows why a gateway
  restarted. Exact payloads are in [MQTT_CONTRACT.md](MQTT_CONTRACT.md). The
  firmware side ships separately as fw 2.8.0-direct. **Deploy this backend
  before flashing or OTA-ing fw 2.8.0**: an older backend stores the new
  events as untitled, unacknowledged warnings and parses the `[offline]` log
  replays as info.

**Frontend (minimal).** The driver's session monitor used to render *any*
recent alarm for its plug as a red banner. It would have shown
`PLUG_RECOVERED` as a red "Plug reachable again" alert in the middle of a
session. Now it takes the newest alarm for the plug and renders it only if
it is not info-level. A newer `PLUG_RECOVERED` therefore clears the
`PLUG_UNREACHABLE` banner instead of leaving it up for its full 2 minutes.
`LOCAL_LIMIT_CUTOFF` (info, software agent only) no longer shows a red banner
either; the finalize already tells the driver. `statusCopy` gained the
operator-facing names "Gateway restarted", "Plug unreachable" and "Plug
reachable again".

The CPO alert strip and the Health page did not change, but the data they
read did. The strip renders only critical/warning, yet it filters in the
browser *after* fetching the newest 50 unacknowledged events
(`unacknowledged_only=true&limit=50`). A boot report on every chip reset
would have filled that window and pushed an older unacknowledged critical
(an `UNAUTHORIZED_ON`, say) out of the strip — after about a month of
ordinary resets, or within the hour in a brownout boot loop — and ticked the
Health nav badge each time. Storing `GATEWAY_BOOT` and `PLUG_RECOVERED`
already acknowledged keeps them out of that set. They are still persisted,
broadcast and logged; the Health page hides them by default ("Unacknowledged
only" starts ticked) and lists them once it is unticked. `PLUG_UNREACHABLE`
is a real warning and stays unacknowledged, so it shows in the strip.
`backend/tests/test_gateway_event_feed.py` (DB-gated) proves that 60 boots
plus a recovery leave an older `UNAUTHORIZED_ON` in the strip's window.

**Known gaps (not changed here):**

- The alert strip still windows before it filters. With the forensic rows
  pre-acknowledged the window holds only real alerts, but 50 or more newer
  unacknowledged *warnings* (a flapping plug's repeated `PLUG_UNREACHABLE`,
  say) could still push an older critical out of the strip. This is
  pre-existing; the event stays on the Health page and in the badge count. A
  server-side severity sort or filter on `/api/cpo/events` would close it.
- The Health nav badge (`TenantContext.unackedEvents`) counts every
  unacknowledged event, so the remaining info notices (`OTA_*`,
  `LOCAL_LIMIT_CUTOFF`) still tick it until acknowledged. They are rare;
  `GATEWAY_BOOT` and `PLUG_RECOVERED` no longer do.
- A notice still pending when the backend stops is cancelled. After the
  restart the retained `offline` replay is not a transition, so no notice is
  re-armed. A gateway that dropped within ~2 minutes before a deploy never
  produces the driver notice. The reaper still closes the session.
- Two status messages for the same gateway are persisted by independent
  coroutines (pre-existing). If they interleave so that the stored status ends
  OFFLINE although the gateway is back, the deferred notice fires. It then
  matches what the start gates see, and the re-read covers the opposite
  order.

---

## 0.00005: 2026-08-21 photos & reviews for public plugs

Turned the discovery map into a trust surface: public plugs now carry driver
**star reviews** and **photos**.

- **Reviews (✅):** verified chargers only, a driver may rate/review a plug only
  after a finished `ChargingSession` (`completed|paid`) there; one standing
  review per driver (re-submit edits it). `POST/GET/DELETE
  /api/plugs/{id}/reviews`; the GET is public. `avg_rating` + `review_count`
  ride `PublicPlugResponse`/`PlugResponse` via one grouped query
  (`resolve_review_aggregates_batch`, no N+1).
- **Photos (✅, held for approval):** same verified-charger gate; an upload lands
  `pending` and is invisible publicly until a CPO/admin approves it. `POST/GET
  /api/plugs/{id}/photos` (raw body, magic-byte-sniffed JPEG/PNG/WebP ≤ 8 MiB,
  no multipart); moderation at `GET /api/cpo/plug-photos` + `.../approve|reject`
  (CPO console, **Plug photos**). Bytes live in the private bucket
  `gs://amphive-plug-photos-2` (keyless, `services/photo_publish.py`; set `PLUG_PHOTO_GCS_BUCKET`) and are
  served by the gated image route (see the 2026-11-15 fix batch); one approved
  photo becomes each plug's `thumbnail_url`.
- **Frontend:** a `StarRating` component, a `PlugReviewsModal` (reviews + photo
  gallery + upload) opened from the map popup/list, popup thumbnail + rating, and
  the `CpoPlugPhotos` moderation page.
- **DB:** migrations `0039_plug_reviews` + `0040_plug_photos` (idempotent,
  chained on `0038_account_closure`).
- **INFRA (operator, one-time):** bucket `gs://amphive-plug-photos-2` (exists, asia-south1, private: never
  add `allUsers`) needs only `PLUG_PHOTO_GCS_BUCKET=amphive-plug-photos-2` in the VM `.env`; the compute SA already has `roles/storage.objectAdmin`, see
  [../deploy/docs/plug_photo_publishing.md](../deploy/docs/plug_photo_publishing.md).
  Until then, photo uploads 502.

---

## 0.0001 — 2026-08-18 production-readiness audit: privacy, data rights, SEO, a11y

A whole-product audit (35 areas, from secrets and injection through to mobile
UX and error pages) plus the remediation batch. Full security detail is in
[SECURITY.md](SECURITY.md) §"2026-08-18" and is not repeated here; this entry
records what changed for the PRODUCT.

**The biggest gap was not security — it was that a service handling names,
emails, locations and payments had no privacy surface at all.** There was no
Privacy Policy, no Terms of Service, no refund policy and no contact page; the
only footer in the app was the marketing one on the anonymous homepage, so
signing in made even the charging-credit terms unreachable; and there was no
way for a user to get their data or close their account. Now:

- **New public pages** — `/privacy`, `/terms` (umbrella Terms of Service),
  `/refunds`, `/contact`. The existing closed-loop credit terms moved verbatim
  from `/terms` to `/charging-credit-terms` (the RBI closed-system PPI framing
  is load-bearing — see the file header before touching it), and `/terms` links
  to it prominently so any external reference still lands in one click. Every
  claim on the privacy page was written against source, with the check named in
  the file header. Nothing claims certification or compliance with any regime.
- **A footer on every surface** (`components/SiteFooter.jsx`), rendered by the
  driver shell, both consoles and the auth screens.
- **Consent at signup** — a stated-consent line linking Terms and Privacy
  (`Signup.jsx`'s old header comment said none was invented because none
  existed to link to; they exist now).
- **`GET /api/auth/me/export`** — self-service data export, strictly self-scoped,
  no credentials, bounded per collection.
- **`DELETE /api/auth/me`** — self-service account closure. **Anonymises rather
  than deletes**: every `user_id` FK is `ON DELETE CASCADE`, so a real delete
  would take the charging sessions, ledger rows and GST invoices with it — the
  operator's tax records, and the inputs to the CPO earnings/payout watermark.
  Personal rows are purged, the account row becomes a scrubbed tombstone, and
  remaining charging credit is forfeited as an explicit `account_closure` ledger
  line so the running balance still reconciles. Alembic `0038_account_closure`.

**SEO — the app was invisible and un-shareable.** ~35 routes all served the
same `<title>`, there was no canonical (two hostnames serve a byte-identical
bundle), no Open Graph or Twitter tags at all (a link pasted into WhatsApp —
the main distribution channel for a consumer app in India — unfurled bare), no
`robots.txt`, no `sitemap.xml`, no structured data, and no `noindex` on the
authenticated surfaces or the operator console. All added: a `useDocumentMeta`
hook (per-route title/description/canonical/robots, noindex by default so
authenticated pages opt out of indexing rather than into it), a generated
1200x630 share image (`frontend/scripts/make-og-image.py` — real brand assets
only), robots/sitemap, and Organization + WebSite JSON-LD with no invented
fields.

**A real 404.** Every unknown path used to `<Navigate to="/">` — a silent
bounce to the homepage — and nginx answered **HTTP 200** for all of them, so
every mistyped link was a soft 404 inviting crawlers to index unlimited garbage
as homepage duplicates. There is now a `NotFound` page and an nginx route
allowlist that returns a genuine 404 status, with a vitest test
(`src/nginxRoutes.test.js`) that fails CI if a route is added to `App.jsx`
without updating the allowlist.

**Accessibility.** Skip link, SPA route-change announcement + focus reset,
`<main>` landmark on the one page missing it, `contentinfo` landmark for the
console footers, screen-reader text for the status dots that conveyed
online/offline by colour alone, tabs wired to their panel with
`aria-controls`/`role="tabpanel"`, and `eslint-plugin-jsx-a11y` at error
severity so none of it regresses. That linter also found a real bug:
`aria-selected` on a `<button>` inside `role="group"` (AdminPayouts) is ignored
by assistive tech, so the active filter read as unselected — now `aria-pressed`.

**Mobile + performance.** Touch targets raised to 44px under
`@media (pointer: coarse)` only (desktop console density untouched), `100dvh`
for the iOS toolbar gap, gzip at the edge (the ~300 KB bundle shipped
uncompressed on any non-Caddy path), and vendor chunk splitting: the CPO
dashboard route chunk went **397 kB -> 8.8 kB** and the map route **163 kB ->
10 kB**, with recharts and leaflet now cached independently of app code.

**Post-deploy fixes (PR #122, same day).** A cross-device Playwright pass
against the *deployed* site (desktop 1440 / tablet 834 / mobile 390) caught
three regressions that neither the unit tests nor the header smoke test could
see, worth noting as a category, since all three were invisible to the suites
that were green: (1) `DriverShell` gated `SiteFooter` on `user`, so an
**anonymous** visitor to `/privacy`, `/terms`, `/refunds` or `/contact` got no
footer at all, and anonymous is precisely who reads a privacy policy (someone
deciding whether to sign up, or a payment processor's reviewer); the gate only
ever existed to avoid stacking two footers on the marketing homepage, so it now
conditions on that one case. (2) `/map` never called `useDocumentMeta`, so the
public charger-discovery surface inherited the site default, which is
noindex-by-default, and carried no `index` directive. (3) `.mkt-hero-visual::before`'s
decorative radial glow (`inset: -12% -8%`) pushed the document 4px past the
viewport at tablet width, giving the landing page a horizontal scrollbar.

---

## 0.0005 — 2026-08-13 billing correctness: counter-dip double-count + false unmetered alarms

Owner ran a real 4-hour charge (prod session 80, plug 1) and hit both halves
of the same underlying assumption — that a drop in the firmware's
session-relative kWh counter means the counter restarted at zero.

**Overbilled 1.2725 kWh (backend, deployed).** At 12:13:46 the gateway
reconnected mid-session. The firmware persists its energy meter to NVS only
every `ENERGY_PERSIST_THRESHOLD_WH` (50 Wh), so the restored meter came back
**47.9 Wh behind** (raw 1.2725 → 1.2246) and kept climbing from there — it did
not reset. REC-01's reset handler
(`services/mqtt/telemetry.py` `_persist_telemetry`) fires on any drop past
`ENERGY_COUNTER_RESET_DROP_KWH` (5 Wh) and banked the **entire** pre-drop
reading into `energy_reset_offset_kwh`, so the 1.2725 kWh the counter went on
to re-report was billed twice: `energy_kwh` finalized at **3.0626** for
**1.838** actually delivered, charging 33.78 coins instead of 19.08.
With a 5 Wh trigger against 50 Wh of NVS granularity, this fired on
essentially every mid-session gateway reboot. Fix: bank the **drop**
(`last_raw - kwh`), not `last_raw` — correct for both shapes, since a genuine
reset to ~0 banks `last_raw - ~0` exactly as before, while a partial dip banks
only the lost slice and keeps `offset + kwh` continuous. Session 80 was
re-billed and the driver refunded.

**False `UNMETERED_CONSUMPTION` after every session (firmware 2.7.0-direct).**
Two seconds after that session stopped, the CPO portal warned of 1.845 kWh
"consumed with no billed session covering it" — the driver's own charge.
`tapo_plug_reconcile_idle_baseline` is the only writer of the plug's idle
baseline and `telemetry_task` only calls it on the idle branch, so the
baseline froze for the whole session and the first idle poll after the stop
measured all of it as a delta. Fix: the session branch now calls
`tapo_plug_advance_metered_baseline()` once per sweep. See
[FIRMWARE.md](FIRMWARE.md) §4a.

---

## 0.0007: 2026-08-11 audit remediation: MQTT ingestion freeze, OAuth takeover, email verification

Three merged PRs from the 2026-08-11 Claude-native security audit
([audits/security-audit-2026-08-11.md](audits/security-audit-2026-08-11.md)),
recorded here retroactively on 2026-08-21, the audit's findings were tracked
in [TODO.md](TODO.md) but never reached this page.

**PR #117: one malformed frame could freeze ingestion platform-wide.** The
highest-impact of the three, and not an auth bug at all: `_on_message` decoded
JSON, then handed the result to a topic handler that called `payload.get(...)`.
A non-*object* body (`5`, `[]`, `"x"`, `true`, `null`) parses perfectly well
and then raises `AttributeError`, on the paho network-loop thread, which
paho 2.1.0 runs with `suppress_exceptions=False`. The escape killed the loop
thread outright, freezing **all** MQTT ingestion for **every tenant**,
telemetry, billing accrual, auto-stop, status, alarms and safety-finalize,
discovery, until someone restarted the backend. The precondition was one
gateway credential and a single 1-byte publish to its own topic. Fixed on both
levels: reject non-dict payloads before dispatch, *and* wrap decode+route+
handler in a catch-all so no future handler bug can escape either.

**PR #118: Google OAuth account pre-hijacking (HIGH).** Register the victim's
email as a password account before they ever sign up; when they later "Sign in
with Google", the link path attached the Google identity to the attacker's
existing row, leaving the attacker's password live on the victim's account.
Linking now invalidates the stored password, bumps `token_version` (evicting
the attacker's sessions) and sets `auth_provider='google'`. Shipped alongside
three lower-severity fixes: the per-account login cap became failure-only (as a
pre-handler dependency keyed on the submitted email, it was a lockout DoS, a
correct password now clears the bucket and is never throttled), a per-email
forgot-password cap against inbox flooding, and localhost origins dropped from
the default prod CORS allowlist.

**PR #119: email verification at registration.** #118 evicted the attacker at
link time; this removes the precondition, so an unverified email can no longer
hold a usable credential in the first place. Migration `0037` **grandfathers
every existing account as verified**, the live prod user base was not locked
out; only new signups verify. The user-visible consequence is that
registration no longer auto-logs-in.

Two ordering details worth keeping, both about not creating oracles: `login()`
checks verified status **after** the password, and the resend endpoint's
per-email rate limit is keyed **before** the account lookup.

---

## 0.001 — 2026-08-04 admin console completion: cross-tenant chargers view, admin OTA, UI-based firmware publishing

Trigger: the platform operator logged into the CPO portal with the admin
account and found it empty — the admin console at `/admin` now covers the
whole fleet. Branch `feat/admin-dashboard`. Four backend additions plus the
matching admin-portal UI:

- **`GET /api/admin/stats/overview` extended** — `plugs` splits into
  `{total, public, private}` (`public` = ungrouped or in an
  `is_public` group; `private` = group with `is_public` false), and
  `energy_kwh` / `revenue_coins` each gain a `last_30d` window
  (`started_at >= now-30d`; revenue over COMPLETED+PAID sessions).
- **New `GET /api/admin/plugs`** — the **first-ever cross-tenant charger
  listing** (previously the admin console only had a plug *count* in the
  overview). Filters `q` (name ilike), `visibility=public|private`,
  `status` (a `PlugStatus`, 400 on invalid); house `{total, items}` +
  `limit` (cap 200)/`offset` pagination. INNER-joins Gateway+Tenant
  (claimed gateways only), LEFT-joins the charger group; items carry
  gateway/tenant/group names, `visibility`, live power, telemetry
  freshness, and `tariff_id`.
- **New `POST /api/admin/gateways/{gateway_id}/ota`** — cross-tenant OTA
  with the same body/semantics as the CPO endpoint (`CpoGatewayOtaRequest`:
  `release_id` XOR `firmware_url`) but no tenant scoping — an admin can
  target any gateway including unclaimed inventory, and `firmware_url` is
  allowed without the CPO-side 403. Gates on raw `status == ONLINE` (the
  same deliberate choice as the CPO endpoint), 409 with no plugs to route
  through, 502 on broker publish failure; audited as `gateway.ota`.
- **New `POST /api/admin/firmware-releases/upload`** — UI-based firmware
  publishing: raw-body (`application/octet-stream`, ≤4 MiB) upload of the
  signed `build/amphive-gateway.bin`. The backend validates the ESP image
  magic (`0xE9`) + `esp_app_desc` magic (`0xABCD5432`), extracts the
  version and project name (must be `amphive-gateway`) from the binary
  itself, stores it under `FIRMWARE_IMAGE_DIR` (docker volume in
  `docker-compose.relay.yml`), auto-registers the `FirmwareRelease`
  (audited `firmware_release.upload`; duplicate version → 400), and serves
  it from the **new public, unauthenticated
  `GET /api/firmware/images/{filename}`** (`routers/firmware_images.py`;
  filename-whitelisted + path-contained; public by design — parity with
  the world-readable GCS bucket, the on-device ECDSA app signature is the
  trust anchor). Publishing is now: build locally → drag the `.bin` into
  Admin → Firmware Releases → push from any gateway page picker.
  `gs://amphive-fw-2` remains valid for already-registered URLs and as the
  script fallback (`deploy/scripts/publish_firmware.ps1`).
- **Frontend:** new **Admin → Chargers** page (`/admin/chargers`) listing
  all public+private chargers with filters; an **"Update firmware"
  picker** on Admin → Gateways (mirrors the CPO flow, custom-URL always
  available); the **upload widget** on Admin → Firmware Releases; and the
  Admin Overview gains Energy(30d)/Revenue(30d) tiles plus the
  public/private charger split.

See [API_REFERENCE.md](API_REFERENCE.md) (Platform Admin Console table +
the public firmware-image host note), [FIRMWARE.md](FIRMWARE.md) §7
(publishing flow), [DATA_MODEL.md](DATA_MODEL.md) (`firmware_releases`),
and [SECURITY.md](SECURITY.md) §3 (why the image route is public).

---

## 0.005 — 2026-08-04 runtime Wi-Fi reconnect fix (fw 2.5.0-direct)

Real incident: a home router rebooted at midnight and the gateway never came
back — it needed a manual power cycle in the morning. Root cause in
`firmware/main/main.c`: on `WIFI_EVENT_STA_DISCONNECTED` the handler retried
`esp_wifi_connect()` only `MAXIMUM_RETRY` (5) times — exhausted in well under
a minute while the AP was still down — then set `WIFI_FAIL_BIT` and stopped.
Nothing consumes that bit after boot (only boot-time `wifi_init` and the
portal pre-check wait on it), and ESP-IDF never reconnects on its own, so the
gateway sat disassociated forever while the backend's LWT reaper marked it
offline.

Fix (fw `2.5.0-direct`): once the STA link has held an IP at least once,
exhausted retries arm a 10 s one-shot redial timer that keeps calling
`esp_wifi_connect()` until the AP returns; a persistent outage past 1 h
triggers a last-resort reboot (skipped while any charging session is active —
same rule as OTA `REBOOT`), which lands in the existing boot-time
portal-idle→retry-STA loop. esp-mqtt already owns its own reconnection, so no
backend or MQTT change was needed. Boot-time and portal pre-check semantics
(bounded retry → definite pass/fail) are unchanged. Details in
[FIRMWARE.md](FIRMWARE.md) §1; security angle (persistent-DoS-by-brief-jam,
now closed) in [SECURITY.md](SECURITY.md) §8.4. Build-verified with ESP-IDF
v5.3.3. **OTA'd to the field as of fw `2.7.0-direct`** (2026-08-13, §0.0005),
2.7.0 ships everything in 2.5.0/2.6.0 plus the idle-baseline fix and is the
first of this line actually running on the fielded gateway; the earlier "not
yet OTA'd" note here was stale. See [FIRMWARE.md](FIRMWARE.md) §"Current".

---

## 0.01 — 2026-08-02 offline/unmetered-consumption detection (fw 2.4.0-direct)

Real incident: an operator manually toggled a P110 on/off (physical button /
Tapo app) while its gateway was fully unreachable, and the platform never
caught the unbilled energy — the gateway's own session-relative `energy_kwh`
integrator only advances while its driver is actually polling, so a
gateway-offline gap is invisible to it entirely. Closed with a two-layer
detector built on the P110's own `today_energy`/`month_energy` counters
(maintained on the plug, independent of the gateway):

- **Firmware (`firmware/main/tapo_protocol.c`
  `tapo_plug_reconcile_idle_baseline`, fw ≥ 2.4.0-direct):** compares fresh
  `today_energy`/`month_energy` readings against a per-plug idle baseline
  persisted in NVS (`"bl_<plug_id>"`) whenever a plug has no active AmpHive
  session; a day/month cross-check tells a calendar rollover/plug reset
  (never reported) from real consumption (month preferred, resets far less
  often). Publishes a one-shot `{"error":"UNMETERED_CONSUMPTION",...}` on
  `/alarms`, idempotent/lossless across an offline gap (the baseline only
  advances once the report is actually delivered). Every telemetry frame now
  also carries `today_kwh`/`month_kwh` for continuous backend cross-checking.
  `tools/p110_sim/` extended (today/month independent accumulators + a new
  `--daily-reset-counter` flag) so this is bench-testable without hardware.
- **Backend (`services/mqtt/telemetry.py` `_persist_telemetry` +
  `services/mqtt/alarms.py`):** a continuous version of the same check on
  every telemetry frame (`plugs.last_today_energy_kwh`/
  `last_month_energy_kwh`, migration `0035_offline_consumption`) — catches
  the first reconnect frame even if the firmware's own report is lost/
  delayed. Both paths raise a `GatewayEvent` (`UNMETERED_CONSUMPTION`,
  severity `warning`) and — new, unlike every other alarm type, which only
  reach the passive `gateway_events` feed — bell-notify every CPO of the
  tenant with the plug name and estimated kWh.

See [MQTT_CONTRACT.md](MQTT_CONTRACT.md) and
[FIRMWARE.md §4a](FIRMWARE.md#4a-offlineunmetered-consumption-reconciliation-fw--240-direct)
for the full design (topic/payload shapes, the reset-vs-consumption decision
rule, threshold constants). Firmware build-verified (ESP-IDF v5.3.3,
`idf.py set-target esp32 && idf.py build`); not yet OTA'd to a fielded
gateway. Backend: 12 new unit tests in `test_mqtt_manager.py` (DB-free,
mocked sessions); the DB-gated migration/schema-drift test suite runs in CI.

---

## 0.02 — 2026-07-22 Razorpay closed-loop "charging credit" reframe (PR #79)

Razorpay rejected onboarding as "wallet services." Rather than remove the
prepaid balance, it was reframed as closed-loop **"charging credit"** —
redeemable only for EV charging on AmpHive, the same RBI closed-system-PPI
exemption ChargeZone and Statiq operate under (Statiq's own T&C names
Razorpay as its processor). Merged to `main` via PR #79 (`chore/razorpay-
closed-loop-charging-credit` @ `850b579`); **deployed** with a clean
Playwright prod smoke.

- **User-facing copy reframed** wallet/add-money/top-up language → "charging
  credit" across marketing, the dashboard, app bar, session monitor, the
  credit page, status labels, and backend ledger/notification strings.
  **Internal identifiers are unchanged by design** — `coin_balance`,
  `WalletContext`, `/api/wallet/ledger`, and the CSS classes still say
  "wallet"; only what the driver reads changed.
- **Route renamed `/wallet` → `/credit`** (`Wallet.jsx` unchanged as the
  page component); legacy `/wallet` and `/topup` both redirect to `/credit`.
  See [ARCHITECTURE.md §4](ARCHITECTURE.md).
- **New public `/terms` page** (`Terms.jsx`) — a Charging Credit Terms page
  with the closed-loop clause, linked from the marketing footer and the
  credit page.
- `plans/remove-wallet-per-session-payment.md` scopes a **Path B fallback**
  (drop the prepaid model for straight per-session payment) if Razorpay
  still rejects the resubmission — not built, kept as a contingency plan.
- **Still pending on a human:** Razorpay resubmission under MCC 5552 with
  the reframed terms; the outcome (approved / still-rejected → Path B) is
  not yet known as of this writing.

550 frontend tests green.

---

## 0.05 — 2026-07-21 audit-backlog batch: batched plug pricing, passlib → bcrypt, self-hosted fonts + tighter CSP

Three audit items deferred from the 2026-07-20 batches as "needs dedicated
careful work", now done:

- **Plug-list pricing N+1 (the audit money-path perf item):**
  `GET /api/plugs/available` and the unauthenticated `GET /api/plugs/public`
  resolved the tariff chain per plug (up to 5 queries each). Both now use
  `services/pricing.resolve_price_display_batch` — two fixed IN-queries
  (tariffs, then slots of the winning tariffs) for the whole list; the chain
  inputs (plug/group tariff ids, tenant) already ride the endpoints' joins.
  Per-plug results are identical (same fallback chain incl. dangling-id
  fallthrough, tenant timezone, same-local-day "next price" rule), and the
  whole list prices at one consistent instant. Single-plug
  `GET /api/plugs/{id}` keeps the scalar resolver. DB-free coverage:
  `tests/test_pricing_batch.py`.
- **passlib replaced with direct pyca/bcrypt (audit dependency-health):**
  passlib is abandoned upstream and pinned us to `bcrypt==4.0.1`;
  `services/auth.py` now calls `bcrypt` directly (`>=4.1`). Hashes are the
  same `$2b$12$…` — every pre-swap hash keeps verifying, and passlib's silent
  72-byte truncation is now explicit (a >72-byte password registered under
  passlib still logs in). Coverage incl. a frozen passlib-era hash:
  `tests/test_auth_hashing.py`.
- **CSP third-party style/font origins dropped (the CSP-saga follow-up):**
  Inter is self-hosted via `@fontsource/inter` and Leaflet CSS is bundled
  from node_modules (map markers are `divIcon`, so Leaflet's default-icon
  path detection is never used) — `unpkg.com` / `fonts.googleapis.com` /
  `fonts.gstatic.com` are gone from both the frontend and the Caddy CSP in
  `deploy.ps1` (now `style-src 'self' 'unsafe-inline'; font-src 'self'
  data:`). Per the 2026-07-20 lesson, smoke-test the CSP in a real browser
  (Playwright) right after the next deploy: map + fonts render, Razorpay
  modal opens, zero CSP violations.

Still open from the audit backlog: split the `cpo.py` god router and the
`MQTTManager` god object; a backend deps lockfile. (Frontend code-splitting
shipped with redesign v3, §0.0 below.)

**2026-07-21 — CI ruff full-lint / mypy / coverage beyond F401, closed:**
`pyproject.toml` now carries `[tool.ruff]` (E/F/W + isort `I`, `line-length =
100`; `E501` ignored — ~240 hits even at 100 cols, mostly one-line
`mapped_column(...)` declarations in `database/models.py` with no autofix
available, so enforcing it would mean hand-wrapping hundreds of lines for no
readability win) and `[tool.mypy]` (lenient — `ignore_missing_imports`, no
strict flags — `follow_imports = "silent"`, scoped via `files` to the 39
`backend/services`/`backend/routers` modules that pass today; the other 18
have 149 real errors — untyped SQLAlchemy `Result.rowcount`/mixin-attribute
access from the `MQTTManager` split, `Decimal`/`float`/`Optional[int]`
mismatches in pricing/tariffs — TODO-listed in the config, not silently
exempted). CI (`ci.yml`) now runs `ruff check backend/`, `mypy`, and `pytest
--cov=backend --cov-fail-under=66` (2 pts below the 68.39% measured
2026-07-21 with DB-gated tests skipping locally; real CI coverage, which runs
those tests against the Postgres service, is higher).

---

## 0.0 — 2026-07-21 frontend redesign v3 (branch `redesign/ui-v3`, PR #77 merged @ `18723b0`; PR #78 shipped same-day after)

Ground-up rebuild of every UI surface into **three role portals on one
bundle** — driver app + marketing homepage (`data-theme="day"`, warm light),
CPO console (`data-theme="volt"`, charcoal/amp-lime), and a **new platform-admin
portal** (`/admin/*` on the CPO host, violet accent) over the new
`/api/admin/*` router. Hand-rolled two-theme design system
(`frontend/src/styles/tokens.css` + primitives; zero CSS/JS CDNs — CSP
unchanged), shared component kit (`components/ui/`: Modal/ConfirmDialog/
Toast/Tabs/DataTable/Empty-vs-ErrorState/StatusDot/Money), `lucide-react`
icons replace emoji, Bricolage Grotesque + JetBrains Mono join Inter
(all @fontsource self-hosted). Route-level code-splitting via `React.lazy`
(closes that audit-backlog item), lazy Razorpay (no longer render-blocking in
`<head>`), PWA manifest, boot splash instead of the blank-screen auth gate,
API-client timeout + 401 location preservation. New driver IA: `/wallet`
(replaces `/topup`), `/activity` (replaces `/history`), unified `/map`,
`/account`, split `/signup`; console IA: `/cpo/chargers|health|pricing`
(replace plugs/faults/tariffs; old paths redirect). Machine-string → human
copy centralized in `utils/statusCopy.js`; money is ₹-first via
`utils/money.js`; plug availability now 5 states (maintenance split from
offline, unpowered de-colliding from in-use). Old pages/`global.css` deleted.
Frontend tests rewritten alongside: **512 Vitest tests / 61 files** (was 127).

## 0.1 — 2026-07-20 audit + quick-wins hardening (PR #72; deployed 2026-07-20, backlog batch PR #75 deployed same day)

A 7-level codebase audit (architecture / security / quality / performance /
testing / dependencies / docs) surfaced **68 findings (0 Critical, 2 High, 24
Medium, 30 Low, 12 Info)**. The low-effort/high-impact subset shipped as **PR
#72 (merged to `main` @ `7669bce`)** and was deployed to prod the same day —
followed by PR #74 (`fix/deploy-csp-origins`), which corrected the CSP after a
Playwright smoke-test on prod caught it blocking Leaflet/Inter/Razorpay-risk
scripts.

What merged:
- **Security:** reset-link default → `https://amphive.app` (was the retired duckdns); Socket.io `connect` now enforces the `token_version` epoch (revoked/logged-out tokens can no longer open a realtime channel — closes the gap vs the HTTP path); `http(s)://8.231.81.12` removed from the CORS + Socket.io allowlists; `amount_inr` bounded at the schema (`Field(gt=0, le=10000, allow_inf_nan=False)` — NaN was slipping past the manual guard); **CSP + `X-Frame-Options: DENY` + `nosniff` + HSTS `includeSubDomains`** added to both Caddy vhosts.
- **Performance:** the outbound MQTT publishes (`send_plug_interval`/`send_plug_limits`, and `send_plug_command` from session-start) no longer block the asyncio event loop on the broker PUBACK — a slow/reconnecting broker no longer freezes the backend (**audit High**). New composite indexes on `charging_sessions` (`plug_id/status`, `user_id/status`, `tenant_id/started_at`) and `ledger_transactions` (`user_id/created_at`) — Alembic `0024_session_ledger_indexes`, idempotent `CREATE INDEX IF NOT EXISTS`, mirrored in the ORM `__table_args__`.
- **Quality / CI:** the hold-exhausted auto-stop notification is no longer mislabeled "Charging complete" (`session_lifecycle` now matches `"exhausted"`); **408 dead imports swept** (ruff F401) and **CI now gates `ruff check --select F401 backend/`**.
- **Deps:** `frontend/Dockerfile` → `npm ci`; `uvicorn[standard]`; `tapo<1.0`.

**Deploy caveats:** the browser-smoke-test lesson (a CSP must be verified with
Playwright, not curl) came from this batch's deploy — see PR #74; migration
`0024` is idempotent and safe to re-run.

**Audit backlog as of this batch — since resolved elsewhere:** the 2nd audit
High (Razorpay signature/webhook credit path had **zero tests**),
`python-jose>=3.4.0` (CVE floor) and the overlay/duckdns **doc drift** all
shipped in PR #75 (2026-07-20); the `/api/plugs` N+1 tariff resolution and the
passlib replacement shipped 2026-07-21 (§0.05 above), and frontend
code-splitting shipped with redesign v3 (§0.0); CI ruff full-lint / mypy /
coverage beyond F401 also shipped 2026-07-21 (§0.05 above). Still open: split
the `cpo.py` god router + `MQTTManager` god object; a backend deps lockfile.

---

## 1. Status matrix

### Backend
| Capability | Status | Notes |
|------------|:------:|-------|
| REST API (auth, groups, plugs, sessions, payments, direct, CPO portal) | ✅ | 43 endpoints (2026-07-10: CPO events feed `GET /api/cpo/events` + `POST /api/cpo/events/{id}/ack`, unified `GET /api/wallet/ledger`, public `GET /api/config`, `GET /api/cpo/analytics/sessions.csv` export) — see [API_REFERENCE.md](API_REFERENCE.md) |
| JWT auth + bcrypt | ✅ | Env-configurable expiry (`JWT_EXPIRY_DAYS`, default 7), **sliding** since 2026-11-15: `GET /api/auth/me` returns a re-minted token the SPA stores, so only a device idle that long signs in again (renewal capped at `JWT_MAX_SESSION_DAYS`, default 90, from the original sign-in); user loaded fresh per request. **Revocable** via the `token_version` epoch (`tv` claim, re-checked per request; `POST /api/auth/logout-everywhere` bumps it: "log out everywhere") and, since 2026-11-15, **per device**: every sign-in mints a `sid` claim (kept across renewals) and `POST /api/auth/logout` revokes only that session via `revoked_sessions` (a sid-less legacy token falls back to the global bump); see the 2026-11-15 section above. **Rate-limited** (2026-07-11): login/register enforce a per-IP sliding window (`LOGIN_RATE_LIMIT` 10/60s, `REGISTER_RATE_LIMIT` 10/3600s → 429 + Retry-After; `services/rate_limit.py`), closes SEC §8.6. **Per-account rate limiting** (2026-08-02): a second, account-scoped sliding window layers on top of the per-IP ones above (which can't see one account rotating source IPs), `account_rate_limit_dependency` (keyed by `user.id`) on session start/stop, payment order creation, and CPO offline top-up creation; `login_account_rate_limit_dependency` (keyed by normalized email, no authenticated user yet at `/login`) layered onto the login route, with a 429 message byte-identical to the per-IP one (no account-enumeration oracle). Env-tunable via `*_ACCOUNT_RATE_LIMIT`. **Self-service password reset** (2026-07-20): `POST /api/auth/forgot-password` (enumeration-safe generic 200; single-use token, SHA-256 digest stored in `password_reset_tokens`, migration `0023`; `RESET_TOKEN_TTL_MIN` 30 min) + `POST /api/auth/reset-password` (8-72 rule, bcrypt rehash, `token_version` bump revokes all sessions); both rate-limited (`FORGOT_PASSWORD_RATE_LIMIT` / `RESET_PASSWORD_RATE_LIMIT`); delivery via `services/email.py`, SMTP STARTTLS when `SMTP_*` env is set, else the reset link is logged at WARNING (console fallback). Frontend `/forgot-password` + `/reset-password` pages. **2026-08-11 (PR #118):** the per-account login cap no longer runs as a pre-handler dependency keyed on the submitted email, that let anyone lock a victim out by spamming their address. It now counts **only FAILED attempts inside the handler**: a correct password is verified first, clears the bucket, and is never throttled, while distributed wrong-password brute force stays capped per email (new `record_failure`/`reset` primitives in `services/rate_limit.py`). Also `FORGOT_PASSWORD_EMAIL_RATE_LIMIT` (default 3/3600) keyed on the submitted email and checked *before* the account lookup, so it caps inbox flooding without becoming an enumeration oracle. **2026-08-11 (PR #119): email verification at registration**, `users.email_verified` + `email_verification_tokens` (migration `0037`, mirroring the reset-token machinery). `register()` now creates an UNVERIFIED account and **does NOT auto-login**, returning `{status:"verification_sent"}`; `POST /api/auth/verify-email` consumes the single-use token race-safely (sets verified, bumps `token_version`, issues a JWT so the click logs the user in) and `POST /api/auth/resend-verification` is generic-200 + rate-limited both per-IP and per-email. `login()` 403s an unverified account, checked **after** the password so it never becomes a verified-status oracle. The migration **grandfathers every pre-existing account as verified**, only new signups must verify. |
| "Sign in with Google" | ✅ | **2026-08-02: merged to main (PR #88). LIVE in prod as of 2026-08-11** (verified: `GOOGLE_*` set, `/api/config` `google_login_enabled=true`, `/login` 302s to Google), the previous "stays hidden until `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are set" note was stale. **Account pre-hijacking closed 2026-08-11 (PR #118, HIGH):** first-time linking of a Google identity to a pre-existing password account now invalidates the stored password (fresh unusable hash), bumps `token_version` (revoking the attacker's live sessions) and sets `auth_provider='google'`, so an attacker who pre-registered the victim's unverified email is fully evicted the instant the real owner links via Google. The other half (an unverified email holding a usable credential at all) is closed by PR #119's email verification, above. Backend-driven authorization-code flow (no JS SDK): `GET /api/auth/google/login` sets a short-lived CSRF-nonce cookie (`google_oauth_state`, the app's only cookie; auth stays bearer-JWT-only) and 302s to Google; `GET /api/auth/google/callback` validates the state, exchanges the code, verifies the ID token against Google's JWKS (`google-auth`), then finds-or-creates/links the `users` row (`auth_provider`/`google_sub`, migration `0033_google_identity`, partial-unique `google_sub` index) and 302s to `FRONTEND_ORIGIN/auth/google/callback#token=<jwt>` (fragment, never a query string). No separate identities table: a Google-only signup gets an unusable random bcrypt hash, the same trick as `_DUMMY_PASSWORD_HASH`, so `/api/auth/login` refuses it with zero route changes, and the first Google link to an existing password account takes it over: the stored password is replaced with an unusable hash, `token_version` is bumped and `auth_provider` becomes `google`, whether or not the account is verified (the owner can set a password again via forgot-password). `GET /api/config`'s `google_login_enabled` gates the frontend button; unset `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`GOOGLE_OAUTH_REDIRECT_URI` hides the feature everywhere. Tests: `backend/tests/test_google_oauth.py` (mocked token exchange/verification, no network). |
| Role-based access control | ✅ | Enforced via `services/rbac.py` `require_role(...)` on all `/api/cpo/*` routes (checks the DB role, not just the token) |
| MQTT command publish (ON/OFF) | ✅ | QoS 1, 3 s wait |
| MQTT inbound telemetry/status handling | ✅ | Telemetry updates TelemetryStore and session DB (now incl. `voltage`/`relay`). Status updates gateway state in DB. |
| MQTT ingestion crash-isolation | ✅ | **2026-08-11 (PR #117).** A non-*object* JSON payload (`5`, `[]`, `"x"`, `true`, `null`) parses fine, then hits `payload.get(...)` in a topic handler and raises `AttributeError` on the paho network-loop thread; paho 2.1.0 runs with `suppress_exceptions=False`, so that escape killed the loop thread and **froze ALL MQTT ingestion for every tenant**, telemetry, billing accrual, auto-stop, status, alarms/safety-finalize, discovery, until the backend was restarted. Reachable by any single gateway credential publishing one 1-byte frame to its own topic. `services/mqtt/router.py` `_on_message` now rejects non-dict payloads before dispatch **and** wraps the whole decode+route+handler body in a catch-all `except Exception` that logs and drops just that frame, so no current-or-future handler bug can escape into the paho thread (`BaseException` still propagates). The non-JSON logs path and the oversized-frame gate stay outside the dict guard. Tests: `backend/tests/test_mqtt_payload_guard.py`. |
| MQTT inbound alarm handling + CPO events feed | ✅ | Subscribes `amphive/gateways/+/alarms` (2026-07-10): `_handle_gateway_alarm` maps `{"error"\|"event"}` → severity, persists a `gateway_events` row (tenant resolved from the gateway), and broadcasts a `gateway_alarm` Socket.io event. CPO reads via `GET /api/cpo/events` (filters: `unacknowledged_only`, `severity`) and clears with `POST /api/cpo/events/{id}/ack`. **Verified live in prod 2026-07-10** (synthetic `UNAUTHORIZED_ON` → persisted event id, retrieved + acked via API). **2026-08-02:** `UNMETERED_CONSUMPTION` (offline-consumption detection, above) additionally bell-notifies every CPO of the tenant directly (`notify()`) — the first alarm type to do so, on top of the passive feed every type gets. **2026-10-20:** fw 2.8.0 diagnostics `GATEWAY_BOOT` / `PLUG_UNREACHABLE` / `PLUG_RECOVERED` (§0); the two info ones are stored already acknowledged (`_PRE_ACKED_EVENT_TYPES`), so they never occupy the unacknowledged window the CPO alert strip and Health badge read. |
| MQTT inbound firmware log-line diagnostics (TD#28) | ✅ | Subscribes `amphive/gateways/+/logs` (QoS 0, plain text — not JSON, matched before the JSON parse in `services/mqtt/router.py`). `services/mqtt/logs.py` `MQTTLogsMixin` parses the ESP-IDF level letter (E/W/I/D/V → error/warning/info, skipping a leading ANSI colour escape) and persists a `gateway_logs` row (tenant resolved from the gateway) — no notify/socket fan-out, a diagnostics feed distinct from the alarm/event feed above. CPO reads via `GET /api/cpo/gateways/{gateway_id}/logs`; admin cross-tenant via `GET /api/admin/gateway-logs`. Pruned after `GATEWAY_LOGS_RETENTION_DAYS` (default 14) by the session reaper's `reap_gateway_logs_once()`. |
| Diagnostics UI (TD#28/TD#26) | ✅ | Built 2026-11-17 on `release/2026-11-17-w1`, deployed 2026-11-19. Frontend only, no backend change. CpoGateways "Logs" button opens a drawer (`components/GatewayLogsPanel.jsx`) on `GET /api/cpo/gateways/{id}/logs`: level filter (default warning+error, issued as one request per level and merged, because the endpoint accepts a single level), Load more paging, opt-in 15 s auto-refresh that pauses while the tab is hidden, empty state naming the 14-day retention. Log text is rendered as plain text with ANSI stripped. AdminGateways has a Fleet/Logs tab on `GET /api/admin/gateway-logs` with an optional gateway filter. New CPO page `/cpo/audit` (`pages/cpo/CpoAudit.jsx`) on `GET /api/cpo/audit` with Load more, linked from the CpoLayout Operations nav group. Admin audit page already existed (`AdminAudit.jsx`), unchanged. |
| Operator alerts (idle gateway offline + safety faults) | ✅ | Built 2026-11-17 on `release/2026-11-17-w1`, deployed 2026-11-19. `services/operator_alerts.py`; behaviour, env vars and in-memory limits in section 0d above. **Wave 4 R2 (DUR, built, not deployed):** the offline / back-online state, its cooldown and the pending re-check are durable on `gateways` (migration 0051) and re-armed at startup; the safety-fault cooldown is rebuilt from `gateway_events`. See section 0h. |
| Admin health console | ✅ | Built 2026-11-17 on `release/2026-11-17-w1`, deployed 2026-11-19. `GET /api/admin/health` + `/admin/health`; host cron `deploy/scripts/check_host_health.sh` installed on the VM 2026-11-19 (every 5 min, see DEPLOYMENT.md). |
| Per-session power curve, insights, cheapest window, public status (F2, F8, F6, F11) | ✅ | Built 2026-11-17 on `release/2026-11-17-w1`, deployed 2026-11-19. See section 0d above. |
| Orphan-OFF operator alert | ✅ | `mqtt/status.py._republish_off_for_orphaned_plugs` (reconnect-time OFF republish for plugs with no ACTIVE session) now also notifies every CPO of the gateway's tenant: one `orphan_off` bell notification (`services/notifications.py notify()`) per (plug, CPO) pair, only when at least one plug was actually OFF'd. First CPO-targeted `notify()` call site (previously driver-only); delivered via the existing NotificationBell/Socket.io path, no frontend change needed. **2026-10-20:** the bell now also needs the plug to have been last seen drawing power (`plugs.current_power_w >= ORPHAN_ALERT_MIN_POWER_W`, default 5 W) on top of the existing real-OFFLINE→ONLINE-transition gate (a retained `online` replay never bells). An idle plug still gets its OFF, with only an INFO log (§0a). |
| Plug maintenance workflow (fault console) | ✅ | **2026-07-12**: a THERMAL_CUTOFF/OVERCURRENT_CUTOFF alarm now auto-enters the plug into MAINTENANCE (`MQTTManager._auto_enter_maintenance`; excludes `UNAUTHORIZED_ON` and OTA events; env-gate `AUTO_MAINTENANCE_ON_CRITICAL_ALARM`, default on) — session-start already 409s any non-AVAILABLE plug, so this blocks new sessions until an operator clears it. Operators drive enter/clear via `POST /api/cpo/plugs/{id}/maintenance` (`clear` 409s while an ACTIVE session exists); both paths audit (`plug.maintenance_enter`/`plug.maintenance_clear`) and broadcast the status flip like any other plug-status change. |
| Gateway reachability in the driver plug API | ✅ | `GET /api/plugs/available` and `/api/plugs/{id}` now return `gateway_online` (via `gateway_is_live`: ONLINE + `last_seen_at` within the liveness window), so the driver UI can flag an unreachable charger before a start attempt rather than only via a 409. **Verified in prod** (live gateways → true, never-connected gateway → false). |
| Live gateway connectivity push (faster-gateway-offline-detection) | ✅ | See [proposals/faster-gateway-offline-detection.md](proposals/faster-gateway-offline-detection.md) — levers 1+3 shipped: `mqtt/status.py` emits `plug_connectivity` on every real online↔offline transition, and `services/session_reaper.py reap_gateway_silence_once()` closes the silence-case gap (TCP-connected but telemetry wedged, no LWT) by reusing `gateway_is_live()` per reaper tick with a bounded in-memory dedup (`_silence_pushed`). Dashboard/CpoGateways/CpoChargers all patch in place on the socket event plus a 30s `usePoll` backstop. Lever 2 (tighter keepalive/liveness-window constants) deliberately held pending field data. **2026-10-20:** the offline direction is debounced. It waits `GATEWAY_OFFLINE_NOTIFY_DELAY_SEC` (default 120 s) and is dropped if the gateway is back by then; the online direction still fires at once (§0). The silence sweep only looks at gateways stored ONLINE, so it does not bypass the debounce. |
| Gateway firmware-version tracking | ✅ | The `fw` field on the gateway's `online` status is now persisted to `gateways.firmware_version` (Alembic `0006_gateway_firmware_version`; LWT/offline never clobbers it), exposed by `GET /api/cpo/gateways`, and shown in the CPO plugs table (online dot + `fw <ver>`) so an operator can see which gateways need an OTA. |
| Live-stream staleness flag | ✅ | The Socket.io `telemetry` payload now carries `is_stale`/`age_sec` (age since the last MQTT write vs `TELEMETRY_STALE_AFTER_SEC`, default 15 s), plus `relay_on`/`voltage_v`, so the frontend shows a "reconnecting" banner instead of freezing on stale values. |
| Live telemetry / Socket.io | ✅ | Streams real telemetry from TelemetryStore via Socket.io (sole transport — the legacy SSE endpoint + `sse-starlette` dep were retired 2026-07-07). Automatically triggers the plug to report telemetry at 1s intervals when there are active listeners or an active session. **Correction 2026-07-05:** the stream task called a non-existent `await sio.get_participants(room=...)` API, which raised on every iteration and killed each stream before the first emit — the earlier "fully functional/verified" claim was wrong. Fixed (room-manager membership check) with a regression test in `backend/tests/test_socketio.py`. |
| Time-series telemetry persistence | ✅ | Persistent `telemetry_readings` table fed by a buffered background batch-flush from the MQTT handler (`services/telemetry_persistence.py`); queried by `GET /api/cpo/analytics/telemetry` via `date_trunc`. Plain Postgres (no TimescaleDB) — hypertables/continuous-aggregates noted as a future upgrade; row retention is env-driven (`TELEMETRY_RETENTION_DAYS=90` in prod as of 2026-07-07). Live Socket.io still uses the in-memory TelemetryStore. |
| Razorpay create-order + verify | ✅ | HMAC-verified; credits coins + ledger. Supports decimal INR amounts and coin balances (money columns are `Numeric(12,2)`/Decimal as of 2026-07-06). **2026-07-05:** `/verify` now credits the **Razorpay-confirmed** amount fetched server-side (the client-sent `amount_inr` is deprecated/ignored — it was previously trusted, allowing arbitrary wallet inflation). |
| Razorpay webhook auto-credit | ✅ | Credits coins on `payment.captured`; atomic + idempotent vs. `/verify` (dedupes on the UNIQUE `razorpay_payment_id` via `IntegrityError`). Money columns are `Numeric(12,2)`/Decimal (2026-07-06). |
| Wallet debit on stop + ledger | ✅ | Row-locked (`SELECT ... FOR UPDATE`) in stop/verify/webhook paths |
| Prepaid protection: auto-stop on balance exhaustion | ✅ | On each telemetry write, if the accrued energy cost (`kwh × rate`) reaches the session's exhaustion threshold, the session is auto-stopped via the shared `finalize_charging_session` path (own txn, row-locked, race-safe with a user stop / the reaper). Caps free charging past a drained wallet to ≤ one telemetry interval. **2026-07-12:** the threshold is now the session's own authorization hold (`ChargingSession.hold_coins`) when set, not the driver's whole wallet balance — a concurrent second session may be holding the rest of it (see the "Authorization hold" row below). Legacy `hold_coins IS NULL` sessions keep the old live-wallet-balance threshold. Env-toggle `AUTO_STOP_ON_BALANCE_EXHAUSTED` (default on). Tests in `test_mqtt_manager.py`, `test_auth_holds.py`. |
| Session-sized authorization hold (MARKET_GAP_ANALYSIS.md §3) | ✅ | **2026-07-12:** `POST /api/sessions/start` reserves `min(available_balance, max_kwh × rate)` onto `ChargingSession.hold_coins` (Alembic `0013_auth_holds`, nullable — NULL = legacy pre-hold session), replacing the old flat `MIN_START_BALANCE_COINS` floor. `available_balance` (`services/wallet.py`, a single aggregate query) is `coin_balance` minus the SUM of `hold_coins` across the user's OTHER ACTIVE sessions — computed under the same user-row lock the start path already takes, so concurrent starts can't double-reserve the same coins; a hold never touches `coin_balance` itself (logical reservation only, so the non-negative CHECK and every credit/debit path are unchanged). `finalize_charging_session` now debits `min(final_cost, hold_coins)`, closing the old forgiven-overage revenue leak (SECURITY.md §5) for every held session — unspent hold is simply released, no money moves. `/api/auth/me` and `GET /api/wallet/ledger` additionally expose `available_balance` alongside `coin_balance`. Legacy sessions (`hold_coins IS NULL`) finalize with the exact pre-hold behavior. Tests: `backend/tests/test_auth_holds.py`. **Wave 4 R2 (built, not deployed):** with an energy limit the hold is `min(available, energy_cost(max_kwh, max_rate) + idle_fee_cap)` (C1 idle reserve; C9 member price as the rate), and a limits PATCH keeps the idle cap. See section 0h. |
| Driver notifications (feed + Socket.io + Web Push) | ✅ | 2026-07-11: `notifications`/`push_subscriptions` tables (Alembic `0007`), `services/notifications.py` (persist → Socket.io user room → VAPID Web Push via `pywebpush`, dead subscriptions pruned on 404/410). Emit points: every session stop (user/auto-stop/reaper/safety cutoff), low balance once per session (`LOW_BALANCE_WARN_FRACTION`, default 0.8), gateway-offline mid-session, top-up credit. Endpoints `/api/notifications*` (feed + read + push-subscription CRUD). **Behavior change:** `THERMAL_CUTOFF`/`OVERCURRENT_CUTOFF` alarms now finalize the plug's ACTIVE session immediately (bills recorded energy, frees the plug) instead of leaving it for the reaper. Tests in `test_notifications.py`. **Verified live in prod 2026-07-11** (migration `0007` applied at startup; billed fake-plug session #24 → `session_stopped` notification with the exact receipt figures (0.234 kWh / 1.17 coins / 497.79 left), unread_count 1 → read → 0; push public-key endpoint `enabled:true`). Browser-side push delivery needs a manual browser opt-in — not yet exercised. |
| "Notify me when free" plug watches | ✅ | **2026-07-12:** one-shot watch on an occupied/offline plug (the society/office "someone's on my charger" case). `plug_watches` table (Alembic `0014_plug_watches`; UNIQUE `(user_id, plug_id)` + per-plug index), armed/disarmed via `POST`/`DELETE /api/plugs/{id}/watch` (both idempotent; POST enforces the same private-group access rule as `GET /api/plugs/{id}` via the shared `ensure_plug_group_access` helper, and 409s only a plug that is startable *right now* — AVAILABLE with a live gateway; occupied/offline/maintenance are all watchable). When the plug flips back to AVAILABLE — `finalize_charging_session` (the just-stopped driver excluded) or the CPO maintenance-clear — `services/plug_watch.py notify_watchers_plug_available` sends each watcher a `plug_available` notification through the existing pipeline (feed + Socket.io + Web Push) and deletes exactly the fired rows (one-shot; never raises into the billing path, rolls the shared session back on failure). Plug list/detail responses carry a per-caller `watching: bool` (ONE extra query for the whole list — the endpoint stays N+1-free); the Home plug card shows a bell toggle on every non-startable plug (optimistic, reverts on failure, cleared live by the `plug_status→available` broadcast). Tests: `backend/tests/test_plug_watch.py` (24), Home bell cases in `Home.test.jsx`. |
| Per-CPO/per-site tariff model + time-of-day pricing | ✅ | New `Tariff` model (`tenant_id`, `name`, `price_per_kwh` `Numeric(12,2)`; Alembic `0010_tariffs`), resolved plug → its charger group → the tenant's default tariff → the legacy global `COINS_PER_KWH` env var (`services/pricing.py resolve_rate_for_plug`), SNAPSHOTTED onto `ChargingSession.rate_coins_per_kwh` at session start so a later tariff *edit/reassignment* never changes an in-flight/already-billed session. CPO CRUD + assign/unassign at `/api/cpo/tariffs*` (tenant-scoped; cross-tenant assignment rejected); `price_per_kwh` exposed on the plug list/detail API and the session receipt. **Pricing v2 — time-of-day + segmented billing (Phase 2 wired 2026-07-14, docs/PRICING_V2_SPEC.md):** a `Tariff` can now carry `tariff_slots` (minute-of-day windows in the tenant's timezone). `resolve_rate_window` returns the in-effect rate **and** the wall-clock boundary it next changes at; session start snapshots that boundary and sizes the auth hold at the worst-case rate over the session window (`max_rate_over_window`). All three billing paths bill via `services/billing.py session_cost` (frozen closed-segment cost + open segment at the current rate), and `reprice_session_if_due` closes a segment **forward-only** when the boundary passes — driven by the `_persist_telemetry` frame hook (~1 s), an operator-edit trigger *(still Phase 3)*, and the session-reaper backstop — emitting a `rate_changed` driver notification. A **flat tariff resolves no boundary → one segment → bills byte-identically to before** (safe to deploy before any slot exists). **Phase 4 shipped 2026-07-14:** operator TOD-slot editor at `/cpo/tariffs` (list/create/delete tariffs + add/remove `tariff_slots` sub-resources `GET/POST/PUT/DELETE /api/cpo/tariffs/{id}/slots`, window+overlap validated via `services/pricing.py slot_overlaps`; tenant timezone shown read-only from `GET /api/cpo/profile`), and driver current+next price — `PlugResponse.price_next_per_kwh`/`price_changes_at` (`resolve_price_display`, one slot-load per plug, no new N+1) rendered as the Home ribbon's "→ 6 @ 18:00" hint. **Phase 3 (2026-07-14):** an operator tariff/slot edit reprices already-ACTIVE tenant sessions forward-only — `services/pricing.py mark_tenant_sessions_for_reprice` stamps `rate_valid_until=now` from the tariff update/delete + slot create/update/delete endpoints (env `AUTO_REPRICE_ACTIVE_SESSIONS`, default on), and the frame hook/reaper then reprice + notify where the rate moved; PATCH-`/limits` sizes the hold at `max_rate_over_window`. (Tariff *reassignment* still uses the start snapshot by design.) Tests: `backend/tests/test_pricing.py`, `test_pricing_v2.py`, `test_telemetry_store.py`; frontend `CpoTariffs.test.jsx`, `Home.test.jsx`. **Wave 4 R2 (built, not deployed):** a tariff can carry opt-in idle-fee terms (`0052_idle_fee`, [IDLE_FEE.md](IDLE_FEE.md)), and a private-group member can have a flat per-member price that beats every tariff and slot (`0054_member_price_invites`). See section 0h. |

| User-set charging limits + backend auto-stop ([MARKET_GAP_ANALYSIS.md](MARKET_GAP_ANALYSIS.md) §5 "Stop at target kWh") | ✅ | **2026-07-12:** `POST /api/sessions/start`'s `max_kwh`/`max_duration_seconds` (long accepted, forwarded to the gateway in the MQTT ON payload, and enforced by the firmware as local relay watchdogs — but never sent by the UI, and the firmware publishes NO alarm on those cutoffs, so a firmware cutoff left the session ACTIVE until the reaper) are now SNAPSHOTTED onto `ChargingSession.max_kwh`/`max_duration_seconds` (Alembic `0015_session_limits`, nullable — NULL = legacy pre-limit session, never limit-auto-stopped) and mirrored backend-side: on every telemetry persist, `MQTTManager._maybe_auto_stop_on_limits` finalizes via the shared `finalize_charging_session` path at `energy_kwh >= max_kwh` ("auto-stopped: energy limit reached") or elapsed `>= max_duration_seconds` ("auto-stopped: time limit reached") — telemetry is ~1 s apart during a session, so the stop lands within ~1 s of the limit; sequenced after the balance-exhaustion check (its reason wins a same-frame tie), race-safe via finalize's row-locked ACTIVE re-check. The session reaper adds a duration **backstop** sweep (`reap_time_limited_once`, same reason string). Env toggle `AUTO_STOP_ON_LIMITS` (default on, honored by both paths). Hold sizing already used the request's `max_kwh`, so a small user limit automatically shrinks the auth hold too. Limits exposed on the start response, `GET /api/sessions/active`, and the finalize receipt; the session-stopped notification maps the new reasons ("Charging stopped — your limit was reached", reason in the body). **Opt-in by default (2026-08-02):** `SessionStartRequest`/`QueueChargeRequest` now default `max_kwh`/`max_duration_seconds` to `None` instead of `30.0`/`14400` — a bare start persists NULL/NULL and runs until the driver (or a real safety net: balance exhaustion, gateway-offline reaping, overcurrent, plug caps) stops it. NULL is excluded from every auto-stop check above by construction, so this needed no new gating logic — only the schema defaults changed, plus resolving NULL to a large `UNLIMITED_DURATION_SECONDS`/`UNLIMITED_MAX_KWH` sentinel (`services/mqtt_manager.py`) before publishing `ON`/`SET_LIMITS`, since neither the firmware nor the AmpHive Agent software gateway has a safe wire representation for "no limit" (omitted field → their own old hard default; `0` → an instant on-device cutoff on the very next poll). The authorization hold for a `max_kwh=None` session is sized at the available balance alone (`services/session_start.py begin_active_session`), which is exactly what keeps balance-exhaustion auto-stop working as the real safety net. Tests: `backend/tests/test_session_limits.py`. |
| Session dispute / refund flow (coins-only) | ✅ | Driver files one at a time per session (`POST /api/sessions/{id}/dispute`, finished sessions only; a partial unique DB index backs the one-OPEN-dispute-per-session rule). The owning CPO reviews tenant-scoped (`GET /api/cpo/disputes`) and resolves (`POST /api/cpo/disputes/{id}/resolve`): approve credits the driver's wallet via `services/wallet.credit_wallet` + a reconciling `REFUND` ledger row (cumulative refunds capped at the session's `coins_spent`, row-locked against concurrent resolves); reject just records a note. No Razorpay money-out. Tests: `backend/tests/test_disputes.py`. **UI shipped 2026-07-16:** driver files via a "Report an issue" action on each History session row → `DisputeModal` (reason ≥10 chars, inline 409 for an already-open dispute); the owning operator reviews/resolves on the new `/cpo/disputes` page (status filter, Approve/Reject with confirm, inline error). Tests: `DisputeModal.test.jsx`, `CpoDisputes.test.jsx`, History dispute-modal case. |
| ~~Direct Mode Tapo endpoints~~ | — | **Removed 2026-08-02** — dev/test-only ESP32-bypass path (`DIRECT_MODE`, `backend/routers/direct.py`, `backend/services/tapo_direct.py`), unused by frontend/agent/tools and defaulted off; see [ARCHITECTURE.md](ARCHITECTURE.md). |
| CPO payout / settlement ledger | ✅ | **Record-keeping only — no bank/UPI/payment-gateway integration** (money still moves out-of-band; the admin marks a payout PAID after the transfer). `Payout` model (Alembic `0009_payouts`) + `services/payouts.py` compute a tenant's gross/fee/net earnings (`SUM(coins_spent)` of its COMPLETED sessions, single aggregate query on the already-denormalized `charging_sessions.tenant_id`) and a rolling settlement watermark (`MAX(period_end)` over non-CANCELLED payouts). `GET /api/cpo/earnings` (lifetime + unsettled), `POST`/`GET /api/cpo/payouts` (request/list; 400 if nothing unsettled, 409 if a request is already pending, race-safe via a `SELECT ... FOR UPDATE` tenant-row lock so concurrent requests can't double-settle the same window), admin-only `POST .../mark_paid`, and owner-or-admin `POST .../cancel` (frees the window). **2026-07-12:** flipped 🟡→✅ — the CPO-portal Earnings & Payouts page shipped (see the Frontend table below), and all three payout money ops now write `payout.request`/`payout.mark_paid`/`payout.cancel` rows into the TD#26 audit trail (`services/audit.py`, tenant = the payout's tenant so an admin's action lands in the owning CPO's `GET /api/cpo/audit`; detail carries the window + gross/fee/net). Closes [requirements.md §5.1](../requirements.md)'s "withdraw earnings" promise at the record-keeping level; see [MARKET_GAP_ANALYSIS.md](MARKET_GAP_ANALYSIS.md) §1.6/§7. Tests: `backend/tests/test_payouts.py` (incl. audit assertions). **2026-07-21 — CPO offline (cash) top-ups added:** `OfflineTopup` model (Alembic `0026_offline_topups`, also `ALTER TYPE`-extends `tx_type` with `cpo_topup` — the first enum extension in this repo) + `services/payouts.py tenant_earnings_summary`'s new `available_pool_coins` (unsettled net minus top-ups already issued since the watermark) let a CPO manually credit a driver's wallet for cash collected offline via `POST /api/cpo/topups` (409 over-pool, row-locks the same tenant row `POST /api/cpo/payouts` locks so the two serialize), with `GET /api/cpo/topups` history and the figure surfaced on `GET /api/cpo/earnings` as `topup_pool`. `POST /api/cpo/payouts` now pays out that reduced figure so the same earnings can never be drawn out twice. Tests: `backend/tests/test_offline_topups.py` (pool math, the payout-watermark interaction incl. a real-concurrency race test, 409/404s, ledger reconciliation, tenant scoping). |
| Plug reservations (bookable time slots) | ✅ | **2026-07-12** (feat/reservations — the first prospective client's private society/office use case): new `Reservation` model + `reservation_status` enum (Alembic `0016_reservations`, renumbered from 0014 at merge — `0014_plug_watches`/`0015_session_limits` landed first; `tenant_id` denormalized like `charging_sessions`; half-open `[start_at, end_at)` windows; index `(plug_id, start_at)`). **Free in v1 — no coin hold.** `POST /api/reservations` (same plug-access rule as session start; plug not MAINTENANCE; window 15 min–`RESERVATION_MAX_HOURS` (4), start ≤ `RESERVATION_MAX_ADVANCE_DAYS` (7) ahead / ≥ now−2 min; per-user cap `MAX_UPCOMING_RESERVATIONS_PER_USER` (2) of BOOKED-with-future-end → 409; `[start,end)` overlap vs BOOKED on the plug under a `SELECT ... FOR UPDATE` plug-row lock → 409; confirmation notification), `GET /api/reservations/my` (upcoming + last-20 history), `POST /api/reservations/{id}/cancel` (owner or owning tenant's cpo/admin — operator cancel notifies the driver; anyone else 404), `GET /api/plugs/{plug_id}/reservations` (the schedule, for anyone with plug access), tenant-scoped `GET /api/cpo/reservations`. **Session-start gate** (inside the plug-locked section of `start_charging_session`): lazy no-show expiry FIRST (`services/reservations.py expire_lapsed_reservations`, shared by the gate/overlap/list paths — a BOOKED row past `start_at + RESERVATION_NO_SHOW_GRACE_MIN` (15) flips EXPIRED, so no background sweep is needed), then a covering BOOKED window 409s a non-holder ("Plug is reserved until <end>") while the holder's start marks it FULFILLED + links `session_id` (reverted to BOOKED if the MQTT publish fails). Plug list/detail APIs carry `reserved_now`/`reserved_now_by_me`/`reserved_until`/`next_reservation` (list computes them in ONE grouped query — stays N+1-free). Reservations ≠ scheduled start — nothing switches the plug on at `start_at`. **2026-07-13 — the noted `SessionReaperService` follow-up shipped** (`reap_reservation_starts_once`, a third sweep alongside the stale/duration ones): when a window opens it nudges the holder once ("Your reservation has started", idempotent via the new nullable `Reservation.started_notified_at`, Alembic `0017_reservation_started`, claimed under a `SELECT … FOR UPDATE SKIP LOCKED` so a racing holder-start never blocks the janitor) **and force-stops any non-holder session still running on the plug** — the walk-up overrun the start gate structurally can't catch (that session started legally *before* the window; the gate only 409s *new* non-holder starts). The force-stop goes through the shared `finalize_charging_session` (own txn, row-locked, bills persisted energy, frees the plug) with an `"auto-stopped: plug reserved"` reason that routes finalize's stop notification to a "Charging stopped — plug reserved" title; env `RESERVATION_FORCE_STOP_WALKUP` (default on) gates the stop, the holder nudge fires regardless. To make that stop fair, `start_charging_session` also warns a walk-up up front (best-effort, off the plug-locked section) when it starts on a plug another member has booked within the session's projected window (`services/reservations.py next_conflicting_reservation`, env `RESERVATION_WALKUP_WARN_LOOKAHEAD_MIN` default 180). Tests: `backend/tests/test_reservations.py`, `backend/tests/test_reservation_reaper.py`. **Wave 4 R2 (C8, built, not deployed):** per-plug approval mode (`pending` / `declined`) and an optional capped no-show fee charged by the reaper sweep (`0055_reservation_approval`); see [RESERVATION_APPROVAL.md](RESERVATION_APPROVAL.md) and section 0h. |
| Circuit caps + admission control | ✅ | **2026-07-14** (feat/caps-admission-control — backend foundation): two nullable amperage columns (Alembic `0019_current_caps`) — `plugs.max_current_a` (per-plug cap; NULL = env `DEFAULT_PLUG_CAP_A`, 16 A P110 cutoff) and `charger_groups.max_current_a` (the shared circuit/line capacity; NULL = no limit). `services/caps.py check_circuit_admission` gates `start_charging_session` under the plug lock (+ a group-row lock so concurrent same-circuit starts serialize): a start is admitted iff Σ(effective caps of the group's already-ACTIVE plugs) + the new plug's cap ≤ the circuit limit → else 409 with the free-amps figure. **Hard guarantee at the 16 A default** (a plug can't exceed its cap; no modulation — P110 is relay+meter); a SUB-16 A per-plug cap is admission-advisory until the firmware watchdog enforces it via an ON-payload current threshold (pending OTA). Env `ENFORCE_CIRCUIT_ADMISSION` (default on). Operator setters on `PUT /api/cpo/plugs/{id}` + `PUT /api/cpo/groups/{id}` (`max_current_a`, send 0 to clear); `GET /api/cpo/plugs` returns the plug cap and `GET /api/cpo/groups` returns `max_current_a` + live `current_load_a`. A ChargerGroup doubles as the circuit (one-society-one-line primary case; a dedicated Circuit entity is the upgrade path). **Operator UI shipped 2026-07-14** (feat/caps-operator-ui): the CpoGroups edit modal sets the circuit capacity (A) and each group card shows live `current_load_a / max_current_a`; the CpoPlugs edit modal sets the per-plug "Max current (A)" (blank = default) — both PUT `max_current_a` (0 clears). **Request-capacity flow shipped 2026-07-14** (feat/caps-request-capacity): a start refused by admission returns a structured `409 {code:"circuit_full"}` (the frontend api client now surfaces `err.code`), and `ChargeSetupModal` offers a **"Request capacity"** button → `POST /api/plugs/{id}/request-capacity` arms a one-shot `capacity_requests` row (Alembic `0020`, UNIQUE user+plug, mirrors `plug_watches`). When the circuit next has room — `finalize_charging_session` (a session ended) or the operator raising the cap (`PUT /api/cpo/groups`) — `services/capacity.py notify_capacity_available` fans out a `capacity_available` notification to every requester whose plug now fits and clears those rows (first-come-first-served; the start-time admission gate arbitrates the actual starts). Operators see demand on the Groups page (`pending_capacity_requests` count → "⚡ N waiting for capacity"). Only remaining: firmware sub-16 A per-plug enforcement (pending OTA — admission is a hard guarantee at the 16 A default regardless). **Operator-facing caveat (be explicit with CPOs):** lowering a plug's per-plug cap below the 16 A hardware default (e.g. 16 A → 8 A) is *admission math only* — it is NOT enforced on the P110, which still draws to its 16 A cutoff, so a shared circuit can be overloaded even though the operator set a lower cap. The circuit-protection guarantee is HARD only when every plug sits at the 16 A default; sub-default caps are advisory until the firmware watchdog OTA ships. The CpoPlugs / CpoGroups edit modals now surface this inline (feat/caps-honest-ui). Tests: `backend/tests/test_caps.py` (admission + fan-out), frontend `CpoPlugs.test.jsx`, `ChargeSetupModal.test.jsx`. |
| GST tax invoices | ✅ | **2026-07-12**: new `Invoice` model + four GST columns on `Tenant` (`gstin`, `legal_name`, `invoice_prefix`, `next_invoice_seq`; Alembic `0012_gst_invoices`). `services/invoices.py issue_invoice_for_session` is idempotent (one invoice per session, UNIQUE `session_id` + `IntegrityError` catch backs the pre-check, not just the pre-check itself) and computes an INCLUSIVE GST split off `ChargingSession.coins_spent` (`taxable_value = total / (1 + rate/100)`, `gst = total - taxable_value`, both via `to_money` so the two always foot to the cent; rate from env `GST_RATE_PCT`, default 18.0) with immutable seller (tenant legal name + GSTIN) and line (`energy_kwh`, rate) snapshots so a later `Tenant` edit never rewrites an issued invoice. `invoice_number` (`{tenant's invoice_prefix, or a tenant-scoped fallback if unset}-{FY}-{seq:05d}` — the fallback avoids two unconfigured tenants colliding on the same globally-unique number) is allocated under a `SELECT ... FOR UPDATE` on the tenant row so concurrent issues never collide. `GET /api/sessions/{id}/invoice` (driver who owns the session, or cpo/admin of the tenant; issues on first call; `?format=html` for a minimal printable copy, no PDF dependency) and tenant-scoped `GET /api/cpo/invoices`. **India intra-state GST only** — CGST/SGST vs. IGST is not split out (no geo-verification of driver/CPO state). **UI shipped 2026-07-16 (🟡→✅):** a CPO **Settings** page (`/cpo/settings`) configures GSTIN/legal name/invoice prefix (extended `PUT`/`GET /api/cpo/profile` — the seller fields were add-only there), a CPO **Invoices** page (`/cpo/invoices`) browses issued invoices with a per-row printable "View", and the driver **History** sessions table gains an "Invoice" action (authenticated raw-fetch of the `?format=html` copy, shown only for completed/paid sessions that billed > 0). Tests: `CpoSettings.test.jsx`, `CpoInvoices.test.jsx`, History invoice case, `test_invoices.py` profile-schema cases. See [MARKET_GAP_ANALYSIS.md](MARKET_GAP_ANALYSIS.md) §1.5/§3. |
| Platform admin API (`/api/admin/*`) | ✅ | **2026-07-21 (redesign/ui-v3):** `backend/routers/admin.py` — cross-tenant stats/tenants/users/payouts/gateways/disputes/audit reads + user role/disable (`users.is_disabled`, Alembic `0025_user_disable`; enforced at login AND in `get_current_user` with 403 `account_disabled`, token_version bumped on disable/demote, 403 on self-demote/self-disable) + signed wallet adjust-balance (row-locked, 0-floored, ledgered, audited). `AuditLog.tenant_id` relaxed to nullable for platform-level rows. All `require_role("admin")`; house `{total, items}` pagination. See [API_REFERENCE.md](API_REFERENCE.md#platform-admin-console-apiadmin-routersadminpy-redesignui-v3). **Admin console frontend shipped alongside** (`pages/admin/*`, `/admin/*` on the CPO host, Phase E) — platform overview, tenants, users, payouts, gateways, disputes, audit — with PR #77's merge (@ `18723b0`), not left in-progress. Tests: `backend/tests/test_admin_router.py`, frontend `AdminLayout.test.jsx`/`AdminOverview.test.jsx`/`AdminTenants.test.jsx`/`AdminTenantDetail.test.jsx`/`AdminUsers.test.jsx`/`AdminPayouts.test.jsx`/`AdminGateways.test.jsx`/`AdminDisputes.test.jsx`/`AdminAudit.test.jsx`. |
| Preflashed-unit claim-code onboarding | ✅ | **2026-08-02** (feat/easy-provisioning): replaces "operator hand-registers every gateway" with self-serve claiming. `gateways.tenant_id` relaxed to nullable (Alembic `0034_gateway_claim_code`, mirrors the `users.tenant_id` nullable-FK precedent) + new `claim_code` (partial-unique index, `WHERE claim_code IS NOT NULL`, mirrors `uq_users_google_sub`) and `claimed_at` columns. Admin mints inventory: `POST /api/admin/gateways/inventory` creates an unclaimed row (`tenant_id NULL`) with a fresh 10-char code from an unambiguous alphabet (`23456789ABCDEFGHJKMNPQRSTUVWXYZ` — no `0/O/1/I/L`, collision-retried against the UNIQUE index); `GET /api/admin/gateways/inventory` lists them (claimed/unclaimed filter) — separate from `GET /api/admin/gateways`'s existing tenant-fleet view (inner-joined on `Tenant`, so it can't show `tenant_id IS NULL` rows). CPO/buyer claims: `POST /api/cpo/gateways/claim` (`backend/routers/cpo/_gateways.py`) looks up the normalized code (whitespace/dashes stripped, uppercased), sets `tenant_id`/`claimed_at`, and returns the SAME generic 404 for "code not found", "already claimed", and malformed input — no enumeration oracle (mirrors the `forgot_password` generic-response reasoning, `routers/auth.py`) — and does the lookup query regardless of branch outcome for roughly constant-time behavior. Rate-limited via the existing `account_rate_limit_dependency` (`services/rate_limit.py`, new `cpo_gateway_claim_account_rate_limiter`, default 10/60). CPO portal: `CpoGateways.jsx`'s primary "Add gateway" modal is now the claim-code form; the old gateway-ID/name form was behind a secondary "Register a gateway manually" link, **removed 2026-11-16** (`POST /api/cpo/gateways` is now admin-only; the operator path is admin inventory mint followed by a CPO claim, SECURITY.md section 8.8). Operator runbook: [deploy/docs/preflashed_unit_runbook.md](../deploy/docs/preflashed_unit_runbook.md); buyer-facing card: [deploy/docs/buyer_setup_card.md](../deploy/docs/buyer_setup_card.md). Out of scope by design: automatic Mosquitto broker-credential provisioning (still `add_gateway_user.ps1`, run once per unit at flash time). Tests: `backend/tests/test_gateway_claim.py` (mint, claim, double-claim, foreign-tenant, bad-code, rate-limit wiring — mocked-DB unit tests plus a `TEST_DATABASE_URL`-gated integration section), frontend `CpoGateways.test.jsx`. |

### Frontend
| Capability | Status | Notes |
|------------|:------:|-------|
| Login/register, protected routes | ✅ | **Behavior change 2026-08-11 (PR #119):** registering no longer signs you straight in, Register shows a "check your email" confirmation with a resend affordance, a new public `/verify-email` page (mirroring `/reset-password`) auto-verifies and logs the user in, and Login catches the unverified-account 403 with its own resend affordance. |
| Plug-ID start + available-plugs list | ✅ | **2026-07-12:** QR/deep-link start — visiting `/?plug=<id>` prefills the Plug ID input and scrolls/focuses it; still fully auth-gated (an anonymous visitor is redirected to `/login`, which now returns the driver to their original destination — including the query string — via router `state.from`, shared by `ProtectedRoute`/`CpoProtectedRoute`). An unknown/inaccessible id shows a small inline notice rather than blocking the page. No in-app camera scanning (per [requirements.md](../requirements.md) §4) — the QR (rendered CPO-side, see below) just encodes this URL for the phone's own camera. **Sectioned list (2026-07-12, later the same day):** the flat charger list is now collapsible sections — "Your chargers" (plugs whose group is non-public, via the new `is_private` on the plug APIs; the society/office primary use case) first, then "Public chargers" (collapsed by default when private ones exist), each header showing live per-status counts; plug cards also show the resolved `price_per_kwh`. |
| Driver reservations UI (Home) | ✅ | **2026-07-12** (feat/reservations): each plug card gets a "Reserve" action → `ReserveModal` (date + start time + 30 min/1 h/2 h/4 h duration presets → `POST /api/reservations` with ISO strings; the plug's upcoming windows are listed in the modal via `GET /api/plugs/{id}/reservations` so members book around each other; 409s — slot taken / cap reached — render inline). Cards show a "Reserved until HH:MM" badge (local time) when a booking covers now — distinct from occupied; the holder sees "Reserved for you" and the card stays startable, while a plug inside someone else's window stops inviting the start click (the server would 409 anyway). A "Your reservations" strip lists upcoming bookings (plug, window, cancel). Times render in the viewer's local timezone (`utils/reservationTime.js`). Tests: `ReserveModal.test.jsx`, `Home.test.jsx` ("Home — reservations"). |
| Live session monitor (Socket.io) | ✅ | Real Socket.io client with token-auth. **2026-07-10:** client-side ticking elapsed clock (no longer freezes between frames), a "reconnecting" staleness banner (server `is_stale` OR no frame for 15 s), voltage + actual-relay secondary line, and a per-plug `gateway_alarm` warning banner (e.g. unauthorized-on). Tests in `SessionMonitor.test.jsx`. |
| Driver plug list: gateway-offline UX | ✅ | Home marks a plug whose gateway is unreachable (`gateway_online === false`) as "charger offline", dims it, and disables start — no more blind 409s at session start. |
| CPO alert feed | ✅ | `CpoAlerts` on the dashboard fetches `GET /api/cpo/events?unacknowledged_only=true`, merges live `gateway_alarm` broadcasts, and dismisses via the ack endpoint. Severity-styled (critical/warning/info). |
| CPO fault console | ✅ | **2026-07-12**: new `/cpo/faults` page (sidebar link) — `GET /api/cpo/events` with severity + unacknowledged-only filters, a top strip of plugs currently in maintenance, acknowledge, and "Put in maintenance" / "Clear maintenance" on plug-scoped safety faults via the new maintenance endpoint. Live-updates by re-pulling on the existing `gateway_alarm` Socket.io broadcast (`useSession().alarms`, same mechanism as `CpoAlerts`) rather than polling. Tests in `CpoFaults.test.jsx`. |
| CPO earnings & payouts page | ✅ | **2026-07-12**: new `/cpo/earnings` page (sidebar "Earnings") — lifetime + unsettled gross/fee/net summary cards with the unsettled window dates (`GET /api/cpo/earnings`; coins labelled ₹-equivalent, 1 coin = ₹1), "Request payout" with a confirm dialog (backend 400 "nothing unsettled" / 409 "already pending" surfaced inline), and the payout history table (period, gross/fee/net, status badge, requested/paid timestamps) with Cancel on REQUESTED rows and an admin-only "Mark paid" (role from AuthContext's `/api/auth/me` user). Footnotes state the transfer itself happens outside the app (bank/UPI). Tests in `CpoEarnings.test.jsx`. **2026-07-21:** added an "Offline top-ups" card — the available-pool figure (`topup_pool` from `GET /api/cpo/earnings`) with an honest explainer that it comes out of unsettled earnings, a two-step Modal-then-ConfirmDialog "Credit a driver" flow (409-over-pool surfaced inline), and a top-up history table (`GET /api/cpo/topups`, paginated). Driver-side wallet ledger rows for the credit render as "Top-up by operator (cash)" (`utils/statusCopy.js`). |
| CPO reservations page | ✅ | **2026-07-13**: new `/cpo/reservations` page (sidebar "Reservations") over the tenant-scoped `GET /api/cpo/reservations` — a table of every booking (plug, driver name/email, window in the viewer's local timezone, status badge, linked session id) with a status filter (booked/fulfilled/cancelled/expired) + "Upcoming only" toggle and a live "N shown · M active" count. Operators cancel a still-BOOKED slot inline (`POST /api/reservations/{id}/cancel`, `window.confirm` first since the driver is notified; a 409 on a terminal-state row surfaces inline). Closes the reservations frontend follow-up (the driver-side Home UI already shipped 2026-07-12). Tests in `CpoReservations.test.jsx`. |
| CPO tariff & TOD-slot editor page | ✅ | **2026-07-14** (Pricing v2 Phase 4): new `/cpo/tariffs` page (sidebar "Tariffs") — list/create/delete tariffs (name + base coins/kWh), and per-tariff time-of-day slots managed inline (`GET/POST/DELETE /api/cpo/tariffs/{id}/slots`) via native `<input type="time">` (HH:MM→minute-of-day; a window ending at midnight maps to 1440). Backend validates start<end (400) and non-overlap (409, `services/pricing.py slot_overlaps`). The tenant timezone the slots are read in is surfaced read-only from `GET /api/cpo/profile`. **Per-weekday slots shipped 2026-07-16:** the add-slot form has Mon–Sun toggle chips → a `days_mask` (Mon=bit0…Sun=bit6, default every day; ≥1 day enforced client- + server-side) sent on create, and the slot list shows each slot's days ("Every day"/"Weekdays"/"Weekends"/day list). The backend already stored/validated `days_mask` (disjoint days don't conflict); the one gap closed alongside was `_slot_rate_and_bound` rolling its next-boundary forward to the next applicable weekday (so a session in an end-of-day gap or on a slot-less weekday still reprices when the next window opens — the driver "next price" ribbon stays same-day only). Tests in `CpoTariffs.test.jsx`, `test_pricing_v2.py`. **Simple/Advanced split shipped 2026-08-02** (feat/simple-pricing-ux — owner feedback: the full editor overwhelmed a small CPO who just wants to set one price): the `/cpo/pricing` page (renamed from `/cpo/tariffs` in redesign v3) now defaults to a **Simple** tab — one "Price per kWh (₹)" field plus a live example cost ("a 3 kW charge for 1 hour ≈ ₹X") — with the original tariff/slot/assignment editor moved to an **Advanced** tab, unchanged in capability. Frontend-only: Simple's Save edits (or creates) the tenant's one flat tariff through the *same* `/api/cpo/tariffs*` endpoints, deletes any of its TOD slots (a flat rate "broadcasts" across every weekday/phase by construction — no slot means the base rate covers every hour), sets it as the tenant default, and clears any group/charger `tariff_id` pinned to a *different* tariff — no schema change. `utils/pricingUniformity.js getPricingSummary` decides the default tab: uniform (0 tariffs, or exactly 1 that's the tenant default with no slots and no group/charger override) opens Simple; anything else opens Advanced and Simple shows a "Custom schedule active" notice instead of a misleading single number, routing Save through a confirm dialog before it overwrites the schedule. Tests: `pricingUniformity.test.js`, `CpoPricingSimple.test.jsx`, `CpoPricing.test.jsx`. |
| CPO session CSV export | ✅ | `GET /api/cpo/analytics/sessions.csv` (same filters as the JSON endpoint) → downloadable `text/csv`; "Export CSV" button on the Sessions page (authenticated fetch → blob). |
| CPO load analytics: current (amps) | ✅ | `/api/cpo/analytics/telemetry` now also aggregates `avg_current_a`/`max_current_a` per bucket; the dashboard load chart header shows peak W **and** A. |
| Razorpay top-up flow | ✅ | CDN script + `window.Razorpay`; key comes from backend order. Formats and displays decimal coin balances. |
| Pricing clarity | ✅ | `GET /api/config` (public) feeds a `ConfigProvider`; Home shows the tariff (`coins_per_kwh`) + what the driver's balance covers (≈ kWh) with a top-up nudge below the minimum, and the session monitor reads the rate from config instead of hardcoding it. The session-start minimum is now env-driven (`MIN_START_BALANCE_COINS`) and the 402 message matches the displayed number (2026-07-10). |
| Low-balance live warning | ✅ | The session monitor warns (with remaining coins ≈ kWh) as accrued cost nears the session's hold (or the wallet for legacy unheld sessions), pairing with the backend auto-stop so the driver sees it coming. Since 2026-11-15 it warns only when the wallet is what will stop the session: a kWh-limited session whose limit fits inside its hold stays quiet (the Limit banner covers it), matching the backend `low_balance` notification. Tests in `SessionMonitor.test.jsx` and `test_notifications.py`. |
| Driver-set charging limit at start (kWh / time), opt-in | ✅ | **2026-07-12, superseded by the redesign-v3 `ChargeSetupModal` and made opt-in 2026-08-02:** the shared start/queue setup dialog (`frontend/src/components/ChargeSetupModal.jsx`) offers time-limit preset chips (No limit / 30m / 1h / 2h / 4h / 8h / Custom hh:mm, defaulting to "No limit") and a plain kWh field (blank by default); the coverage line uses the plug's own resolved `price_per_kwh`. Sent as `max_kwh`/`max_duration_seconds` through `SessionContext.startSession` — **only the keys the driver actually set**; no limit chosen → neither key is sent → `SessionStartRequest` persists NULL/NULL (charge until stopped), not a hidden default duration/energy (before 2026-08-02 an omitted field defaulted to 14400 s / 30 kWh). The gateway/software-agent ON command still always carries concrete numeric watchdog values — the driver's limit, or an UNLIMITED sentinel when none was set (`services/mqtt_manager.py firmware_duration`/`firmware_max_kwh`) — since neither the firmware nor the AmpHive Agent has a wire representation for "no limit" (omitted → their own old hard default; `0` → an instant on-device cutoff). The session monitor shows live progress toward the focused session's limits when one is set ("0.42 / 1.00 kWh · stops automatically", `focusedLimits` restored from `/api/sessions/active`) and an indeterminate ring / "Set a charging limit" affordance when it isn't. Tests in `ChargeSetupModal.test.jsx`, `SessionMonitor.test.jsx`, `SessionContext.test.jsx`, `backend/tests/test_session_limits.py`. |
| Driver notification bell + Web Push opt-in | ✅ | 2026-07-11: navbar `NotificationBell` — unread badge, dropdown feed (`GET /api/notifications`), live prepend from the Socket.io `notification` user-room event, mark-read/mark-all, and an enable-push flow (permission → `sw.js` service worker → `pushManager.subscribe` with the backend-derived VAPID key). `frontend/public/sw.js` is push-only (no fetch interception). Tests in `NotificationBell.test.jsx`. |
| Post-session receipt | ✅ | `finalize_charging_session` now returns a full receipt (plug name, energy, peak power, duration, coins charged + any forgiven shortfall, balance before → after, timestamps, stop reason); the Session page shows a `SessionReceipt` card on stop (with an auto-stop notice when applicable) and refreshes the wallet. **Verified live end-to-end 2026-07-10** — a real billed session on the fake plug (0.101 kWh → 0.51 coins, balance 499.47 → 498.96, reconciled in the ledger). Tests in `SessionReceipt.test.jsx`. |
| Charger groups (join/list) | ✅ | |
| CPO operator portal (setup, dashboard, plugs, groups, sessions) | ✅ | `pages/cpo/*` behind `CpoProtectedRoute`; charts via `recharts` |
| CPO portal on its own hostname | ✅ | **2026-07-20:** one bundle, two hostnames — the operator portal is served on `cpo.amphive.app` (a dedicated DNS A record to the same IP; optional `CADDY_CPO_DOMAIN` in `.env` makes `deploy.ps1` emit a second identical Caddy site block, same HSTS). `utils/appHost.js` (`isCpoHost()` + `VITE_FORCE_CPO_HOST` dev/test override, `cpoOrigin()`/`driverOrigin()`) partitions `App.jsx`/`Navbar.jsx`: the **driver host** shows driver routes only, `/cpo/*` hard-redirects to the CPO origin, and the navbar's in-app CPO links are replaced by a modest external "Apply to host chargers" link; the **CPO host** shows the operator portal only — `/` role-routes (`components/HostRouting.jsx` `CpoLanding`: anonymous → login, cpo/admin → dashboard, a driver-role login gets a "not an operator account" notice linking to the driver origin and the `/cpo` become-a-host flow) and driver routes redirect to the driver origin. Both hostnames added to the backend CORS + Socket.io allowlists. Tests: `appHost.test.js`, `HostRouting.test.jsx`, `Navbar.test.jsx`. |
| Map of available plugs | ✅ | Leaflet/OpenStreetMap `MapComponent` on Home. Plug geolocation is now persisted (`Plug.latitude`/`longitude`, falling back to the gateway's coords); markers use real coordinates and plugs without a known location are omitted — the old `Math.random()` fallback (which also jittered markers on every re-render) is gone. **2026-07-12:** markers are now color-coded (Available/In use/Offline, `utils/plugAvailability.js`) with a legend showing live counts next to the map, plus availability and group-name filters (shared state) that narrow both the list and the map markers together. **Later the same day:** the map moved to the **bottom of Home** in a collapsed-by-default section (the product's primary society/office users know where their charger is; tiles aren't fetched until opened), plots **public plugs only**, and auto-fits its initial view to the plotted markers (falling back to the India-wide view when nothing has coords). **Correction 2026-11-19:** plugs now carry `rated_power_w` and `connector_type` (advertised specs) and, from wave 3 (C4, `0049_plug_hours`), `amenities` and opening hours with an Any hours / Open now / Open 24x7 driver filter; power, connector and amenity filters on the map are still not offered; see [MARKET_GAP_ANALYSIS.md §2](MARKET_GAP_ANALYSIS.md). |
| Public charger discovery map (pre-signup) | ✅ | **2026-07-16:** a **public `/map` route** (not auth-gated) lets a visitor browse nearby **public** chargers before signing up (PlugShare-style discovery funnel). Backend: new UNAUTHENTICATED `GET /api/plugs/public` returning only public-group/ungrouped located plugs with a minimal safe projection (`{id,name,status,latitude,longitude,price_per_kwh,gateway_online}`) — private/society plugs are never exposed, and no per-user/session/network fields leak; rate-limited per IP (`PUBLIC_MAP_RATE_LIMIT`, default 60/60). Frontend: `PublicMap.jsx` reuses `MapComponent` (new optional `selectLabel` prop → "Sign up to charge"); every action routes to sign-in ("browse without an account, act with one"). A "🗺️ Map" navbar link (shown to everyone, incl. on `/login`) is the anonymous entry point. Tests: `PublicMap.test.jsx`, `backend/tests/test_public_plugs.py`. |
| CPO per-plug QR code | ✅ | **2026-07-12:** `/cpo/plugs` has a "QR" action per row rendering a printable QR (`qrcode.react`) for `{origin}/?plug=<id>` (origin read from `window.location.origin` at render time, never hardcoded) in a modal with the plug name/id and a print stylesheet limited to the code itself. |
| "View History" button (WalletCard) | ✅ | Superseded by the redesign-v3 split (§0.0): the old tabbed `History.jsx` (Charging Sessions + Wallet Ledger) is gone — `Activity.jsx` at `/activity` now owns session history (`GET /api/sessions/history`) and `Wallet.jsx` at `/credit` owns the money trail (`GET /api/wallet/ledger`, still the unified top-up-credits-and-session-debits view with signed amount + running `balance_after` from the original 2026-07-10 "debits-only" fix). |
| CPO gateways management + OTA-from-UI | ✅ | New `/cpo/gateways` page (sidebar link) lists each gateway's status, reported firmware, last-seen, and plug count, with an "Update Firmware" action that POSTs `/api/cpo/gateways/{id}/ota` (button disabled unless the gateway is online with ≥1 plug). Completes the OTA loop the fw-tracking surfaced (2026-07-10). **Firmware release registry + version picker (2026-08-02, feat/ota-version-picker):** hand-pasting the image URL is gone — new `firmware_releases` table (Alembic `0036_firmware_releases`; admin-only `POST`/`GET /api/admin/firmware-releases` + `POST .../{id}/deactivate`, new admin console page `/admin/firmware-releases`) backs a `GET /api/cpo/firmware-releases` (active only) that the CPO "Update firmware" modal renders as a `<select>` dropdown, newest-version-first, options newer than the gateway's current version marked "— newer" (semver-aware ordering both sides — `backend/services/versioning.py` / `frontend/src/utils/version.js` — not a string sort, so "2.10.0" ranks above "2.9.0"). `POST .../ota` now takes `{release_id}` (resolved server-side to the release's URL) OR the original `{firmware_url}`, which is now an **admin-only escape hatch** (403 for a `cpo`-role caller) surfaced in the CPO modal only to admins. Registering a URL per version is this cut's deliverable — binary upload/hosting through the registry is a noted follow-up, not built. |
| TypeScript usage | — | **Decided against 2026-07-07** (TD#14): toolchain removed; all app code is plain `.jsx`/`.js` by policy. ESLint now actually lints `js/jsx` (the old config matched only `ts,tsx` — zero files). |
| Frontend tests (Vitest + RTL) | ✅ | **~570 tests / 62 files as of the 2026-07-22 charging-credit reframe (§0)** — up from 512 tests / 61 files right after the redesign-v3 rewrite (2026-07-21, §0.0), plus the follow-on backlog batch (§0.05); every rebuilt page/component carries a behavioral suite (states render, actions hit the right APIs, errors surface) plus the UI-kit suites (Modal focus trap, Toast timers, DataTable states, statusCopy/money tables). Historical note — the pre-redesign suite was 127 tests: AuthContext, ProtectedRoute/CpoProtectedRoute (incl. the `state.from` redirect target), Login (returns to the preserved origin, incl. `?plug=`), TopUp payment handler (no client amount on `/verify`), multi-session SessionContext (incl. limit payloads + `focusedLimits` restore), History, Home (QR/deep-link prefill + auth gate + unknown-id notice, private/public sections + collapsed-map behavior, availability/group filters, legend + section counts, charging-limit payloads incl. per-plug coin conversion, notify-when-free bell: shown only on non-startable cards, optimistic POST/DELETE with revert-on-failure, cleared by a live `plug_status→available` flip, reservations: "Reserved until"/"Reserved for you" badge + blocked start click, Reserve modal opening, "Your reservations" strip + cancel), ReserveModal (schedule fetch/render, booking payload ISO strings + duration presets, inline 409, default preset), ChargeLimitControl (presets, coins→kWh conversion + clamping, previews), SessionMonitor limit-progress display, MapComponent (marker color per state, fit-bounds vs country-wide fallback), CpoPlugs QR modal, CpoEarnings (summary, request-payout incl. 409 path, cancel, history). `npm test` runs in CI. |

### Firmware
| Capability | Status | Notes |
|------------|:------:|-------|
| **Direct MQTT transport (fw ≥ 1.3.0, default)** | ✅ | `AMPHIVE_DIRECT_MQTT=1`: outbound `mqtts://8.231.81.12:8883` right after Wi-Fi — no overlay, NAT/CGNAT-immune, esp-mqtt owns reconnects. **Verified on-device 2026-07-10** (~3.3 s power-on→connected through a symmetric-NAT router; telemetry at 10 s cadence). Binary shrank ~50% (microlink linked out; the component itself was deleted from the tree 2026-08-02). |
| **Unauthorized physical-on guard (fw ≥ 1.5.0)** | ✅ | The relay ON with no active session (physical button / Tapo app / stale NVS resume) is forced OFF locally every poll and alarmed once per episode (`UNAUTHORIZED_ON`, rising-edge). Uses the plug's real `device_on` (previously read but discarded). **Live on the real gateway 2026-07-10** (OTA'd to `1.5.0-direct`); the backend ingests the alarm end-to-end (verified in prod). The remote out-of-band physical-press trigger itself is unit-tested + by-construction (no LAN path to press the button remotely). |
| **Offline/unmetered-consumption reconciliation (fw ≥ 2.4.0-direct)** | ✅ | `tapo_plug_reconcile_idle_baseline()` cross-checks the plug's own `today_energy`/`month_energy` counters (independent of the gateway) against an NVS-persisted idle baseline whenever no AmpHive session is active, distinguishing a calendar rollover/plug reset from real consumption, and publishes a one-shot `UNMETERED_CONSUMPTION` alarm. Every telemetry frame also carries `today_kwh`/`month_kwh` for the backend's own continuous cross-check. See [FIRMWARE.md §4a](FIRMWARE.md#4a-offlineunmetered-consumption-reconciliation-fw--240-direct). **Build-verified** (ESP-IDF v5.3.3, `idf.py set-target esp32 && idf.py build`); bench-tested against `tools/p110_sim/` (extended with independent today/month counters + `--daily-reset-counter`). **OTA'd to the fielded gateway as of fw `2.7.0-direct`** (2026-08-13), which also fixed the false-alarm bug this feature shipped with (the idle baseline froze for the duration of a session, so the first idle poll after a stop reported the driver's own legal charge as unmetered; see §0.0005 and [FIRMWARE.md](FIRMWARE.md) §4a). |
| **Richer telemetry: relay state + trapezoidal energy (fw ≥ 1.5.0)** | ✅ | Telemetry now carries the actual `relay` (device_on) state alongside derived `current`/nominal `voltage`; the driver-side kWh integrator switched from left-rectangle to the **trapezoidal rule** (averages consecutive power samples) for lower error on ramping loads at the 10 s cadence. **Verified on the wire 2026-07-10** (real gateway telemetry shows `"relay":false`). |
| ~~`microlink` Tailscale client (Noise/ts2021, DERP, DISCO, STUN, WG)~~ | — | **Removed 2026-08-02.** Worked for full-cone NATs, but symmetric NAT defeated DISCO hole-punching (root-caused 2026-07-09) — the reason for the direct-MQTT pivot. Kept compilable-but-unused for rollback until the linker map confirmed zero objects were pulled from it under the shipped build, then deleted (see [FIRMWARE.md §5](FIRMWARE.md#5-historical-microlink-removed-2026-08-02)). |
| MQTT control loop + topic contract | ✅ | Matches backend topics. **Multi-plug (TD#20), shipped fw 1.7.1-direct, verified on-device 2026-07-12**: `main.c` keeps a `plugs_mutex`-guarded per-plug slot table (each slot = DB `plug_id` + LAN IP + per-plug `tapo_plug_t` KLAP context + its own session/watchdog state) and `tapo_protocol.c` moved the KLAP session and energy integrator into that per-plug context, so a command for plug B can no longer actuate plug A and telemetry is published under each plug's own id. As of **fw 2.0.0-direct** the gateway learns each plug's IP from the backend's **retained plug roster** on `amphive/gateways/{gw}/config` (`handle_plug_roster` builds/reconciles the slot table on connect; the ON/OFF `local_ip` is now a refresh/fallback), so idle telemetry flows for every plug with no provisioned plug IP and no boot-time provisional slot (the removed slot's old job of keeping the liveness gate fresh — a 1.7.0 regression once fixed in 1.7.1 — is now the roster's). **Single-plug charging regression verified end-to-end on the real gateway** (OTA to 1.7.1, billed session: 0.014 kWh → 0.07 coins, ledger reconciled); the **2.0.0-direct roster path is verified live on the real gateway 2026-07-15** (OTA'd from 1.9.0; the plug is learned from `.../config` and idle telemetry flows — `last_seen` advancing), with **two-real-plug validation still pending** (single plug on the bench). (§3.50, §3.57, TD#20, SEC §8.5) |
| Captive portal provisioning | ✅ | `AmpHive_Setup_XXXX` → NVS → reboot. **As of fw 2.0.0-direct** it collects only Wi-Fi + Tapo account + per-gateway MQTT password — the "Target Plug IP" field was removed; plug IPs now arrive from the backend's retained roster (§3.57). **Locked down fw 1.6.0** (SEC §8.1 closed): WPA2 AP + `/save` gated by a per-device setup code (NVS-persisted, printed over serial for the unit label), AP-only interface, 10-min idle timeout → reboot/STA retry. **Live on the real gateway 2026-07-11** (OTA `1.5.0-direct` → signed `1.6.0-direct`, rollback cancelled); the locked portal itself is verified by construction/build only — exercising it needs physical access to force a Wi-Fi-loss fallback. **Mobile-first wizard + Wi-Fi scan (2026-08-02, feat/easy-provisioning):** `portal_html` reworked into a two-step wizard (Step 1 Wi-Fi incl. a tappable `GET /scan` network list with manual-entry fallback, Step 2 Tapo account; big touch targets, `autocapitalize=none` on the setup-code field to close a phone-keyboard auto-caps trap against the case-sensitive compare); the MQTT password field became optional (`save_config_to_nvs` skips the NVS write when blank instead of clobbering a pre-provisioned value) so a preflashed unit's broker password survives an ordinary buyer's flow. Every response renders through a shared `send_status_page()` helper (colored heading, plain-language copy). **Build-verified** (`idf.py build`, ESP-IDF v5.3.3, ESP32-C3 target, signed image, 43% partition free) but not yet flashed/exercised on real hardware. See [FIRMWARE.md §2](FIRMWARE.md#2-captive-portal--implemented-locked-down-fw-160-mobile-first-wizard-2026-08-02). |
| Edge watchdogs (duration/energy/thermal/over-current) | ✅ | Thermal + over-current now use the plug's `overheat_status`/`overcurrent_status` flags (the P110 has no °C sensor) |
| Over-current cutoff | ✅ | Enforced via the plug's `overcurrent_status` flag → local OFF + `OVERCURRENT_CUTOFF` alarm |
| Tapo P110 driver (KLAP/AES) | ✅ | **Real KLAP v2** (mbedTLS SHA/AES + `esp_http_client`); fully verified on-device; builds on **ESP-IDF v5.3** (not v6) |
| Session persistence in NVS / offline resync | ✅ | `session_nvs` module persists active session to NVS; `offline_log` ring buffer (64 entries) caches telemetry during MQTT outages; resync on reconnect |
| OTA updates | ✅ | Dual OTA app slots (`partitions_ota.csv`) + `esp_https_ota` with bootloader rollback (`ota_update.c`). Triggered by the `OTA` MQTT command / `POST /api/cpo/gateways/{id}/ota`; refuses mid-session; cancels rollback only once the new image re-reaches the broker. **Verified end-to-end on-device 2026-07-08** (`1.1.0 → 1.1.1`) and again **over the direct-MQTT path 2026-07-10** (`1.3.0 → 1.3.1`, image on a public URL, slot swap + `marking image valid`, no overlay). |
| OTA hardening: signed images + https-only | ✅ | **Rolled out 2026-07-10** (fw ≥ 1.4.0): ECDSA signed-app verification on update (`SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT`, key gitignored), plain http refused by firmware + backend (`CpoGatewayOtaRequest` `^https://`, **deployed**), images on the public HTTPS bucket `gs://amphive-fw`. The real gateway `1cc3abb4fb54` was OTA'd end-to-end over the direct-MQTT path from `1.3.2-direct` → signed **`1.5.0-direct`** (`OTA_OK_REBOOTING` → offline → back online on 1.5.0, rollback cancelled). From 1.4.0 onward only signed images install. |
| ~~Headscale (vs Tailscale defaults)~~ | — | **Removed 2026-08-02** along with `microlink` (the coordination client that made the `/key` fetch/Headscale support relevant). |

### Software gateway (AmpHive Agent, [`agent/`](../agent) — [AMPHIVE_AGENT.md](AMPHIVE_AGENT.md))
| Capability | Status | Notes |
|------------|:------:|-------|
| Multi-brand software gateway (kasa/shelly/sim providers, discovery→assign) | ✅ | Speaks the firmware MQTT contract verbatim; verified end-to-end against a real broker with the `sim` provider (see AMPHIVE_AGENT.md). |
| **Local kWh + duration watchdog (offline cutoff)** | ✅ | **2026-07-20** — closes the "software agent has NO local kWh limit → unbilled offline tail" gap (memory `gateway-offline-reconnect-reconciliation.md`, `docs/proposals/queued-charge-offline-plug.md` §out-of-scope, reconciliation audit exclusion list). The `ON` payload's `max_kwh`/`max_duration_seconds` are stored (persisted in the agent's JSON store, so a restart mid-session keeps them); every poll the agent checks session energy (cumulative-meter delta, with a watts×dt integration fallback for meterless plugs) and elapsed time, and on a limit cuts the plug OFF **locally over the LAN — works with the broker unreachable**, mirroring the firmware watchdog. Like the firmware, the trip frame is published pre-watchdog (occupied + final kwh) and, unlike the firmware (which stays silent on limit cutoffs), a QoS-1 `{"event":"LOCAL_LIMIT_CUTOFF","reason":"ENERGY_LIMIT"\|"DURATION_LIMIT","plug_id"}` alarm is queued (paho delivers on reconnect) — **the backend finalizes the session on this alarm** (bills, frees the plug, notifies the driver; no maintenance), like `OVERCURRENT_CAP`. `SET_LIMITS` re-caps a running session without re-baselining (no-op when idle), matching MQTT_CONTRACT.md. **[Opt-in charging limits, 2026-08-02]** the agent's own `_DEFAULT_MAX_KWH`/`_DEFAULT_MAX_DURATION_S` (30.0 / 14400, `agent/amphive_agent/core.py`) are now a dead-in-practice fallback for a genuinely absent field — the backend always sends a concrete numeric value on `ON`/`SET_LIMITS`, real limit or the UNLIMITED sentinel, never `0` (this agent's `limit_exceeded()` trips on `session_kwh/elapsed >= 0`, same instant-cutoff hazard as the firmware watchdog it mirrors). Tests: `agent/test_local_limits.py` (stubbed device + offline broker) + `python -m amphive_agent.core` self-checks. |

### Infra / deploy
| Capability | Status | Notes |
|------------|:------:|-------|
| Docker Compose on GCP VM | ✅ | Live/canonical. **2026-07-12:** the frontend nginx now sends explicit `Cache-Control` — `no-cache` on `index.html`/`sw.js` (revalidate on every launch; previously *no* cache header meant browsers heuristic-cached the SPA shell, so phones kept launching stale bundles days after a deploy) and `public, max-age=31536000, immutable` on the content-hashed `/assets/*`. |
| `deploy.ps1` + `scripts/*.bat` helpers | ✅ | VM start/stop + remote compose/logs (in `scripts/`) |
| K8s/K3s manifests | — | **Retired 2026-07-07** (TD#15): banner-marked unmaintained reference (`deploy/k8s/README.md`); Compose-on-VM is the only deployment model |
| Web HTTPS (Caddy front door) | ✅ | **Deployed + verified in prod 2026-07-11.** `deploy.ps1` ships `docker-compose.tls.yml` by default (`-NoTls` = plain-HTTP rollback): Caddy is the only public web entrypoint on 80/443 with an auto-renewed Let's Encrypt cert for `CADDY_DOMAIN` (Caddyfile generated on the VM from `.env`); the frontend container publishes no host port; bare-IP/unknown-Host requests are *served* (not redirected) so a DNS-provider outage can't take the site down. The tls compose was brought to prod parity first (8883/passwd/ACL/cert mounts — it predated direct-MQTT and would have broken the broker). **Verified live**: `https://amphive.duckdns.org` 200 with a validated LE cert (CN match, valid to 2026-10-09), domain http→https 308, `/api` + Socket.io handshake over https, CPO login + gateway list; broker untouched — both gateways stayed online throughout. Rollout hit a real **DuckDNS authoritative-nameserver outage** (cert issuance blocked ~1 h; Caddy auto-retried it in — incident log: `deploy/docs/web_tls_rollout.md`). **Follow-ups closed 2026-07-11**: public `:8000` firewalled (tcp:80-only rule; VM-local access kept) + **HSTS** (max-age=31536000), verified live. Remaining: a **real domain** (DuckDNS is a demonstrated SPOF), then flip bare-IP serve→redirect. |
| Backups (DB dumps + config + disk snapshots) | ✅ | **Live + verified end-to-end 2026-07-11.** Nightly cron (21:00 UTC) on the VM runs `backup_db.sh` (shipped by `deploy.ps1`): `pg_dump -Fc` of `amphive` + ops-config tarball (`.env`, Caddyfile, mosquitto passwd/ACL — per-gateway hashes exist only on the VM — broker certs) → `gs://amphive-db-backups` (private, 30-day lifecycle), keeping the last 3 sets locally either way. Daily **disk snapshots** (policy `amphive-daily-snapshot`, 14-day retention) attached to the VM disk. VM uploads keylessly (scope raised to `devstorage.read_write`, ~48 s owner-approved downtime; bucket IAM = objectCreator+Viewer only — a **`gsutil rm` from the VM is denied**, so a compromised VM can't destroy backups). **Verified**: upload set in `gs://amphive-db-backups/2026/07/`; **restore tested** into a scratch DB (row counts matched live). Runbook + quarterly drill: `deploy/docs/db_backup_restore.md`. **2026-10-20 correction:** `deploy.ps1` stopped shipping the script in its 2026-08-02 relay rewrite (the VM copy is hand-installed and has drifted: it uploads to `gs://amphive-db-backups-2` since the 2026-09-04 GCP account move and also tars the mail store), and the cron **silently took no backup for 45 days** after a manual `sudo` run on 2026-09-04 left `backup.log`/`backups/` root-owned. Fixed (chown + manual run); the repo script now refuses to run as root and fails loudly (stderr + syslog) on unwritable paths. The guard still has to be merged into the live copy by hand. See the runbook's incident section. |
| Mosquitto broker | ✅ | Auth enforced (`allow_anonymous false` + passwd file) **+ topic ACLs** (2026-07-10). **8883 TLS is PUBLIC — the primary "direct MQTT" transport** (devices dial outbound `mqtts://8.231.81.12:8883`, NAT-immune; per-gateway accounts scoped to `amphive/gateways/<id>/#` via `add_gateway_user.ps1`). Overlay-bound plaintext 1883 stays as the legacy/transition path. Verified in prod: ESP32 fw 1.3.0-direct connected from the public internet through a symmetric-NAT router in ~3.3 s power-on→connected; ACL isolation cross-checked. See SECURITY.md §3, MQTT_CONTRACT.md. |

---

## 2. End-to-end reality check

- **Path A (ESP32 + MQTT)** is the operating path and has now been run
  **end-to-end on physical hardware** (2026-07-06): a real ESP32 gateway + P110
  drove a billed session over MQTT — the plug delivered the correct energy and
  real telemetry flowed through TelemetryStore → DB → the live stream →
  wallet debit (`DIRECT_MODE=false`).
- **Billing correction (2026-07-06, verified on-device):** the first hardware run
  surfaced a **session overbilling bug** — the firmware published its *lifetime*
  energy integrator as the telemetry `kwh`, so every session after the first billed
  the plug's entire accumulated history (`kwh × COINS_PER_KWH`). The firmware now
  reports **session-relative** energy (`meter − session_baseline`, the same value
  the watchdog uses); idle reports 0. **Reflashed and re-verified later the same
  day** (ESP-IDF v5.3.3 toolchain now installed at `C:\esp\v5.3.3`): consecutive
  billed sessions (#77–79) each started at `kwh = 0.0000` — the second/third did
  **not** inherit the first's accrual — and raw broker payloads confirmed both the
  session-relative `kwh` and the `session_id` echo on the wire.
  Also fixed alongside: the inbound telemetry `TelemetryStore.update()` was called
  from the paho thread, invoking `asyncio.Event.set()` cross-thread; it is now
  marshaled onto the event loop (`tests/test_mqtt_manager.py`).
- **Operational gotcha found during the 2026-07-06 reflash:** the gateway's NVS
  held the **pre-rotation Tapo password** (secrets were rotated 2026-07-06 after
  the board was provisioned), so every KLAP handshake failed with `handshake1
  auth mismatch` until the `tapo_pwd` NVS key was rewritten (done via a one-off
  fixer app; no NVS erase / re-provisioning needed). Rotating the Tapo account
  password **always** requires updating the provisioned copy on each gateway.
- **Path B (Direct Mode + WireGuard relay)** has been **retired** — the WireGuard
  tunnel is no longer used. `tapo_direct` and the `/direct/*` endpoints were
  **removed 2026-08-02** (dev/test-only, defaulted off, unused by
  frontend/agent/tools).

## 3. Full discrepancy list (doc says X → code does Y)

1. [Resolved 2026-07-02] The README API section now summarizes all **35**
   endpoints (including the CPO portal) and links to [API_REFERENCE.md](API_REFERENCE.md).
2. [Partly resolved 2026-07-02] Time-series telemetry **is** now persisted to the
   `telemetry_readings` table via a buffered batch-flush from the MQTT handler
   (`backend/services/telemetry_persistence.py`), queried by
   `GET /api/cpo/analytics/telemetry`. **TimescaleDB specifically** is still not
   used — plain Postgres + `date_trunc` aggregation was chosen deliberately;
   hypertables/native-retention/continuous-aggregates remain a possible future
   upgrade.
3. [Resolved] ~~LWT offline alerts on the backend~~ — the README no longer
   makes this claim (LWT is published by the *firmware*; the backend persists
   gateway status when received).
4. [Resolved] ~~Python version mismatch~~ — README now says 3.11, matching
   the Dockerfile.
5. [Resolved 2026-07-07] ~~`schema.sql`/`schema_v2.sql` are not executed~~ —
   schema management moved to **Alembic** (`backend/migrations/`, frozen-DDL
   baseline `0001_baseline`); the drifted SQL files are deleted and
   `create_all` + `_INPLACE_UPGRADES` retired. Startup stamps pre-Alembic
   databases at the baseline, then upgrades to head. CI verifies
   baseline == models against real Postgres (`backend/tests/test_migrations.py`).
6. [Resolved 2026-07-02] RBAC is enforced. Self-registration still creates a
   `driver`, but a driver self-promotes to `cpo` via `POST /api/cpo/setup`, and
   `require_role("cpo","admin")` (`backend/services/rbac.py`) gates all `/api/cpo/*`
   routes against the live DB role.
7. [Resolved 2026-07-02] The unauthenticated `gateways/register` /
   `plugs/register` endpoints were removed; provisioning now goes through the
   RBAC-gated, tenant-scoped `POST /api/cpo/gateways` / `POST /api/cpo/plugs`.
8. [Resolved] ~~Direct Mode enabled in the committed config~~ — Path B is
   retired: the `DIRECT_MODE` config and the `/api/direct/*` endpoints were
   removed 2026-08-02 (see §2).
9. [Resolved] `charging_sessions.peak_power_w` is now populated.
10. [Resolved 2026-07-02] Wallet updates are now row-locked (`SELECT ... FOR
    UPDATE`) in the stop, verify, and webhook paths.
11. [Resolved 2026-07-04] **Real-time Communication (Socket.io):** Replaced live SSE with Socket.io for session telemetry updates. Auth is verified using JWT token on connection (via auth payload or query parameters).
12. [Resolved 2026-07-06] A Leaflet/OpenStreetMap map (`MapComponent`) is on Home,
    and plug **geolocation is now in the data model** (`Plug.latitude`/`longitude`,
    with a fallback to the gateway's coords). Markers use real coordinates; plugs
    without a known location are omitted. The old `Math.random()` fallback — which
    also moved markers on every re-render — is gone.
13. [Resolved 2026-07-02] The dead `frontend/src/api/mockSse.js` leftover has been
    deleted.
14. [Resolved 2026-07-04] The firmware Tapo driver is now a **real KLAP v2**
    implementation (mbedTLS SHA/AES + `esp_http_client`), fully verified on-device.
    The project builds on **ESP-IDF v5.3.3** (v6.0.1 has breaking changes that cause a
    LoadProhibited panic on custom netif registration in `netif_callback_fn`).
15. [Resolved 2026-07-07] ~~No OTA / single-app partition table~~ — the
    partition table is now dual-OTA (`partitions_ota.csv`: `ota_0`/`ota_1` +
    `otadata`, NVS at its pre-OTA offset so provisioning survives the
    migration flash), and `esp_https_ota` + bootloader rollback are wired in
    (`ota_update.c`), triggered by the `OTA` MQTT command /
    `POST /api/cpo/gateways/{id}/ota`. **Verified end-to-end on-device
    2026-07-08** (1.1.0 → 1.1.1 over MQTT: download into `ota_1`, reboot,
    reconnect, rollback cancelled). Note: the overlay took ~3 min to
    re-establish after the OTA reboot (DERP/WireGuard re-handshake), during
    which the image stayed `PENDING_VERIFY` — correct behavior (commit only
    on broker reach), just a wide rollback-armed window.
16. [Resolved 2026-07-02] **NVS session register / offline telemetry resync** now
    implemented via `session_nvs.c` (persists active session params) and
    `offline_log.c` (64-entry NVS ring buffer). Watchdogs enforce limits locally
    even when MQTT is down; buffered telemetry is resynced on reconnect.
17. [Resolved 2026-07-03] **Over-current cutoff** is now enforced on the firmware via
    the plug's `overcurrent_status` flag (local OFF + `OVERCURRENT_CUTOFF` alarm).
18. **Telemetry topic shape** in `requirements.md` (`.../plugs/{id}/telemetry`)
    doesn't match the implemented per-gateway topic — but firmware & backend agree.
19. [Largely resolved 2026-07-08] ~~MQTT plain `mqtt://`, secured only by the
    overlay~~ — the broker now has a **TLS listener on 8883** and firmware
    ≥ 1.2.0 dials `mqtts://` (self-signed CA, chain + IP SAN validated). Not
    the spec's Noise, but transport is now TLS-encrypted + broker-authenticated
    (on top of the overlay). Plaintext 1883 stays up during the staged rollout.
20. [Resolved 2026-07-07] ~~K8s vs VM divergence~~ — moot: the manifests are
    **retired** (TD#15) and banner-marked as unmaintained reference in
    `deploy/k8s/README.md`, which records the divergence.
21. **Committed VM public IP differs across docs** — the IP is ephemeral (see
    [SECURITY.md](SECURITY.md)); stale literals now remain only in
    historical/retired runbooks (`wireguard_tunnel_setup.md`,
    `gcp_migration_runbook.md`), which are banner-marked as such.
22. [Resolved 2026-07-07] ~~Stale "EC2" wording in `deployment_checklist.md`~~
    — reworded to GCP.
23. [Resolved 2026-07-05] ~~mosquitto 9001 published but not served~~ — the
    port is no longer published (see §3.28 hardening).
24. [Resolved 2026-07-07] ~~Relay port mismatch~~ — `wireguard_tunnel_setup.md`
    now carries a RETIRED banner (Path B is gone) noting the correction:
    relay mode uses `relay_server.py` on `:8000` (`RELAY_PORT`); `:80` was
    lib mode's plug portproxy.
25. [Resolved 2026-07-07] ~~`frontend/README.md` is the stock Vite template~~
    — replaced with real package docs (stack, commands, env vars, layout,
    conventions; canonical docs stay in `docs/`).
26. [Resolved 2026-07-05] **Socket.io telemetry was non-functional** despite
    being documented as verified: `stream_telemetry_task` awaited a
    non-existent `sio.get_participants(room=...)` API and died before the
    first emit. Fixed via the room manager's registry; regression-tested.
27. [Resolved 2026-07-05] **`/api/payments/verify` trusted the client-sent
    `amount_inr`** (the checkout signature does not cover the amount). It now
    fetches and credits the Razorpay-confirmed captured amount, and rejects
    payments whose order was created for a different user.
28. [Changed 2026-07-05] Security hardening: JWT known-default secrets are
    refused (backend generates an ephemeral key; `deploy.ps1` aborts),
    committed credentials were stripped from `tools/`, `setup_duckdns.sh`,
    and `amphive_tunnel.conf` (now untracked, `.example` added), the DB
    password and MQTT bind interface are `.env`-driven
    (`POSTGRES_PASSWORD`, `MQTT_BIND_IP`), and mosquitto's unused 9001 port
    is no longer published. **Rotation of the burned secrets is still
    pending** — see [SECURITY.md](SECURITY.md).
29. [Resolved 2026-07-06] **CORS** locked to an explicit allowlist (wildcard
    removed) in `backend/main.py:187`; committed and deployed.
30. [Resolved 2026-07-06] **`stop_charging_session` ledger reconciliation.** The
    `max(0, …)` clamp used to forgive debt while still writing `amount = -final_cost`
    / `balance_after = 0`, so the ledger didn't reconcile. It now debits
    `min(final_cost, balance)` and records that same delta in `amount`,
    `balance_after`, and `coins_spent`; a forgiven shortfall is logged.
31. [Resolved 2026-07-06] **Money is now `Numeric(12,2)`** (Decimal), not `Float`,
    for `coin_balance`, `coins_spent`, ledger `amount`/`balance_after`. Wallet math
    routes through `services/money.to_money` (half-up, 2 dp); columns migrated in
    place via a guarded `ALTER … TYPE` in `db.py:_INPLACE_UPGRADES`. Energy/power
    stay `Float`. The DB-level non-negative-balance CHECK landed 2026-07-07 as
    Alembic revision `0002_wallet_non_negative` (the first post-baseline
    revision): clamps legacy negative rows to 0, then adds
    `ck_users_coin_balance_non_negative` (also declared on the `User` model).
32. [Resolved 2026-07-06] **`CpoSetup` redirect-during-render** replaced with a
    declarative `<Navigate … replace />` (the render-body `navigate()` triggered a
    React "update during render" warning and could loop under StrictMode).
33. [Resolved 2026-07-06, flashed + verified] **Firmware command parsing** moved
    from `strstr`/`sscanf` to cJSON, and the MQTT buffers were widened (topic 256,
    data 512) with an oversized/fragmented-payload guard, so a command carrying a
    `session_id` no longer truncates/corrupts. Verified on-device: ON commands
    carrying `session_id` parsed correctly through three E2E sessions (#77–79).
34. [Resolved 2026-07-06, flashed] **Firmware energy meter reset on reboot** —
    `s_energy_wh` now persists to NVS (restored on `tapo_init`, written throttled
    per 50 Wh), so post-reboot `consumed_kwh` no longer goes negative and the
    energy safety watchdog stays armed. Flashed and running; the cross-reboot
    restore itself hasn't been explicitly exercised yet (needs ≥ 50 Wh accrued to
    hit the throttled write — test sessions drew only ~3–9 W).
35. [Resolved 2026-07-06, flashed + verified] **Firmware billed the lifetime
    energy integrator, not the session.** `telemetry_task` published the raw
    monotonic `telemetry.energy_kwh` (a cross-reboot cumulative meter) as the
    telemetry `kwh`, while the backend bills that field as session energy. The
    first session on a fresh plug billed ~correctly; every subsequent one overbilled
    by the plug's entire history. Firmware now publishes session-relative energy
    (`telemetry.energy_kwh − active_session.start_energy_kwh`, clamped ≥ 0) in both
    the live payload and the offline-log buffer (`firmware/main/main.c`).
    **Verified on-device 2026-07-06:** sessions #77–79 each started at
    `kwh = 0.0000` despite prior accrual in the same boot (raw broker payloads
    checked via `mosquitto_sub`); idle telemetry reports 0; each session billed
    only its own energy.
36. [Resolved 2026-07-06] **Inbound telemetry crossed a thread boundary unsafely.**
    `_handle_gateway_telemetry` runs on the paho network thread and called
    `TelemetryStore.update()` inline, which calls `asyncio.Event.set()` to wake
    stream consumers — not thread-safe cross-thread (can miss wakeups / corrupt loop
    state). The store update is now marshaled onto the event loop via
    `loop.call_soon_threadsafe`, keeping the store single-threaded. Regression test:
    `backend/tests/test_mqtt_manager.py`.
37. [Resolved 2026-07-06, flashed] **Firmware energy
    integrator updated outside the KLAP mutex.** `tapo_get_telemetry` mutated
    `s_energy_wh`/`s_energy_last_tick`/`s_energy_persisted_wh` after releasing
    `s_mutex`; the telemetry task and the ON-handler baseline read can overlap and
    race the read-modify-write (double-count / drop a slice). The integrator update
    and throttled NVS persist now run inside the lock, with the kWh snapshotted for
    the caller (`firmware/main/tapo_protocol.c`).
38. [Resolved 2026-07-06] **`session_id` now round-trips on the live path.** The
    backend sends the DB session id (string) on `ON`; the firmware echoes it in
    telemetry; `_persist_telemetry` attributes the reading to that exact session
    (guarded ACTIVE + same plug), falling back to the plug's active session when
    absent. Previously the firmware parsed/persisted a `session_id` the backend
    never sent, so the crash-recovery field was always empty. Offline-resynced
    readings still attribute by `plug_id` (the NVS ring-buffer entry has no room
    for the id). Backend parsing regression-tested in `test_mqtt_manager.py`;
    **firmware echo verified on the wire 2026-07-06** (`mosquitto_sub` showed
    `"session_id":"79"` echoed in live telemetry; DB rows attributed to the
    correct sessions, with post-stop idle rows correctly NULL via the
    ACTIVE-session guard).
39. [Resolved 2026-07-06] **Login/`/me` crashed with `MultipleResultsFound` for
    users holding >1 ACTIVE session.** `check_and_speed_up_active_session`
    (called on every `/api/auth/login` and `/api/auth/me`) fetched "the" active
    session with `scalar_one_or_none()`, but nothing limits a user to one active
    session — a user with several (e.g. stale sessions on offline gateways) hit
    a 500 on login and session restore, locking them out of the app (observed in
    prod with 3 concurrent ACTIVE sessions). Now iterates `scalars().all()`.
    Regression tests: `backend/tests/test_active_session_speedup.py`. Related
    open gap: there is no backend session reaper — see [TODO.md](TODO.md).
40. [Resolved 2026-07-06] **Sessions could start against dead gateways and sit
    ACTIVE forever.** `send_plug_command` only confirms the broker PUBACK, so a
    start against a mock/offline gateway "succeeded", pinning the plug OCCUPIED
    with no telemetry and no way to time out server-side (prod sessions 72–76).
    `/api/sessions/start` now 409s unless `gateway_is_live`: status ONLINE
    **and** `last_seen_at` within `GATEWAY_LIVENESS_WINDOW_SEC` (default 120 s).
    Telemetry refreshes `last_seen_at` (throttled 1/min per gateway); the
    `Gateway.last_seen_at` `onupdate=now` hook was removed so unrelated row
    edits can't fake liveness. Tests: `backend/tests/test_gateway_liveness.py`.
41. [Resolved 2026-07-06] **Duplicate-insert races returned 500.**
    `/api/auth/register` and `/api/cpo/setup` did exists-check-then-insert; a
    concurrent duplicate slipped past the SELECT and surfaced the unique-index
    `IntegrityError` as a raw 500. Both now catch it at commit/flush, roll
    back, and return the same 400 as the sequential duplicate path (same
    pattern `_credit_topup` already used). Tests:
    `backend/tests/test_registration_races.py`.
44. [Resolved 2026-07-07] **Structural backlog cleared** in one round, all
    deployed + verified in prod: `main.py` split (2,384 → 221 lines; routes
    verbatim in `backend/routers/`, schemas in `schemas.py`, runtime handles
    in `state.py`, session helpers in `services/session_lifecycle.py`;
    OpenAPI parity 36 operations before/after) · N+1 queries eliminated in
    `get_my_groups`/`get_available_plugs`/`cpo_list_plugs`/
    `cpo_analytics_sessions` (driver endpoints verified byte-identical against
    prod baselines) · legacy SSE endpoint + `sse-starlette` retired ·
    `TELEMETRY_RETENTION_DAYS=90` set in prod. Remaining from that list:
    broker TLS only (deferred — the overlay already encrypts transport).
42. [Resolved 2026-07-06] **No backend session reaper** — a gateway dying
    mid-session left the session ACTIVE forever (the only timeout lived in the
    firmware, on the dead device). A lifespan-owned `SessionReaperService`
    (`services/session_reaper.py`) now auto-finalizes ACTIVE sessions with no
    telemetry for `SESSION_STALE_TIMEOUT_SEC` (default 300 s; sessions carry a
    `last_telemetry_at` stamp fed by `_persist_telemetry`, falling back to
    `started_at`). Reaped sessions bill persisted energy through the same
    `finalize_charging_session` used by `/api/sessions/stop`, which now locks
    the session row and re-checks ACTIVE — also fixing the pre-existing
    **double-stop double-debit race** (two concurrent stops both passed the
    unlocked ACTIVE check and each debited the wallet). Tests:
    `backend/tests/test_session_reaper.py`.
43. [Resolved 2026-07-07] **Zombie relay after gateway power loss.** OFF
    commands aren't retained, so a gateway that was dead when its session got
    finalized never received one — its `session_nvs` crash recovery resumed the
    session on reboot with the relay ON and nobody billing (observed on real
    hardware after the reaper finalized session 82). On a gateway `online`
    status, the backend now re-sends OFF (best-effort, no broker-ack wait) to
    each of that gateway's plugs lacking an ACTIVE session. Tests:
    `backend/tests/test_reconnect_off_republish.py`.
45. [Resolved 2026-07-07, deployed + verified in prod] **Concurrent-session
    policy decided: max 2 per user.** Nothing used to limit how many ACTIVE sessions a user could hold,
    while `/api/sessions/active` and the UI surfaced only the newest — older
    active sessions were unreachable/un-stoppable by the user (and >1 ACTIVE
    session previously crashed login, §3.39). `/api/sessions/start` now
    enforces `MAX_ACTIVE_SESSIONS_PER_USER` (env, default 2) with a 409,
    counting under a `SELECT … FOR UPDATE` user-row lock so two simultaneous
    starts serialize (lock order user → plug is consistent with the finalize
    path's session → user → plug; no cycle). `/api/sessions/active` returns
    all active sessions newest-first (the legacy top-level single-session
    fields mirror the newest entry), and the frontend tracks the full list:
    one Home banner per session, a Session-page switcher to refocus the live
    monitor, and stop acts on the focused session. Tests:
    `backend/tests/test_max_active_sessions.py`. **Verified in prod
    2026-07-07** (CI green; OpenAPI still 36 operations; seeded driver gets
    the new `{"active":false,"sessions":[]}` shape; the start path passes
    the cap check under the user lock; served frontend bundle carries the
    multi-session context). The 409-at-cap itself is unit-tested only — a
    live check would need two real sessions on the physical plug.
46. [Resolved 2026-07-09] **Wallet lost-update via the stale identity map.**
    The row-locked read-modify-write in `_credit_topup` and
    `finalize_charging_session` looked race-safe, but the request session
    already held the auth-loaded `User` (get_current_user shares the
    request's `db`), and SQLAlchemy returns that **cached instance without
    refreshing attributes** from a later `select(User).with_for_update()` —
    the lock was taken, the arithmetic ran on the balance as of auth time.
    A credit/debit committed between auth and the lock (webhook top-up
    during a stop request, reaper debit during /verify) was silently
    overwritten. Wallet writes are now DB-side atomics centralized in
    `services/wallet.py` (`credit_wallet`, `debit_wallet_clamped`); the
    logout `token_version` bump had the same shape and is also an atomic
    `UPDATE … RETURNING` now. Postgres-backed regression tests
    (stale-instance scenarios, duplicate-topup rollback, clamp, lock
    serialization): `backend/tests/test_wallet.py` — these are the first
    tests to use CI's postgres service for billing correctness (the purpose
    it was provisioned for).

### 2026-07-06 follow-up audit (statuses re-checked 2026-07-11)

*Found by a code audit on 2026-07-06. Cross-referenced to
[TECH_DEBT.md](TECH_DEBT.md) (`TD#n`) and [SECURITY.md](SECURITY.md) (`SEC §n`).
Several items were fixed by the 2026-07-08…11 work (PRs #4–#7) before this
audit merged; statuses below are as of 2026-07-11.*

47. **[Resolved 2026-07-10] Firmware safety alarms were dropped.** The firmware
    publishes `THERMAL_CUTOFF`/`OVERCURRENT_CUTOFF` (and, fw ≥ 1.5.0,
    `UNAUTHORIZED_ON`) to `amphive/gateways/{id}/alarms`, but `MQTTManager`
    subscribed only to `+/telemetry` + `+/status`, so cutoffs were unrecorded
    and un-alerted. Fixed: `+/alarms` is subscribed, alarms persist as
    `gateway_events` rows and broadcast to the CPO events feed
    (`GET /api/cpo/events` + ack) — see the alarm-handling row above.
    (TD#21)
48. **[Resolved 2026-07-11] Backend trusts the payload `plug_id`.**
    `_persist_telemetry` now verifies `plug.gateway_id == <topic gateway>` and
    drops (with a warning) readings claiming a foreign or nonexistent plug —
    the raw time-series enqueue was moved behind the same check, so neither
    session totals nor `telemetry_readings` history can be written across
    gateways. Residual: the in-memory live-stream store is still fed before
    the DB check (UI-only, no billing effect). (SEC §3, §8.5)
49. **[Resolved 2026-07-11] Unguarded telemetry casts.** `plug_id` is
    int-coerced and the `float(...)` casts in `_handle_gateway_telemetry` are
    guarded with a warn-and-drop path; non-finite values (NaN/inf) are
    rejected too. A malformed reading now logs a warning instead of throwing
    inside the paho callback. (TD#25)
50. **[Resolved 2026-07-12 — code-complete + builds clean, on-device verify
    pending] ESP32 firmware was single-plug.** Was: `main.c` (`target_plug_ip`,
    `active_session`, `active_plug_id`) and `tapo_protocol.c` (global `s_sess`,
    `s_energy_wh`) were single-instance, so commands for a second plug actuated
    the first and telemetry was published under the last-commanded id. Fixed:
    `main.c` now holds a `plugs_mutex`-guarded per-plug slot table and
    `tapo_protocol.c` exposes a per-plug `tapo_plug_t` context (its own KLAP
    session + NVS-persisted energy meter `wh_<plug_id>`); `session_nvs` persists
    **all** per-plug sessions (one atomic blob, each carrying `plug_id` +
    `local_ip`) so crash recovery restores every plug. The gateway learns a
    plug's IP from the `local_ip` the backend now ships on ON/OFF
    (`send_plug_command(..., local_ip=…)`) — no on-device roster *then* (later
    superseded by the backend-pushed retained roster, §3.57), keeping the
    per-gateway broker ACLs and the backend `plug.gateway_id` check intact
    (SEC §8.5). **Regression caught + fixed on-device 2026-07-12:** the first
    build (1.7.0) dropped the pre-multi-plug "poll the provisioned plug from
    boot" behaviour, so a session-less gateway published no idle telemetry and
    fell out of the liveness window (session starts 409'd "gateway offline").
    Fixed in **1.7.1** by pre-registering the provisioned plug at boot
    (provisional id `1`, corrected to the backend's real id by IP-adoption on the
    first command). **Single-plug path then verified end-to-end on the real
    gateway** (OTA `1.7.0`→`1.7.1`, telemetry resumed, billed session 0.014 kWh →
    0.07 coins, balance reconciled). Two-real-plug behaviour still needs a second
    unit. (TD#20, SEC §8.5)
51. **[Resolved 2026-07-11] Sessions startable on OFFLINE/MAINTENANCE plugs.**
    `start_charging_session` now 409s on any non-`AVAILABLE` status (OCCUPIED
    keeps its "in use" message; OFFLINE/MAINTENANCE get "out of service").
    Behavior change: new plugs default to OFFLINE, so a freshly registered
    plug must be set AVAILABLE in the CPO portal before its first session
    (finalize already resets used plugs to AVAILABLE). (TD#22)
52. **[Resolved 2026-07-16, fw 2.1.0-direct — on-device verify pending]
    Crash-recovery resets the duration watchdog.** `start_time_s` is tick-based
    (resets to 0 on reboot), so the recovered session's time cap used to restart
    from zero. Fixed by persisting elapsed-so-far: `session_params_t` gains
    `elapsed_s`, the telemetry task re-persists active sessions on a 30 s throttle
    (event-only persists left a long session at `elapsed_s=0`), and recovery
    restores `start_time_s = now − elapsed_s` (unsigned modular arithmetic recovers
    the true elapsed; if the session already overran, it trips on the first sweep).
    Worst-case overrun is now one throttle interval. Chose this over an SNTP
    wall-clock baseline (which would over-count power-off time). **fw
    2.2.0-direct** additionally persists the session's `max_current_a` (as
    `max_current_ma`) in the same blob, so crash recovery re-arms the
    OVERCURRENT_CAP watchdog at the session's own cap instead of the gateway
    default; the blob-size change safely voids pre-2.2.0 records (fail-closed
    load — and OTA is refused mid-session, so nothing is lost). The backend's
    `send_plug_limits` now also carries `max_current_a=effective_plug_cap(plug)`
    (from `PATCH /api/sessions/{id}/limits`), so a mid-session cap change lands
    on-device via SET_LIMITS. (TD#23)
53. **[Resolved 2026-07-16, fw 2.1.0-direct — on-device verify pending]
    Offline-resync telemetry can bill the wrong session.** The `offline_log` ring
    entry now stores a compact `uint32_t session_id` (18→22 B; `ring_meta_t` gains
    a `format_ver` so old-layout entries are cleared on init), and the resync
    payload echoes it plus `relay`/`offline:true`. The backend attributes each
    buffered reading to its exact session — dropping it if that session already
    finalized — and a new `is_offline` flag keeps a historical frame from tripping
    the REC-02 OFF-republish. Idle buffered frames omit the id + set `relay:false`
    so the idle guard drops them. Tests: `backend/tests/test_mqtt_manager.py`
    (resync attribution + stale-id inert). (TD#24)
54. **[Open, reduced scope] Device / provisioning security.** Still open:
    no flash-encryption (plaintext NVS secrets — Wi-Fi, Tapo account,
    per-gateway MQTT creds, setup code) and the boot-time fallback into the
    portal (now LOW — the portal itself is locked, see below). Since the
    audit: OTA images are **signed** (ECDSA verify-on-update) and HTTPS-only,
    the reusable-overlay-key + anonymous-broker item is **gone** (devices left
    the overlay for direct MQTT with per-gateway credentials + topic ACLs +
    TLS, 2026-07-10), and the **open setup AP + unauthenticated `/save` was
    closed in fw 1.6.0** (2026-07-11): WPA2 AP + per-device setup code on
    `/save` (constant-time check, 1 s throttle + 403 on mismatch), AP-only
    interface, 10-min idle timeout → reboot/STA retry. (SEC §8)
55. **[Partially resolved] Observability / onboarding polish.** Fixed since the
    audit: unified wallet ledger (endpoint + History tab, 2026-07-10 — TD#29),
    gateway staleness (read-time `gateway_is_live` + `gateway_online` in the
    driver API + session reaper — TD#27), the shared-`Event` latency nit
    (closed by retiring SSE 2026-07-07 — TD#32), registration validation
    (`EmailStr` + password rule, 2026-07-11 — TD#30), the portal input CSS
    (`box-sizing:border-box`, fw 1.6.0 — the `width:100%%` diagnosis was
    wrong: the HTML is printf-formatted, so `%%` already rendered as `%`),
    **backend structured logging** (2026-07-12 — TD#28, backend half):
    `backend/logging_config.py` installs a JSON-lines formatter on the root
    logger (`ts`/`level`/`logger`/`msg`/`correlation_id` + structured `extra`
    fields; env `LOG_LEVEL`/`LOG_FORMAT`), a `correlation_id` ContextVar +
    `logging.Filter` stamp every record, and a FastAPI middleware
    (`backend/main.py`) binds it from/echoes it to `X-Request-ID` per
    request — tracing an HTTP request through to the MQTT command it
    triggers for same-task calls (the paho callback thread and background
    services log `-`). Hot-path f-strings converted in `routers/auth.py`,
    `routers/sessions.py`, `services/session_lifecycle.py`,
    `services/mqtt_manager.py`. The broker log is now also mirrored to a file
    on the previously-unused `mosquitto_log` volume (durable across container
    recreation) with the primary stdout stream size/count-bounded via the
    compose `logging:` driver. Tests: `backend/tests/test_logging.py`.
    And the **CPO admin audit log** (2026-07-12 — TD#26): a new `audit_logs`
    table + `services/audit.py` (`record_audit`/`try_record_audit`, non-fatal
    — a write failure is logged, never breaks the admin action) records
    gateway/plug/group create, plug status change, group delete, and
    access-code regen with actor/tenant/target, readable via
    `GET /api/cpo/audit`. Gateway/plug **delete** are pre-named in the action
    taxonomy but have no endpoint yet to hook (no such CPO routes exist).
    The **portal Wi-Fi pre-check** (TD#31, second half) shipped in fw
    2.1.0-direct: `/save` briefly associates in AP+STA mode to the submitted
    SSID/password (fail-open — only a definite association failure blocks the
    save; ≤ 20 s bound; the single radio may briefly drop the installer's
    phone). The plug-IP half is moot — plug IPs come from the retained roster
    (fw ≥ 2.0.0), so the portal no longer collects them. Still open: firmware
    `ESP_LOGI` WARN/ERROR now forward to the `/logs` topic (fw 2.1.0), but the
    backend does not subscribe/persist them yet (TD#28, firmware half
    partially closed). (TD#26, TD#28, TD#31)
56. **[Resolved 2026-07-12] No session-sized authorization hold — overage
    forgiven past the wallet.** `/api/sessions/start` only checked a flat
    `MIN_START_BALANCE_COINS` floor, and `finalize_charging_session` billed
    `min(final_cost, live balance)` — so a session could start on ₹50 and
    rack up an arbitrarily larger bill, with everything past the wallet
    silently forgiven (MARKET_GAP_ANALYSIS.md §3; SECURITY.md §5). Fixed:
    the start path now reserves `min(available_balance, max_kwh × rate)`
    onto the new nullable `ChargingSession.hold_coins` (Alembic
    `0013_auth_holds`, chained onto `0011_disputes` — the actual head by the
    time this branch was cut, re-chaining the orchestrator's own note that a
    sibling "0011" migration was still in flight when this feature's task
    brief was written), and `finalize_charging_session` caps the debit at
    `min(final_cost, hold_coins)` — the revenue leak is closed for every
    held session. `available_balance` (`services/wallet.py`) — coin_balance
    minus what the user's OTHER active sessions already hold — is computed
    under the existing user-row lock, so two concurrent starts can't
    double-reserve the same coins; a hold is purely a read-time reservation
    and never touches `coin_balance` or its non-negative CHECK. The
    mqtt_manager balance-exhaustion auto-stop threshold switched from the
    whole wallet balance to the session's own hold for the same reason (a
    sibling session's balance isn't this session's to spend past). Legacy
    sessions (`hold_coins IS NULL`) keep the old behavior exactly — this is
    forward-only. Tests: `backend/tests/test_auth_holds.py`.
57. **[Done 2026-07-15 — shipped: PR #52 merged, backend deployed, fw
    `2.0.0-direct` OTA'd + verified live] Multi-plug was implicit — the captive
    portal collected one "Target Plug IP".** A gateway drives up to 4 P110s, but provisioning asked for a
    single plug IP and the firmware only learned additional plugs lazily from the
    `local_ip` on an ON/OFF command (plus a boot-time provisional slot for idle
    telemetry) — so multi-plug read as one-plug-per-ESP and duplicated
    `plugs.local_ip` on-device, going stale on a DHCP change. Fixed by a
    **backend-pushed retained plug roster** on `amphive/gateways/{gw}/config`
    (`{plug_id, local_ip, max_current_a}`, no name — keeps 4 plugs under the
    512 B inbound buffer): `MQTTManager.publish_plug_roster` /
    `_publish_roster_for_gateway` publish it on gateway `online`, after a plug
    create/update (`routers/cpo.py _publish_gateway_roster`), and after an agent
    discovery upsert. The firmware (`2.0.0-direct`) subscribes on connect and
    reconciles its slot table (`handle_plug_roster`: add / re-IP / flag-remove;
    the telemetry task frees a dropped plug via the new `tapo_plug_destroy`, never
    an active session), and the captive-portal plug-IP field, the `target_plug`
    NVS key, and the provisional boot slot were removed. Operators can now fix a
    plug's IP after DHCP drift (`local_ip` added to `PUT /api/cpo/plugs/{id}`).
    No broker ACL change (the topic is inside `amphive/gateways/%u/#`) and no DB
    migration. **Rollout is ordered: the backend must be deployed before the
    firmware OTA** — the firmware now relies on the retained roster for idle
    telemetry, the job the removed provisional boot slot used to do. **Shipped
    2026-07-15:** PR #52 merged (`6350350`), backend deployed, gateway
    `1cc3abb4fb54` OTA'd `1.9.0`→`2.0.0-direct` (built on ESP-IDF v5.3.3);
    `last_seen` then advanced on 2.0.0, confirming the device learned plug 1 from
    the retained roster and idle telemetry flows (validation with 2+ physical
    plugs still pending — only one plug is on the bench). Tests:
    `backend/tests/test_plug_roster.py` + the roster cases in
    `test_mqtt_manager.py`. (TD#20, SEC §8.5)
