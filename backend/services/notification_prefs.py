"""
[Notification prefs] Per-user push toggles and quiet hours (feature C7).

Stored as users.notification_prefs (JSONB, NULL = defaults) and enforced in
services/notifications.notify(). The in-app feed row and the live Socket.io
event are ALWAYS delivered; these prefs only decide whether a Web Push goes
out (notify() sends no email).

Push decision, in order:
  1. always_pushed(type, severity): severity critical or warning, or a type in
     ALWAYS_PUSH_TYPES. Pushed even in quiet hours, whatever the prefs say,
     and decided without touching the database.
  2. A type with a per-type toggle (SUPPRESSIBLE_TYPES) that the user turned
     off: no push.
  3. Inside the user's quiet hours: no push (not queued for later).
  4. Otherwise push.

Quiet hours are wall-clock HH:MM in the user's tenant timezone (the same zone
pricing uses for tariff slots, falling back to its default). The window is
half-open, [start, end): start is quiet, end is not. start later than end
wraps across midnight (22:00 to 07:00).
"""
import re
from datetime import datetime, tzinfo
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# The non-suppressible allowlist. Pushed even in quiet hours and never mutable
# by a toggle. Safety and fault events reach drivers as session_stopped (the
# stop receipt carries safety cutoffs such as OVERCURRENT/THERMAL and the
# current-cap stop) and operators as gateway_fault (OVERCURRENT_CUTOFF,
# THERMAL_CUTOFF, UNAUTHORIZED_ON, OTA_FAILED); there is no separate
# notification type per fault.
ALWAYS_PUSH_TYPES = frozenset({
    # Billing receipts and refunds.
    "session_stopped",
    "topup_credited",
    "topup_refunded",
    "promo_redeemed",
    # Safety and faults.
    "orphan_off",
    "charger_offline",
    "unmetered_consumption",
    # Wave-1 operator alerts (services/operator_alerts.py).
    "gateway_offline",
    "gateway_online",
    "gateway_fault",
    # F1 "your car looks fully charged" notice.
    "charge_full",
    # Charge routines (services/routines.py, C5).
    "routine_started",
    "routine_disabled",
    # A one-off scheduled or outage-queue auto-start: like routine_started, the
    # relay energizes with nobody at the plug, so it pushes once even in quiet
    # hours. (session_started stays suppressible; rate_changed stays muted by
    # quiet hours: it is forward-only and the session_stopped receipt, which
    # always pushes, carries the totals.)
    "queued_charge_started",
})

# Severities that always push, whatever the type.
ALWAYS_PUSH_SEVERITIES = frozenset({"critical", "warning"})

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class PushToggles(BaseModel):
    """Per-type push switches, one per suppressible type, all on by default."""
    model_config = ConfigDict(extra="forbid")

    session_started: bool = True
    nearly_done: bool = True
    power_dropped: bool = True


SUPPRESSIBLE_TYPES = frozenset(PushToggles.model_fields)


class QuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    start: str = "22:00"
    end: str = "07:00"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        if not _HHMM.match(value):
            raise ValueError("Use 24-hour HH:MM, for example 22:00.")
        return value

    @model_validator(mode="after")
    def _non_empty_window(self) -> "QuietHours":
        if self.enabled and self.start == self.end:
            raise ValueError("Quiet hours need different start and end times.")
        return self


class NotificationPrefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    push: PushToggles = Field(default_factory=PushToggles)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)


def load_prefs(raw: Any) -> NotificationPrefs:
    """The stored JSON as a model. NULL or anything unreadable is the defaults
    (all pushes on, no quiet hours), so a bad row can only push more."""
    if not isinstance(raw, dict):
        return NotificationPrefs()
    try:
        return NotificationPrefs.model_validate(raw)
    except ValidationError:
        return NotificationPrefs()


def always_pushed(type: str, severity: str) -> bool:
    """True for the non-suppressible allowlist (no prefs or DB needed)."""
    return severity in ALWAYS_PUSH_SEVERITIES or type in ALWAYS_PUSH_TYPES


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def in_quiet_hours(quiet: QuietHours, local: datetime) -> bool:
    """Whether the wall-clock time of `local` is inside [start, end)."""
    if not quiet.enabled:
        return False
    start, end = _minutes(quiet.start), _minutes(quiet.end)
    if start == end:
        return False
    now = local.hour * 60 + local.minute
    if start < end:
        return start <= now < end
    return now >= start or now < end


def push_allowed(type: str, severity: str, prefs: NotificationPrefs,
                 now: datetime, tz: Optional[tzinfo]) -> bool:
    """The full push decision for one notification (see the module doc)."""
    if always_pushed(type, severity):
        return True
    if type in SUPPRESSIBLE_TYPES and not getattr(prefs.push, type):
        return False
    local = now.astimezone(tz) if tz is not None else now
    return not in_quiet_hours(prefs.quiet_hours, local)
