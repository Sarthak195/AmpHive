"""
Gateway commissioning checklist (C10): pure evaluation of a gateway's
read-only health from rows the backend already holds. No I/O here, so every
threshold and roll-up is unit-testable without a database; the route in
routers/cpo/_gateways.py loads the rows (bounded, indexed queries) and calls
`build_selftest`.

Nothing here probes hardware: no MQTT command, no relay actuation, no ping.
The Wi-Fi signal is the live value from telemetry "wifi_rssi" (fw >= 2.10.0,
at most every 5 min, held in memory by the MQTT manager) while it is fresh,
else the value the gateway reported at its last boot; the label says which.

Liveness reuses the existing rules (never a second threshold):
gateway_is_live (GATEWAY_LIVENESS_WINDOW_SEC) and plug_is_powered
(PLUG_POWER_STALE_SEC) from services/session_lifecycle.py.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.database.models import Gateway, GatewayEvent, Plug
from backend.services.session_lifecycle import gateway_is_live, plug_is_powered
from backend.services.versioning import version_sort_key

# Wi-Fi RSSI, dBm. Below WARN is marginal, below FAIL is unreliable.
RSSI_WARN_DBM = -70
RSSI_FAIL_DBM = -80

# A live reading counts while younger than this: two 5-minute report intervals
# plus slack, so one lost QoS 0 frame does not flip the check back to boot.
LIVE_RSSI_FRESH_SEC = 11 * 60

# A plug with no telemetry for this long is a fail rather than a warn, and a
# PLUG_UNREACHABLE only counts inside this lookback.
LOOKBACK_HOURS = 24

# GATEWAY_BOOT.detail is built by services/mqtt/alarms.py _gateway_boot_detail:
# "... Wi-Fi RSSI -79 dBm after 3 attempts ...".
_RSSI_RE = re.compile(r"Wi-Fi RSSI (-?\d+) dBm")

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"


def _check(key: str, label: str, status: str, detail: str,
           fix_hint: Optional[str] = None,
           observed_at: Optional[datetime] = None) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "fix_hint": fix_hint if status in (WARN, FAIL) else None,
        "observed_at": observed_at.isoformat() if observed_at else None,
    }


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _ago(dt: datetime, now: datetime) -> str:
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 90:
        return f"{secs} s ago"
    if secs < 5400:
        return f"{round(secs / 60)} min ago"
    if secs < 172800:
        return f"{round(secs / 3600)} h ago"
    return f"{round(secs / 86400)} d ago"


def parse_boot_rssi(detail: Optional[str]) -> Optional[int]:
    """The dBm value out of a GATEWAY_BOOT event detail, or None."""
    m = _RSSI_RE.search(detail or "")
    return int(m.group(1)) if m else None


def check_online(gateway: Gateway, now: datetime) -> Dict[str, Any]:
    seen = _aware(gateway.last_seen_at)
    label = "Gateway online"
    if gateway_is_live(gateway, now):
        return _check("online", label, PASS, f"Connected, last heard {_ago(seen, now)}.",
                      observed_at=seen)
    seen_txt = f"last heard {_ago(seen, now)}" if seen else "never heard from"
    return _check(
        "online", label, FAIL, f"Not connected ({seen_txt}).",
        "Check the gateway has power and Wi-Fi, then re-check in a minute.",
        seen,
    )


def check_wifi(boot_event: Optional[GatewayEvent], now: datetime,
               live: Optional[Tuple[int, datetime]] = None) -> Dict[str, Any]:
    """`live` is (rssi, received_at) from telemetry. It wins while it is
    fresh and newer than the boot report; otherwise the boot value is used."""
    boot_at = _aware(boot_event.created_at) if boot_event is not None else None
    live_at = _aware(live[1]) if live is not None else None
    if (live is not None and live_at is not None
            and now - live_at <= timedelta(seconds=LIVE_RSSI_FRESH_SEC)
            and (boot_at is None or live_at >= boot_at)):
        label, rssi, at = "Wi-Fi signal (live)", live[0], live_at
        detail = f"{rssi} dBm live, {_ago(at, now)}."
    else:
        label = "Wi-Fi signal (at boot)"
        boot_rssi = parse_boot_rssi(boot_event.detail) if boot_event is not None else None
        if boot_rssi is None or boot_at is None:
            return _check("wifi", label, UNKNOWN,
                          "No boot report with a signal reading yet (needs firmware 2.8.0 or newer).")
        rssi, at = boot_rssi, boot_at
        detail = f"{rssi} dBm at boot, {_ago(at, now)}."
    hint = "Move the gateway closer to the router or add an access point."
    if rssi < RSSI_FAIL_DBM:
        return _check("wifi", label, FAIL, detail, hint, at)
    if rssi < RSSI_WARN_DBM:
        return _check("wifi", label, WARN, detail, hint, at)
    return _check("wifi", label, PASS, detail, observed_at=at)


def _release_variant(version: str) -> Optional[str]:
    key = version_sort_key(version)
    return None if key[0] < 0 else key[3]


def check_firmware(fw: Optional[str], active_versions: Iterable[str]) -> Dict[str, Any]:
    """Gateway firmware vs the newest active release of the same variant (the
    "-direct" style suffix); unknown when either side cannot be compared."""
    label = "Firmware up to date"
    if not fw or _release_variant(fw) is None:
        return _check("firmware", label, UNKNOWN, "The gateway has not reported a firmware version.")
    variant = _release_variant(fw)
    same = [v for v in active_versions if _release_variant(v) == variant]
    if not same:
        return _check("firmware", label, UNKNOWN,
                      f"Running {fw}; no active release of the same kind to compare with.")
    newest = max(same, key=version_sort_key)
    if version_sort_key(fw)[:3] < version_sort_key(newest)[:3]:
        return _check("firmware", label, WARN, f"Running {fw}; {newest} is available.",
                      "Use Update firmware on the gateways page.")
    return _check("firmware", label, PASS, f"Running {fw}, the newest release.")


def check_plug_telemetry(plug: Plug, now: datetime) -> Dict[str, Any]:
    key, label = f"plug_{plug.id}", f"Charger readings: {plug.name}"
    last = _aware(plug.last_telemetry_at)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    if plug_is_powered(plug, now):
        return _check(key, label, PASS, f"Reporting, last reading {_ago(last, now)}.",
                      observed_at=last)
    if last is None or last < cutoff:
        return _check(
            key, label, FAIL,
            f"No readings in the last {LOOKBACK_HOURS} h.",
            "Check the plug is powered and on the same network as the gateway.",
            last,
        )
    return _check(key, label, WARN, f"Readings paused, last one {_ago(last, now)}.",
                  "Check the plug is powered and reachable from the gateway.", last)


def check_roster(plugs: Sequence[Plug], now: datetime) -> Dict[str, Any]:
    """The backend gets no acknowledgement for the retained roster, but a
    gateway only reports plugs it holds, so fresh readings from every plug
    prove delivery; anything else cannot be told."""
    label = "Charger roster delivered"
    if not plugs:
        return _check("roster", label, WARN, "No chargers are added to this gateway yet.",
                      "Add a charger so the gateway has something to poll.")
    if all(plug_is_powered(p, now) for p in plugs):
        return _check("roster", label, PASS,
                      f"All {len(plugs)} charger(s) are reporting, so the gateway has its roster.")
    return _check("roster", label, UNKNOWN,
                  "The gateway does not acknowledge its roster; some chargers are not reporting yet.")


def check_unreachable(events: Sequence[GatewayEvent], plugs: Sequence[Plug]) -> Dict[str, Any]:
    """`events` are the PLUG_UNREACHABLE / PLUG_RECOVERED rows of the last
    LOOKBACK_HOURS. A plug whose NEWEST such event is UNREACHABLE is down."""
    label = "No unresolved plug faults"
    newest: Dict[Optional[int], GatewayEvent] = {}
    for ev in events:
        cur = newest.get(ev.plug_id)
        if cur is None or _aware(ev.created_at) > _aware(cur.created_at):
            newest[ev.plug_id] = ev
    down = [ev for ev in newest.values() if ev.event_type == "PLUG_UNREACHABLE"]
    if not down:
        return _check("plug_faults", label, PASS,
                      f"No unanswered plug-unreachable reports in the last {LOOKBACK_HOURS} h.")
    names = {p.id: p.name for p in plugs}
    listed = ", ".join(sorted(names.get(ev.plug_id, "a charger") for ev in down))
    return _check(
        "plug_faults", label, WARN,
        f"The gateway could not reach: {listed}. No recovery reported since.",
        "Check the plug's power and Wi-Fi, and that its address has not changed.",
        max(_aware(ev.created_at) for ev in down),
    )


def overall_status(checks: Iterable[Dict[str, Any]]) -> str:
    statuses = {c["status"] for c in checks}
    if FAIL in statuses:
        return FAIL
    if WARN in statuses:
        return WARN
    return PASS


def build_selftest(
    gateway: Gateway,
    plugs: Sequence[Plug],
    boot_event: Optional[GatewayEvent],
    plug_events: Sequence[GatewayEvent],
    active_versions: Iterable[str],
    now: Optional[datetime] = None,
    live_rssi: Optional[Tuple[int, datetime]] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    checks: List[Dict[str, Any]] = [
        check_online(gateway, now),
        check_wifi(boot_event, now, live_rssi),
        check_firmware(gateway.firmware_version, active_versions),
        check_roster(plugs, now),
        *(check_plug_telemetry(p, now) for p in plugs),
        check_unreachable(plug_events, plugs),
    ]
    return {
        "gateway_id": gateway.id,
        "generated_at": now.isoformat(),
        "overall": overall_status(checks),
        "checks": checks,
    }
