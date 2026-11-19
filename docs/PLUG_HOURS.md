# Plug opening hours, max session length, amenities and access note (C4)

Per-plug settings a CPO edits in the charger edit modal (CPO portal, Chargers,
Edit, "Hours and access"). Schema: alembic `0049_plug_hours` (four nullable
columns on `plugs`). Logic: `backend/services/plug_hours.py` (pure, DB-free).

## Data

| Column | Type | Meaning |
|---|---|---|
| `opening_hours` | JSONB, nullable | List of `{days, start, end}` windows in the tenant's local time (`Tenant.timezone`). NULL = open 24x7 (every existing plug). |
| `max_session_minutes` | INTEGER, nullable | Cap on one session's length. NULL = none. |
| `amenities` | JSONB, nullable | Tags from a fixed allowlist: restroom, cafe, wifi, covered, lit, cctv, accessible. |
| `access_note` | VARCHAR(280), nullable | Plain text for drivers, rendered as a text node, never HTML. |

Windows use TariffSlot semantics: `days` is a Mon=bit0..Sun=bit6 mask, `start`
and `end` are minutes of the day, half-open `[start, end)`. On save, a window
whose `end` is not after `start` crosses midnight and is stored as two windows
(`[start, 1440)` on its days, `[0, end)` on the following days); `end` 0 means
midnight. Overlapping windows are refused (400) with `pricing.slot_overlaps`.
Touching windows chain, so 22:00-24:00 plus 00:00-06:00 closes at 06:00.

## Enforcement (blocking is the safe direction)

- **Start** (`services/session_start.py begin_active_session`, under the plug
  row lock, so walk-up and queued starts both pass through it): a closed plug,
  or one closing within 60 s, is refused with 409 ("This charger is closed
  right now. It opens at HH:MM."). Otherwise the session's own
  `max_duration_seconds` is clamped to end by the current closing time and by
  `max_session_minutes`, before the hold is sized, so the hold, the stored
  limit and the gateway ON command all carry the clamped value. The firmware's
  duration watchdog then ends the session.
- **Running sessions are never stopped at closing time.** There is no new
  force-stop path and no new remote relay-off trigger.
- **Reservations** (`POST /api/reservations`): the whole `[start_at, end_at)`
  must fall inside the hours, else 409.
- **Queued and scheduled starts** (`session_reaper.reap_queued_starts_once`):
  a closed plug leaves the row WAITING until it opens or the row's TTL
  expires. The row is not failed early. (Hours edited after admission, or an
  outage-queue row, still take this path.)
- **Scheduled create** (`POST /api/sessions/queue` with `not_before`): refused
  with 409 `plug_closed` when the plug is closed at `not_before` or closes
  within 60 s of it (the same clamp as a start), so a closed slot never holds
  the socket against other drivers. The outage queue has no start time and is
  not checked.
- **Charge routines** (`POST /api/me/routines`, and a `PATCH` that changes
  the days or start time or turns the routine on): refused with 409
  `plug_closed` when the plug is closed at the start time on any selected
  weekday (`plug_hours.closed_weekdays`, pure wall-clock).
- **Raising a running session's limit** (`PATCH /api/sessions/{id}/limits`):
  the new `max_duration_seconds` is capped to the same bounds, measured from
  the session's `started_at`, so the clamp cannot be bypassed.
- Ad-hoc closures use maintenance mode; there are no blackout dates.
- NULL hours and NULL max length skip all of this with no extra query.

## API

- `PUT /api/cpo/plugs/{id}`: optional `opening_hours` (list of
  `{days, start, end}`, at most 28), `max_session_minutes` (0..1440),
  `amenities` (list), `access_note` (max 280). An omitted key is unchanged; an
  explicit null, `[]`, `0` or `""` clears it.
- `GET /api/cpo/plugs`: echoes the four fields; `access_note` passes through
  `redact_for`, so a read-only cpo_viewer sees phone numbers, emails, IPs and
  code tails masked.
- `GET /api/plugs/available` and `GET /api/plugs/{id}`: add `opening_hours`,
  `open_now`, `hours_changes_at` (next close when open, next open when
  closed), `max_session_minutes`, `amenities`, `access_note`. All are null for
  a 24x7 plug with nothing set. `access_note` is masked the same way for a
  cpo_viewer here. The unauthenticated `/api/plugs/public`
  projection is unchanged (the note may hold a gate code).

## Driver UI

The plug card, the Home "charge now" lookup and the map's reviews modal show "Open now, closes HH:MM"
or "Closed, opens HH:MM" in the plug's `tenant_timezone`, the max session
length, amenity chips and the note. A closed plug offers no Charge button
(Reserve stays). The Home filter bar adds "Any hours / Open now / Open 24x7".
