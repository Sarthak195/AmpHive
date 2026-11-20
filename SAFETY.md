# SAFETY & DISCLAIMER

## WARNING: HIGH-VOLTAGE ELECTRICAL HAZARD

**THIS SOFTWARE CONTROLS HIGH-VOLTAGE AND HIGH-CURRENT ELECTRICAL HARDWARE.** It actuates real mains-voltage relays and modulates power delivery to electric vehicle charging equipment.

### NOT FOR PRODUCTION USE

- **This software is provided for reference and educational purposes ONLY.**
- **DO NOT deploy this software to control real charging hardware in production.**
- **This code is NOT electrically certified**, does not carry any electrical safety certification (no UL, CE, BIS, IEC, or equivalent listing), and has not been independently safety audited.

### KNOWN SAFETY-RELEVANT DEFECTS

This codebase contains known defects with potential safety implications, including but not limited to:
- Incomplete current-limiting enforcement on embedded devices
- Offline reconciliation gaps that may permit overcharging or unexpected device state
- Insufficient input validation and fault tolerance
- No redundant safety cutoff mechanisms

### SOFTWARE RELAY CONTROL

- Charge-complete auto-stop (F1) is per-session opt-in and only ever switches the relay OFF, through the same `finalize_charging_session` path as a driver stop; it never switches anything ON.
- Personal API tokens (F7), the Home Assistant/evcc recipes and the OCPP bridge (`tools/ocpp_bridge`, not the C2 central system below) are read-only: none of them can switch a relay. A third-party start/stop would bypass the wallet hold, circuit admission and reservations, so no write path exists by design.
- Charge routines (C5, wave 3, deployed 2026-11-19). A recurring weekly routine energizes the socket unattended, with no proof a car is connected, exactly like a one-off scheduled start, and has the same safety posture. Each due occurrence only inserts an ordinary scheduled QueuedCharge (`services/routines.py` `materialize_routines`). The plug can only be switched on by `reap_queued_starts_once` -> `begin_active_session`, with every admission check, wallet hold, reservation gate, opening-hours check and current cap unchanged. A routine must carry a time or energy limit (DB check constraint `ck_charge_routines_has_limit` plus API validation), so an absent car never leaves the socket live without bound. Low-power idle detection still ends a no-draw session. A missed occurrence (backend down past the 5 minute window) is skipped, never started late. While queued or scheduled charging is disabled for its plug, each occurrence is skipped (the routine stays enabled and resumes when the gate is back on, with one "paused" notice per outage); after 3 consecutive failed occurrences the routine turns itself off. A routine never queues an occurrence whose start passed before it was created, re-enabled, or had its days or start time edited, so it never queues a row whose `not_before` is already past (the same rule as `POST /api/sessions/queue`, which requires now < `not_before`). The materialize pass locks the routine row and then the plug row FOR UPDATE, so its check for another driver's WAITING row is serialized with the queue endpoint's scheduled path: two drivers can never both hold scheduled rows on one socket. Disabled or closed accounts never materialize. Turning a routine off or deleting it cancels its queued row if that row has not started.
- Opening hours (C4, wave 3, deployed 2026-11-19) only ever block: a closed plug refuses new starts (`begin_active_session`, under the plug row lock), new bookings, new one-off schedules and routines whose start time falls in a closed slot (409 `plug_closed`), and queued/scheduled auto-starts (left WAITING until the plug opens or the row's TTL lapses; a routine's row is no different). A start's duration limit can only be shortened, to end by the closing time and `max_session_minutes`; the firmware duration watchdog then ends it. A raised limit is capped the same way but never below the session's current limit. There is no new relay-off trigger: a session already running is never stopped at closing time.
- Ready by (F6R, wave 4 R2, built, not deployed). The ready-by planner (`POST /api/sessions/queue/ready-by`) only chooses a start time. It creates ONE scheduled queued charge through the same path as `POST /api/sessions/queue` with `not_before`, so its safety posture is identical to a scheduled start: the plug is switched on only by the session reaper through `begin_active_session`, with the wallet hold, admission, reservations, opening hours and current caps all applied at start. An energy or duration limit is mandatory, and the duration limit is tightened so the run is meant to stop by the driver's deadline (it counts from the actual start, so it can end up to about 31 minutes after T when the start slips; see docs/IMPLEMENTATION_STATUS.md 0h). It is one contiguous run: there is no PAUSED state, no split window and no re-energizing. If the deadline cannot be met the request is refused and nothing is scheduled.
- Idle fee (C1, wave 4 R2, built, not deployed) never switches a plug ON or OFF. It only reads telemetry, writes idle state on transitions, and bills at finalize, and it is opt-in per tariff (0 = off) with a grace of at least 15 minutes and a per-session cap. It is capped by the session's own hold and the wallet, is never charged to an auto-stop-when-full session, and is voided for good the moment the car draws again on a live frame. `idle_fee_notice` is in `ALWAYS_PUSH_TYPES`, so quiet hours and notification preferences never mute it. Rules: docs/IDLE_FEE.md.
- Reservation approval and no-show fee (C8, wave 4 R2, built, not deployed). Nothing here can switch a plug ON: a `pending` reservation never authorizes or blocks a start, and the no-show fee is a ledger debit written only by the reaper sweep, once per reservation, at most the available balance, and never when the plug or gateway may have been offline, in maintenance, closed or in use (any unknown signal means skip, so a mistake falls on the side of not charging). Rules: docs/RESERVATION_APPROVAL.md.
- OCPP 1.6J central system (C2, wave 4 R2, built, not deployed): a development module, OFF by default and OFF in production (`OCPP_CSMS_ENABLED`, no proxy route reaches it). It adds no actuation path: the central system never sends an OCPP CALL (no RemoteStart, RemoteStop or ChangeConfiguration) and never actuates anything; an OCPP charger energizes itself. StartTransaction and StopTransaction run `begin_active_session` / `finalize_charging_session` with `actuate=False` and publish nothing. The start passes every admission check the walk-up start does (account, concurrent cap, group, plug AVAILABLE, reservations, scheduled starts, opening hours, circuit caps, wallet floor and hold); any refusal is answered Blocked or Invalid. A charge point's plugs sit on a backing gateway that is never marked live, so no app start path (walk-up, queued, routine, ready-by) can claim them. The shared backend stop paths (the driver's app Stop, wallet exhaustion, limit auto-stops, the stale and time-limit reapers, a reservation force-stop) still publish their usual OFF / SET_INTERVAL to the topic `amphive/gateways/ocpp-<id>/...`; nothing subscribes there and no broker credentials exist for `ocpp-*` ids, so those publishes are inert. A backend-side stop therefore bills and frees the session but cannot switch the charger off; the charger's own safety and StopTransaction govern the hardware, and energy delivered after a backend-side stop is not billed. A connector reported Faulted is taken out of service (the safe direction). Validated only against a simulator. Rules: docs/OCPP_CSMS.md.
- Per-member prices and group invites (C9, wave 4 R2, built, not deployed) only change the rate and the hold sizing inside `begin_active_session`; no new path switches a plug ON.

### RISK ASSUMPTION & LIABILITY

**IMPROPER USE OF THIS SOFTWARE CAN AND WILL CAUSE:**
- Severe electrical shock or electrocution
- Fire and thermal hazards
- Equipment damage
- Injury or death to persons and animals

**ALL RISK IS ASSUMED BY YOU, THE USER.** The authors and copyright holders assume no liability for any harm, injury, death, property damage, or financial loss arising from any use of this software.

---

## BILLING & PAYMENT DEFECTS

The billing and payment handling code (Razorpay integration, wallet/credit system, session charging, and transaction settlement) is **unaudited and contains known money-handling bugs.**

- **DO NOT use this software to process real payments.**
- **DO NOT charge real users for energy or services using this code.**
- Known defects include incomplete transaction reconciliation, lack of atomicity guarantees, and inadequate audit trails.

---

## RECOMMENDED USE CASES ONLY

This codebase is suitable ONLY for:
- **Reference and study** — understanding architectural patterns for EV-charging systems
- **Closed test environments** — simulation with completely isolated, non-powered hardware (no actual electrical connections)
- **Educational projects** — with explicit safety training and expert supervision

---

## BEFORE USING THIS CODE

1. **Read the full documentation** in [`docs/SECURITY.md`](docs/SECURITY.md) for a detailed inventory of open gaps.
2. **Do not rely on this code for safety-critical decisions.** Implement independent hardware interlocks and software cutoffs.
3. **Obtain electrical and financial liability insurance** if you must test with real hardware or payments.
4. **Engage qualified electrical engineers and financial auditors** to design redundancy and compliance mechanisms that are NOT present in this codebase.
5. **Comply with all local electrical codes** (NEC, IEC, etc.) and financial regulations (PCI-DSS, local payment laws, etc.).

---

## DISCLAIMER IN FULL

THIS SOFTWARE IS PROVIDED "AS IS," WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF, OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

**See [`LICENSE`](LICENSE) for the full MIT License text.**
