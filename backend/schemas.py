"""
AmpHive Pydantic request/response schemas.

Extracted verbatim from main.py (2026-07-07, TD#7 split). One module for all
routers — schemas are cross-cutting (e.g. PlugResponse is returned by both the
driver and CPO plug routes).
"""
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


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


class CpoGroupCreateRequest(BaseModel):
    """Create a new charger group."""
    name: str
    is_public: bool = False


class CpoGroupUpdateRequest(BaseModel):
    """Update an existing charger group."""
    name: Optional[str] = None
    is_public: Optional[bool] = None
    regenerate_access_code: bool = False
