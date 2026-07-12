"""
AmpHive Pydantic request/response schemas.

Extracted verbatim from main.py (2026-07-07, TD#7 split). One module for all
routers — schemas are cross-cutting (e.g. PlugResponse is returned by both the
driver and CPO plug routes).
"""
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


# --- Auth Schemas ---

class RegisterRequest(BaseModel):
    # EmailStr + password length rule (TD#30): previously any string was
    # accepted verbatim as an email and one-character passwords were legal.
    # Max 72: bcrypt silently truncates beyond 72 bytes, so longer would give
    # a false sense of entropy. Login deliberately stays a plain `str` so
    # accounts created before this rule can still sign in.
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    full_name: str

class LoginRequest(BaseModel):
    email: str
    password: str

class AuthResponse(BaseModel):
    token: str
    user: dict

class UserResponse(BaseModel):
    id: int
    email: str
    full_name: str
    role: str
    coin_balance: float
    # [Auth holds] Additive: coin_balance minus coins held by this user's
    # OTHER active sessions (services/wallet.py available_balance) — what a
    # NEW session-start would actually be sized against. Always populated by
    # /api/auth/me; Optional only for schema forward-safety (mirrors
    # PlugResponse.price_per_kwh's convention below).
    available_balance: Optional[float] = None


# --- Session Schemas ---

class SessionStartRequest(BaseModel):
    plug_id: int
    # Bounded so a client can't disable the firmware safety watchdog by sending
    # an absurd limit. 1 s .. 24 h, and 0.1 .. 100 kWh.
    max_duration_seconds: int = Field(default=14400, gt=0, le=86400)  # 4 h default, 24 h cap
    max_kwh: float = Field(default=30.0, gt=0, le=100.0)              # 30 kWh default, 100 kWh cap

class SessionStopRequest(BaseModel):
    session_id: int


# --- Gateway & Plug Schemas ---

class GatewayRegisterRequest(BaseModel):
    gateway_id: str  # MAC/UUID
    name: str
    vpn_ip: str
    tenant_id: int

class PlugRegisterRequest(BaseModel):
    gateway_id: str
    name: str
    local_ip: str
    plug_model: str = "tapo_p110"
    group_id: Optional[int] = None  # [P2] Optional charger group assignment


# --- Group Schemas ---

class JoinGroupRequest(BaseModel):
    access_code: str

class GroupResponse(BaseModel):
    id: int
    name: str
    is_public: bool
    plug_count: int

class ReservationWindow(BaseModel):
    """A [start_at, end_at) booking window, ISO-8601 UTC strings — the shape
    PlugResponse.next_reservation carries (see routers/reservations.py for
    the full reservation objects)."""
    start_at: str
    end_at: str


class PlugResponse(BaseModel):
    id: int
    name: str
    status: str
    current_power_w: float
    plug_model: str
    group_name: Optional[str] = None
    # Effective map coordinates: the plug's own, else its gateway's, else None.
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    # Whether the plug's gateway is live right now (ONLINE + recently seen). The
    # driver UI uses this to warn that a plug is unreachable before a start is
    # attempted, instead of only discovering it via a 409 at session start.
    gateway_online: bool = True
    # The resolved coins-per-kWh rate this plug would bill a session at right
    # now (services/pricing.py resolve_rate_for_plug: plug tariff -> group
    # tariff -> tenant default -> the global COINS_PER_KWH env fallback).
    # Always populated by the router; Optional only for schema forward-safety.
    price_per_kwh: Optional[float] = None
    # True when the plug belongs to a non-public group — which, given the
    # accessibility filter, the requesting user must have joined. Ungrouped
    # and public-group plugs are False. The driver Home page uses this to
    # section the charger list into "your chargers" vs "public chargers".
    is_private: bool = False
    # [Reservations] Whether a BOOKED reservation covers right now (after
    # lazy no-show expiry — services/reservations.py), whether the CALLER
    # holds it (the Home card shows "Reserved for you" instead of warning
    # the holder off their own slot), when the covering window ends (ISO
    # UTC; null unless reserved_now), and the next FUTURE window (strictly
    # start_at > now — the covering one is described by reserved_until).
    # Populated on both the detail and list endpoints (the list computes
    # them in one grouped query, no N+1).
    reserved_now: bool = False
    reserved_now_by_me: bool = False
    reserved_until: Optional[str] = None
    next_reservation: Optional[ReservationWindow] = None
    # [Plug watches] True when the CURRENT user has an armed one-shot
    # "notify me when free" watch on this plug (POST/DELETE
    # /api/plugs/{id}/watch). Drives the Home card's bell toggle; cleared
    # server-side when the watch fires (services/plug_watch.py).
    watching: bool = False


class GatewayEventResponse(BaseModel):
    """A gateway/plug operational event (alarm, safety cutoff, OTA notice)."""
    id: int
    gateway_id: str
    plug_id: Optional[int] = None
    event_type: str
    severity: str
    detail: Optional[str] = None
    acknowledged: bool
    created_at: Optional[str] = None


# --- Payment Schemas ---

class LedgerEntryResponse(BaseModel):
    """A single wallet ledger row — a top-up credit or a session debit."""
    id: int
    amount: float                 # signed: positive = credit, negative = debit
    transaction_type: str         # e.g. "topup", "session_debit"
    direction: str                # "credit" | "debit" (derived from the sign)
    description: Optional[str] = None
    balance_after: float
    session_id: Optional[int] = None
    razorpay_payment_id: Optional[str] = None
    created_at: Optional[str] = None
    # [Auth holds] Additive: the driver's CURRENT available balance
    # (coin_balance minus what active sessions hold right now — see
    # services/wallet.py available_balance), repeated on every row of a
    # GET /api/wallet/ledger response — NOT a per-transaction historical
    # figure like balance_after. Optional for schema forward-safety.
    available_balance: Optional[float] = None


class CreateOrderRequest(BaseModel):
    amount_inr: float  # Amount in Rupees (e.g. 100 for ₹100)

class CreateOrderResponse(BaseModel):
    order_id: str
    amount: int       # Amount in paise
    currency: str
    key_id: str       # Razorpay Key ID (needed by frontend checkout)

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    # Deprecated and IGNORED: the credited amount is always fetched from
    # Razorpay's API server-side. Kept optional so older clients that still
    # send it don't get a 422.
    amount_inr: Optional[float] = None


# --- Notification Schemas ---

class NotificationResponse(BaseModel):
    """One driver notification (session stop, low balance, charger offline,
    safety cutoff, top-up credit)."""
    id: int
    type: str
    severity: str                 # "info" | "warning" | "critical"
    title: str
    body: str
    plug_id: Optional[int] = None
    session_id: Optional[int] = None
    read: bool
    created_at: Optional[str] = None


class NotificationListResponse(BaseModel):
    notifications: list[NotificationResponse]
    unread_count: int


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=255)
    auth: str = Field(min_length=1, max_length=64)


class PushSubscribeRequest(BaseModel):
    """The browser PushSubscription.toJSON() shape."""
    endpoint: str = Field(min_length=1, max_length=1024)
    keys: PushSubscriptionKeys


class PushUnsubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=1024)


# --- Direct Mode Schemas ---

class DirectPlugRequest(BaseModel):
    """Optional request body for direct plug control. If plug_ip is not provided,
    falls back to the TAPO_PLUG_IP environment variable."""
    plug_ip: Optional[str] = None


# --- CPO Schemas ---

class CpoSetupRequest(BaseModel):
    """Request body for CPO onboarding — creates a new tenant."""
    tenant_name: str


class CpoGatewayCreateRequest(BaseModel):
    """Register a new gateway under the CPO's tenant.

    ``gateway_id`` is the device's MAC (lower-case, no separators) — the
    firmware derives it from the hardware and shows it in the setup portal,
    so the operator just copies it here. ``vpn_ip`` is legacy/overlay-only
    (direct-MQTT devices don't use it); optional and defaults to empty.
    """
    gateway_id: str   # device MAC (matches the firmware-derived gateway_id)
    name: str
    vpn_ip: str = ""


class CpoGatewayOtaRequest(BaseModel):
    """Trigger an OTA firmware update on a gateway.

    `firmware_url` must be an **https** URL the *gateway* can reach (the
    public OTA image bucket — see docs/FIRMWARE.md) — not a URL relative to
    the backend. Plain http is rejected: direct-MQTT gateways fetch images
    across the public internet, and firmware ≥ 1.4.0 refuses non-TLS
    downloads anyway (and verifies the image's ECDSA app signature before
    installing). The firmware downloads the image into its passive OTA slot
    and reboots.
    """
    firmware_url: str = Field(pattern=r"^https://", max_length=512)


class CpoPlugCreateRequest(BaseModel):
    """Register a new plug on one of the CPO's gateways."""
    gateway_id: str
    name: str
    local_ip: str
    plug_model: str = "tapo_p110"
    group_id: Optional[int] = None
    # Optional geolocation; when omitted the plug inherits its gateway's coords.
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)


class CpoPlugUpdateRequest(BaseModel):
    """Update an existing plug's details."""
    name: Optional[str] = None
    group_id: Optional[int] = None
    # Status string matching PlugStatus enum values: available, occupied, offline, maintenance
    status: Optional[str] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)


class CpoPlugMaintenanceRequest(BaseModel):
    """
    Body for POST /api/cpo/plugs/{id}/maintenance — the dedicated operator
    maintenance workflow (fault console), distinct from the general status
    setter on CpoPlugUpdateRequest: "enter" always succeeds, "clear" is
    refused (409) while the plug has an ACTIVE session.
    """
    action: str  # "enter" | "clear"
    note: Optional[str] = Field(default=None, max_length=255)


class CpoGroupCreateRequest(BaseModel):
    """Create a new charger group."""
    name: str
    is_public: bool = False


class CpoGroupUpdateRequest(BaseModel):
    """Update an existing charger group."""
    name: Optional[str] = None
    is_public: Optional[bool] = None
    regenerate_access_code: bool = False


# --- Tariff (per-CPO/per-site pricing) Schemas ---

class CpoTariffCreateRequest(BaseModel):
    """Create a new tariff (pricing plan) under the caller's tenant."""
    name: str = Field(min_length=1, max_length=100)
    # Coins per kWh. Inbound as float like other money-ish request fields
    # (e.g. CreateOrderRequest.amount_inr) — the router normalizes it via
    # services/money.to_money before it ever touches the Numeric(12,2) column.
    price_per_kwh: float = Field(gt=0, le=1000)


class CpoTariffUpdateRequest(BaseModel):
    """Update an existing tariff's name and/or rate."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    price_per_kwh: Optional[float] = Field(default=None, gt=0, le=1000)


class CpoTariffAssignRequest(BaseModel):
    """
    Assign or unassign the tariff that bills a plug / charger group / the
    tenant default — the target is named by the URL path (plug_id, group_id,
    or the tenant-default endpoint); this body only carries which tariff.
    `tariff_id=None` clears the assignment (falls back to the next link in
    the resolution chain — see services/pricing.py resolve_rate_for_plug).
    """
    tariff_id: Optional[int] = None

# --- Session Dispute / Refund Schemas ---

class DisputeCreateRequest(BaseModel):
    """Body for POST /api/sessions/{id}/dispute. Length-bounded so a driver
    gives the CPO enough to act on, without accepting an unbounded blob."""
    reason: str = Field(min_length=10, max_length=1000)


class CpoDisputeResolveRequest(BaseModel):
    """Body for POST /api/cpo/disputes/{id}/resolve.

    ``action`` is validated in the router (matching the existing
    CpoPlugUpdateRequest.status convention) rather than via a Literal type,
    so an invalid value gets the same clean 400 the rest of the CPO router
    uses instead of a generic 422.

    ``refund_coins`` is optional on "approve" — the router defaults it to the
    session's coins_spent — and ignored on "reject". When given, must be
    strictly positive; the router additionally enforces that the cumulative
    approved refund for a session never exceeds coins_spent.
    """
    action: str
    refund_coins: Optional[float] = Field(default=None, gt=0)
    note: Optional[str] = Field(default=None, max_length=1000)


class DisputeResponse(BaseModel):
    """A session dispute, driver-filed and CPO-resolved."""
    id: int
    session_id: int
    tenant_id: int
    driver_user_id: int
    reason: str
    status: str
    resolution_note: Optional[str] = None
    refund_coins: Optional[float] = None
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_by_user_id: Optional[int] = None


# --- Plug Reservation Schemas (feat/reservations) ---

class ReservationCreateRequest(BaseModel):
    """Body for POST /api/reservations. Window rules (min/max duration,
    advance horizon, past-start grace) are enforced in the router against
    services/reservations.py policy so the 400s carry precise messages
    instead of generic 422s."""
    plug_id: int
    start_at: datetime
    end_at: datetime

    @field_validator("start_at", "end_at")
    @classmethod
    def _coerce_utc(cls, v: datetime) -> datetime:
        # All timestamps are stored tz-aware UTC. The frontend sends ISO
        # strings with an offset (Date.toISOString() => "...Z"); a naive
        # datetime from an older/naive client is interpreted as UTC rather
        # than rejected.
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class ReservationResponse(BaseModel):
    """One reservation. `user_name`/`is_mine` are populated on the per-plug
    schedule (group members see who holds each slot — the private-society
    use case); `plug_name` on the owner/CPO lists."""
    id: int
    plug_id: int
    user_id: int
    tenant_id: int
    start_at: str
    end_at: str
    status: str
    session_id: Optional[int] = None
    created_at: Optional[str] = None
    plug_name: Optional[str] = None
    user_name: Optional[str] = None
    is_mine: Optional[bool] = None


class MyReservationsResponse(BaseModel):
    """GET /api/reservations/my: upcoming BOOKED windows (soonest first) +
    recent history (everything else, newest first, capped)."""
    upcoming: list[ReservationResponse]
    history: list[ReservationResponse]
