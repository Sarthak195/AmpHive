"""
AmpHive Pydantic request/response schemas.

Extracted verbatim from main.py (2026-07-07, TD#7 split). One module for all
routers — schemas are cross-cutting (e.g. PlugResponse is returned by both the
driver and CPO plug routes).
"""
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

# --- Auth Schemas ---

class RegisterRequest(BaseModel):
    # EmailStr + password length rule (TD#30): previously any string was
    # accepted verbatim as an email and one-character passwords were legal.
    # Max 72: bcrypt silently truncates beyond 72 bytes, so longer would give
    # a false sense of entropy. Login deliberately stays a plain `str` so
    # accounts created before this rule can still sign in.
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    # users.full_name is String(150) (database/models.py) — bounded to match,
    # else an oversized value reaches the DB unchecked and raises a DataError
    # (uncaught 500) instead of a clean 422 here.
    full_name: str = Field(max_length=150)

class LoginRequest(BaseModel):
    email: str
    password: str

class ForgotPasswordRequest(BaseModel):
    # Plain `str` (not EmailStr) on purpose: the endpoint answers the same
    # generic 200 for ANY input (no account enumeration), and login is also a
    # plain str so pre-EmailStr accounts can recover too.
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    # Same 8-72 rule as RegisterRequest (bcrypt truncates at 72 bytes).
    password: str = Field(min_length=8, max_length=72)

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
    # [Opt-in charging limits] Charging limits are OPT-IN: omitted (None)
    # means "charge until I stop it" — no hidden default duration/energy
    # (2026-08-02; previously defaulted to 14400 s / 30.0 kWh here, which
    # silently capped every session that didn't ask for a limit). When a
    # limit IS given it's still bounded so a client can't disable the
    # firmware safety watchdog with an absurd value: 1 s .. 24 h, and
    # 0.1 .. 100 kWh. An unlimited session still stops on the real safety
    # nets — balance exhaustion, gateway-offline/staleness reaping,
    # overcurrent, plug caps (see services/mqtt_manager.py
    # firmware_duration/firmware_max_kwh for what's actually sent to the
    # gateway when these are None — NOT 0 and NOT an omitted field, both of
    # which the firmware reads unsafely).
    max_duration_seconds: Optional[int] = Field(default=None, gt=0, le=86400)
    max_kwh: Optional[float] = Field(default=None, gt=0, le=100.0)


class QueueChargeRequest(BaseModel):
    """[Queued charge] Body for POST /api/sessions/queue — queue an auto-start
    on a plug whose gateway is online but line power is out. Same stop-condition
    bounds as SessionStartRequest (snapshotted onto the QueuedCharge so the
    eventual auto-start bills like a walk-up) — both optional, default None
    (no limit; the eventual auto-start charges until stopped)."""
    plug_id: int
    max_duration_seconds: Optional[int] = Field(default=None, gt=0, le=86400)
    max_kwh: Optional[float] = Field(default=None, gt=0, le=100.0)


class SessionLimitsUpdateRequest(BaseModel):
    """PATCH a RUNNING session's stop conditions ("start now, set the target
    later"). Both optional — send whichever you're changing; the same bounds as
    SessionStartRequest apply. The endpoint 400s if neither is given."""
    max_duration_seconds: Optional[int] = Field(default=None, gt=0, le=86400)
    max_kwh: Optional[float] = Field(default=None, gt=0, le=100.0)


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
    # [Plug power] Whether the plug is reporting fresh telemetry (drawing power)
    # right now — services/session_lifecycle.py plug_is_powered. A plug can be
    # on a live gateway yet have lost mains/relay power; the driver UI uses this
    # to warn before a start, which the same-message 409 also enforces.
    plug_powered: bool = True
    # [Queued charge] Whether the driver can QUEUE a charge on this plug right
    # now: gateway online AND plug NOT powered AND the CPO has queued charging
    # enabled for it (services/session_start.py queued_charging_enabled). The
    # Home card shows a "Queue charge" CTA instead of the "notify me when free"
    # bell when true. Populated on GET /api/plugs/available and /api/plugs/{id}.
    queue_available: bool = False
    # The resolved coins-per-kWh rate this plug would bill a session at right
    # now (services/pricing.py resolve_rate_for_plug: plug tariff -> group
    # tariff -> tenant default -> the global COINS_PER_KWH env fallback).
    # Always populated by the router; Optional only for schema forward-safety.
    price_per_kwh: Optional[float] = None
    # [Pricing v2] The NEXT coins-per-kWh rate and the wall-clock instant it
    # takes effect, when a time-of-day slot boundary changes the price later
    # today (services/pricing.py resolve_price_display). BOTH None for a flat
    # tariff or when the next boundary keeps the same rate — so the driver UI
    # shows "5 now · 6 from 18:00" only on a real, imminent change.
    price_next_per_kwh: Optional[float] = None
    price_changes_at: Optional[str] = None
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
    # [Discovery] Advertised charger specs — 0030_plug_specs. NULL when the
    # CPO hasn't set them. rated_power_w is the SPEC (what the driver is told
    # the socket supports), deliberately distinct from any load-balancing cap.
    rated_power_w: Optional[int] = Field(default=None, ge=0)
    connector_type: Optional[str] = Field(default=None, max_length=32)
    # [Favorites] True when the CURRENT user has starred this plug (POST/DELETE
    # /api/plugs/{id}/favorite) — a standing preference, unlike `watching`
    # (one-shot, self-clears). services/... none yet: plain CRUD on
    # UserFavorite.
    is_favorite: bool = False


class PublicPlugResponse(BaseModel):
    """Minimal, UNAUTHENTICATED projection of a PUBLIC-group plug for the
    pre-signup discovery map (`GET /api/plugs/public`). Deliberately narrow: only
    what a passer-by needs to see a charger on a map — id, name, location, live
    availability, and price. It EXCLUDES every per-user field (watching,
    reserved-by-me, queue), all session/driver state, and network details
    (local_ip). Private/society plugs are never returned by that endpoint at all.
    Starting a charge still requires an account."""
    id: int
    name: str
    status: str
    latitude: float
    longitude: float
    price_per_kwh: Optional[float] = None
    # Whether the plug's gateway is reachable right now — lets the public map
    # color a marker offline (same meaning as PlugResponse.gateway_online).
    gateway_online: bool = True
    # [Discovery] Advertised specs — safe to expose publicly (not per-user,
    # not network detail). Same meaning as PlugResponse's fields.
    rated_power_w: Optional[int] = Field(default=None, ge=0)
    connector_type: Optional[str] = Field(default=None, max_length=32)


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


class GatewayLogResponse(BaseModel):
    """One forwarded firmware log line (diagnostics feed — see GatewayLog)."""
    id: int
    gateway_id: str
    level: str
    message: str
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
    amount_inr: float = Field(gt=0, le=10000, allow_inf_nan=False)  # Amount in Rupees (e.g. 100 for ₹100)

    @field_validator("amount_inr")
    @classmethod
    def _reject_sub_paise_precision(cls, v: float) -> float:
        # Razorpay only bills whole paise: services/payments.py create_order()
        # charges int(round(amount_inr * 100)) paise, while the order's
        # notes.coins is computed from this raw, un-quantized amount_inr. A
        # request with more than 2 decimal places (e.g. 123.455) would make
        # those two numbers diverge by a fraction of a coin. Reject rather
        # than silently round, so the client sees an explicit 422 instead of
        # AmpHive quietly crediting a different amount than it displayed.
        if round(v, 2) != v:
            raise ValueError("amount_inr must have at most 2 decimal places")
        return v

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

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, v: str) -> str:
        # The endpoint is a browser-supplied URL the backend later POSTs to on
        # every notification — reject internal/loopback/non-https targets at
        # the edge so a stored endpoint can never become an SSRF primitive.
        # Deferred import: keeps schemas.py free of the service module's
        # DB/socketio imports at load time. Shared with the send-path guard so
        # both layers apply the exact same rules.
        from backend.services.notifications import is_safe_push_endpoint

        if not is_safe_push_endpoint(v):
            raise ValueError(
                "endpoint must be an https URL for a public push service "
                "(no IP literals, loopback, or internal hosts)."
            )
        return v


class PushUnsubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=1024)


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

    This is the "manual registration" path — still supported for lab/legacy
    use, but the preflashed-unit flow (POST /api/cpo/gateways/claim below)
    is now the primary way a CPO adds a gateway.
    """
    gateway_id: str   # device MAC (matches the firmware-derived gateway_id)
    name: str
    vpn_ip: str = ""


class CpoGatewayClaimRequest(BaseModel):
    """Body for POST /api/cpo/gateways/claim — bind a preflashed, admin-minted
    unclaimed gateway (POST /api/admin/gateways/inventory) to the caller's
    tenant. ``claim_code`` is whatever the buyer typed off the unit's label
    (the router normalizes case/whitespace/dashes before lookup); ``name`` is
    optional — omit it to keep the inventory row's placeholder name, or set
    it to something the CPO's team will recognize.

    Deliberately NOT validated with a strict length/charset pattern here: a
    malformed code should fail exactly like a wrong-but-well-formed one (the
    router returns the same generic 404 either way — no enumeration oracle
    on code format).
    """
    claim_code: str = Field(min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, max_length=100)


class CpoGatewayOtaRequest(BaseModel):
    """Trigger an OTA firmware update on a gateway.

    Two ways to pick the image, exactly one required:
    - `release_id` (preferred): an id from GET /api/cpo/firmware-releases —
      the version-picker flow (feat/ota-version-picker) that replaced
      hand-pasting a URL. The router resolves it to that release's `url`.
    - `firmware_url`: a raw **https** URL, kept as an admin-only escape
      hatch for a one-off/unregistered image — the router (not this schema,
      which doesn't see the caller's role) rejects it with 403 for a
      non-admin caller. Must be an https URL the *gateway* can reach (the
      public OTA image bucket — see docs/FIRMWARE.md), not a URL relative to
      the backend. Plain http is rejected: direct-MQTT gateways fetch images
      across the public internet, and firmware ≥ 1.4.0 refuses non-TLS
      downloads anyway (and verifies the image's ECDSA app signature before
      installing).

    Either way, the firmware downloads the image into its passive OTA slot
    and reboots.
    """
    release_id: Optional[int] = None
    firmware_url: Optional[str] = Field(default=None, pattern=r"^https://", max_length=512)

    @model_validator(mode="after")
    def _exactly_one_target(self):
        if bool(self.release_id) == bool(self.firmware_url):
            raise ValueError(
                "Provide exactly one of release_id (pick a registered firmware "
                "release) or firmware_url (admin-only custom URL)."
            )
        return self


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
    # [Discovery] Advertised specs shown to drivers (0030_plug_specs). Both
    # optional — omit to leave unset.
    rated_power_w: Optional[int] = Field(default=None, ge=0)
    connector_type: Optional[str] = Field(default=None, max_length=32)


class CpoPlugUpdateRequest(BaseModel):
    """Update an existing plug's details."""
    name: Optional[str] = None
    group_id: Optional[int] = None
    # Status string matching PlugStatus enum values: available, occupied, offline, maintenance
    status: Optional[str] = None
    # The plug's LAN IP. Lets an operator fix it after a DHCP change without
    # re-provisioning; on change the backend republishes the retained roster so
    # the gateway re-IPs the plug's slot (omit to leave unchanged).
    local_ip: Optional[str] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    # [Caps] Per-plug current cap in amps for circuit admission. Send 0 to clear
    # it back to the default hardware cutoff; omit to leave unchanged.
    max_current_a: Optional[float] = Field(default=None, ge=0, le=100)
    # [Queued charge] Per-plug overrides of the tenant queued-charge defaults
    # (omit to leave unchanged, mirroring max_current_a). queued_charging_enabled
    # sets the plug's explicit on/off override; auto_start_delay_min is the
    # debounce (minutes) — send 0 to clear it back to the tenant default (NULL).
    queued_charging_enabled: Optional[bool] = None
    auto_start_delay_min: Optional[int] = Field(default=None, ge=0, le=1440)
    # [Discovery] Advertised specs shown to drivers (0030_plug_specs). Omit
    # to leave unchanged; send rated_power_w=0 / connector_type="" to clear
    # back to unset (NULL) — the same sentinel-clear convention as
    # max_current_a above.
    rated_power_w: Optional[int] = Field(default=None, ge=0)
    connector_type: Optional[str] = Field(default=None, max_length=32)


class CpoPlugMaintenanceRequest(BaseModel):
    """
    Body for POST /api/cpo/plugs/{id}/maintenance — the dedicated operator
    maintenance workflow (fault console), distinct from the general status
    setter on CpoPlugUpdateRequest: "enter" always succeeds, "clear" is
    refused (409) while the plug has an ACTIVE session.
    """
    action: str  # "enter" | "clear"
    note: Optional[str] = Field(default=None, max_length=255)


class CpoProfileUpdateRequest(BaseModel):
    """[Queued charge] Update the CPO's tenant-level settings — the queued-charge
    defaults every plug inherits unless it carries its own override
    (CpoPlugUpdateRequest). All optional; omitted fields are left unchanged."""
    # Master on/off default for queued charging across the tenant's plugs.
    queued_charging_enabled: Optional[bool] = None
    # Continuous-power debounce (minutes) before a queued charge auto-starts.
    auto_start_delay_min: Optional[int] = Field(default=None, ge=0, le=1440)
    # How long a WAITING queued charge lives (minutes) before it expires.
    queue_ttl_min: Optional[int] = Field(default=None, ge=1, le=43200)  # up to 30 days
    # [GST invoices] Seller identity stamped onto issued tax invoices
    # (services/invoices.py). Empty string clears the field back to NULL.
    gstin: Optional[str] = Field(default=None, max_length=15)
    legal_name: Optional[str] = Field(default=None, max_length=120)
    invoice_prefix: Optional[str] = Field(default=None, max_length=12)


class CpoGroupCreateRequest(BaseModel):
    """Create a new charger group."""
    name: str
    is_public: bool = False


class CpoGroupUpdateRequest(BaseModel):
    """Update an existing charger group."""
    name: Optional[str] = None
    is_public: Optional[bool] = None
    regenerate_access_code: bool = False
    # [Caps] The group's shared circuit capacity in amps (session-start
    # admission). Send 0 to clear it (no limit); omit to leave unchanged.
    max_current_a: Optional[float] = Field(default=None, ge=0, le=1000)


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


class CpoTariffSlotCreateRequest(BaseModel):
    """[Pricing v2] A time-of-day pricing window on a tariff. Half-open
    [start_min, end_min) minute-of-day in the tenant's local timezone; a
    wrap-around window (e.g. 22:00–06:00) is modeled as TWO slots. days_mask is
    a Mon=bit0..Sun=bit6 weekday bitmask (default: all days). start<end and
    non-overlap are enforced in the router (services/pricing.py slot_overlaps).
    price_per_kwh is normalized via services/money.to_money before it hits the
    Numeric(12,2) column, same as the parent tariff."""
    start_min: int = Field(ge=0, le=1439)
    end_min: int = Field(ge=1, le=1440)
    price_per_kwh: float = Field(gt=0, le=1000)
    days_mask: int = Field(default=127, ge=1, le=127)


class CpoTariffSlotUpdateRequest(BaseModel):
    """Update a TOD slot — every field optional; omitted fields keep their
    current value, and the router re-validates the resulting window."""
    start_min: Optional[int] = Field(default=None, ge=0, le=1439)
    end_min: Optional[int] = Field(default=None, ge=1, le=1440)
    price_per_kwh: Optional[float] = Field(default=None, gt=0, le=1000)
    days_mask: Optional[int] = Field(default=None, ge=1, le=127)

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


# --- Plug Problem Reports ("report a problem with this charger") ---

PLUG_REPORT_CATEGORIES = ("damaged", "wrong_info", "unsafe", "other")


class PlugReportCreateRequest(BaseModel):
    """Body for POST /api/plugs/{plug_id}/report. `category` is one of
    PLUG_REPORT_CATEGORIES (validated below, same clean-400-in-router
    convention as CpoPlugReportResolveRequest.action rather than a Literal
    type, so an invalid value reads the same as the rest of this API).
    `description` is length-bounded the same as DisputeCreateRequest.reason —
    enough for the CPO to act on, without an unbounded blob."""
    category: str
    description: str = Field(min_length=10, max_length=1000)

    @field_validator("category")
    @classmethod
    def _validate_category(cls, v: str) -> str:
        if v not in PLUG_REPORT_CATEGORIES:
            raise ValueError(
                f"Invalid category '{v}'. Must be one of {list(PLUG_REPORT_CATEGORIES)}."
            )
        return v


class CpoPlugReportResolveRequest(BaseModel):
    """Body for POST /api/cpo/plug-reports/{id}/resolve.

    ``action`` is validated in the router (matching CpoDisputeResolveRequest's
    convention) rather than via a Literal type. "acknowledge" moves an open
    report to ACKNOWLEDGED without closing it (mirrors acking a GatewayEvent —
    "seen, working on it"); "resolve" closes it out (stamps resolved_at /
    resolved_by_user_id). Both accept an optional note."""
    action: str
    note: Optional[str] = Field(default=None, max_length=1000)


class PlugReportResponse(BaseModel):
    """A driver-filed plug problem report."""
    id: int
    plug_id: int
    tenant_id: int
    driver_user_id: int
    category: str
    description: str
    status: str
    resolution_note: Optional[str] = None
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_by_user_id: Optional[int] = None


# --- CPO Offline (cash) Top-ups ---

class CpoTopupCreateRequest(BaseModel):
    """Body for POST /api/cpo/topups. `amount_coins` is bounded server-side
    against the tenant's available top-up pool (services/payouts.py's
    available_pool_coins), not just > 0 here — the router 409s with the
    actual figure when it's exceeded. `note` is optional, short free text
    ("cash, pump 3").

    `driver_email` is a plain `str` (not EmailStr) on purpose, matching the
    forgot-password request above: this is a lookup of an EXISTING account
    (the router 404s unknowns), and EmailStr's validator rejects reserved
    TLDs like the seeded `@amphive.test` accounts (caught by CI 2026-07-21)."""
    driver_email: str
    amount_coins: float = Field(gt=0)
    note: Optional[str] = Field(default=None, max_length=500)


class CpoTopupResponse(BaseModel):
    """A CPO offline (cash) top-up — the CPO-side accounting record; the
    driver's own wallet ledger shows the matching CPO_TOPUP credit."""
    id: int
    tenant_id: int
    actor_user_id: Optional[int] = None
    actor_email: Optional[str] = None
    driver_user_id: Optional[int] = None
    driver_email: Optional[str] = None
    amount_coins: float
    note: Optional[str] = None
    created_at: Optional[str] = None


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


# --- Admin console (redesign/ui-v3 — backend/routers/admin.py) -------------
# Appended at the end of the file, same rationale as the reservation/dispute
# sections: avoids touching shared blocks that parallel branches also edit.


class AdminUserUpdateRequest(BaseModel):
    """Body for PATCH /api/admin/users/{id}. Both optional; omitted fields
    are left unchanged. `role` is validated in the router (matching the
    CpoPlugUpdateRequest.status convention) so an invalid value gets a clean
    400 instead of a generic 422."""
    role: Optional[str] = None
    is_disabled: Optional[bool] = None


class AdminAdjustBalanceRequest(BaseModel):
    """Body for POST /api/admin/users/{id}/adjust-balance. `amount_coins` is
    SIGNED: positive credits the wallet, negative debits it (the router
    floors the resulting balance at 0 — the DB CHECK constraint
    ck_users_coin_balance_non_negative forbids negative balances). `reason`
    is mandatory: an admin moving money must say why (it lands in the ledger
    description and the audit row)."""
    amount_coins: float
    reason: str = Field(min_length=3, max_length=200)


class AdminGatewayMintRequest(BaseModel):
    """Body for POST /api/admin/gateways/inventory — pre-register a preflashed
    gateway as UNCLAIMED inventory (tenant_id NULL) with a fresh claim code,
    for the "print claim code + label" step of the manufacturing runbook
    (deploy/docs/preflashed_unit_runbook.md). ``gateway_id`` is the device's
    real MAC (same format as CpoGatewayCreateRequest.gateway_id — no regex
    enforced here either, since not every gateway_id in this system is a MAC,
    e.g. the fake-plug simulator's `fakeplug-gw-01`). ``name`` is optional —
    unset defaults to a placeholder the CPO can rename on claim."""
    gateway_id: str = Field(min_length=1, max_length=50)
    name: Optional[str] = Field(default=None, max_length=100)


class AdminFirmwareReleaseCreateRequest(BaseModel):
    """Body for POST /api/admin/firmware-releases — register a firmware
    build that's already been published (see docs/FIRMWARE.md §7
    "Publishing an OTA image") so it shows up in the CPO OTA version picker
    instead of a hand-pasted URL. This endpoint does not upload/store the
    binary — `url` must already point at a reachable, publicly-fetchable
    image (today: the `gs://amphive-fw` bucket); registering-without-hosting
    is on the caller.

    `version` mirrors `firmware/CMakeLists.txt`'s PROJECT_VER format (e.g.
    "2.3.0-direct") and must be unique — the router returns 400 on a repeat.
    """
    version: str = Field(min_length=1, max_length=32, pattern=r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
    url: str = Field(pattern=r"^https://", max_length=512)
    notes: Optional[str] = Field(default=None, max_length=500)
