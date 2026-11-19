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
- Personal API tokens (F7), the Home Assistant/evcc recipes and the OCPP bridge are read-only: none of them can switch a relay. A third-party start/stop would bypass the wallet hold, circuit admission and reservations, so no write path exists by design.
- Charge routines (C5, wave 3, not deployed). A recurring weekly routine energizes the socket unattended, with no proof a car is connected, exactly like a one-off scheduled start, and has the same safety posture. Each due occurrence only inserts an ordinary scheduled QueuedCharge (`services/routines.py` `materialize_routines`). The plug can only be switched on by `reap_queued_starts_once` -> `begin_active_session`, with every admission check, wallet hold, reservation gate, opening-hours check and current cap unchanged. A routine must carry a time or energy limit (DB check constraint `ck_charge_routines_has_limit` plus API validation), so an absent car never leaves the socket live without bound. Low-power idle detection still ends a no-draw session. A missed occurrence (backend down past the 5 minute window) is skipped, never started late. While queued or scheduled charging is disabled for its plug, each occurrence is skipped (the routine stays enabled and resumes when the gate is back on, with one "paused" notice per outage); after 3 consecutive failed occurrences the routine turns itself off. A routine never queues an occurrence whose start passed before it was created, re-enabled, or had its days or start time edited, so it never queues a row whose `not_before` is already past (the same rule as `POST /api/sessions/queue`, which requires now < `not_before`). The materialize pass locks the routine row and then the plug row FOR UPDATE, so its check for another driver's WAITING row is serialized with the queue endpoint's scheduled path: two drivers can never both hold scheduled rows on one socket. Disabled or closed accounts never materialize. Turning a routine off or deleting it cancels its queued row if that row has not started.
- Opening hours (C4, wave 3, not deployed) only ever block: a closed plug refuses new starts (`begin_active_session`, under the plug row lock), new bookings, and queued/scheduled auto-starts (left WAITING until the plug opens or the row's TTL lapses; a routine's row is no different). A start's duration limit can only be shortened, to end by the closing time and `max_session_minutes`; the firmware duration watchdog then ends it. A raised limit is capped the same way but never below the session's current limit. There is no new relay-off trigger: a session already running is never stopped at closing time.

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
