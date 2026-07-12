"""
AmpHive SQLAlchemy ORM Models
==============================
Defines all database models using SQLAlchemy 2.0 mapped_column syntax.
Covers: Tenants, Users, Gateways, Plugs, ChargingSessions, LedgerTransactions,
ChargerGroups, and GroupMemberships.

Phase 2 additions (marked with [P2]):
- ChargerGroup: public vs private (access-code-gated) plug groups
- GroupMembership: many-to-many join table for user <-> private group access
- Plug.group_id: links each plug to a charger group
"""

import enum
from datetime import datetime
from typing import List, Optional
from decimal import Decimal
from sqlalchemy import CheckConstraint, Column, Integer, BigInteger, String, Float, Numeric, Boolean, ForeignKey, DateTime, Enum as SQLEnum, Index, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

# --- Python Enums corresponding to DB Custom Types ---

class UserRole(str, enum.Enum):
    ADMIN = "admin"
    CPO = "cpo"
    DRIVER = "driver"

class GatewayStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"

class PlugStatus(str, enum.Enum):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"

class SessionStatus(str, enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    PAID = "paid"
    CANCELLED = "cancelled"

class TransactionType(str, enum.Enum):
    TOPUP = "topup"
    SESSION_DEBIT = "session_debit"
    REFUND = "refund"

# --- SQLAlchemy Model Classes ---

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    # Relationships
    users: Mapped[List["User"]] = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    gateways: Mapped[List["Gateway"]] = relationship("Gateway", back_populates="tenant", cascade="all, delete-orphan")
    sessions: Mapped[List["ChargingSession"]] = relationship("ChargingSession", back_populates="tenant", cascade="all, delete-orphan")
    # [P2] A tenant (CPO) can own multiple charger groups
    charger_groups: Mapped[List["ChargerGroup"]] = relationship("ChargerGroup", back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    # DB-level backstop for the wallet: application code already row-locks and
    # clamps debits to the available balance, but only this constraint makes a
    # negative balance impossible through any write path. Added in migration
    # 0002 (which also clamps pre-existing negative rows to 0).
    __table_args__ = (
        CheckConstraint("coin_balance >= 0", name="ck_users_coin_balance_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    role: Mapped[UserRole] = mapped_column(SQLEnum(UserRole, name="user_role", values_callable=lambda x: [e.value for e in x]), default=UserRole.DRIVER, nullable=False)
    # Money: NUMERIC(12,2) → Decimal in Python. All wallet math goes through
    # services.money.to_money to avoid float rounding drift. See models money note.
    coin_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    # Monotonic token epoch: embedded in every JWT as the `tv` claim and
    # re-checked on each request. Bumping it (logout, password change, admin
    # revoke) invalidates all previously issued tokens for this user without a
    # blacklist table. Added in migration 0003.
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    # Relationships
    tenant: Mapped[Optional[Tenant]] = relationship("Tenant", back_populates="users")
    sessions: Mapped[List["ChargingSession"]] = relationship("ChargingSession", back_populates="user", cascade="all, delete-orphan")
    transactions: Mapped[List["LedgerTransaction"]] = relationship("LedgerTransaction", back_populates="user", cascade="all, delete-orphan")
    # [P2] Groups the user has joined (via access codes)
    group_memberships: Mapped[List["GroupMembership"]] = relationship("GroupMembership", back_populates="user", cascade="all, delete-orphan")


class Gateway(Base):
    __tablename__ = "gateways"

    id: Mapped[str] = mapped_column(String(50), primary_key=True) # MAC address or hardware UUID
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    vpn_ip: Mapped[str] = mapped_column(String(45), unique=True, nullable=False)
    status: Mapped[GatewayStatus] = mapped_column(SQLEnum(GatewayStatus, name="gateway_status", values_callable=lambda x: [e.value for e in x]), default=GatewayStatus.OFFLINE, nullable=False)
    # Firmware version last reported in the gateway's `online` status payload
    # (e.g. "1.5.0-direct"). NULL until the gateway first connects and reports
    # it. Lets a CPO see which gateways need an OTA.
    firmware_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Liveness marker: written ONLY by the MQTT handlers (status connect/LWT +
    # throttled telemetry refresh) and read by the session-start liveness gate.
    # No onupdate hook — an unrelated row edit (e.g. a CPO rename) must not
    # make a dead gateway look freshly seen.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="gateways")
    plugs: Mapped[List["Plug"]] = relationship("Plug", back_populates="gateway", cascade="all, delete-orphan")


class Plug(Base):
    __tablename__ = "plugs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gateway_id: Mapped[str] = mapped_column(String(50), ForeignKey("gateways.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    local_ip: Mapped[str] = mapped_column(String(45), nullable=False) # VLAN 20 IP
    plug_model: Mapped[str] = mapped_column(String(50), default="tapo_p110", nullable=False)
    # Stable device identity reported by the AmpHive Agent's discovery
    # (brand-scoped, e.g. "kasa:AA:BB:CC:DD:EE:FF"). NULL for ESP-gateway or
    # manually-provisioned plugs. Used to auto-populate + reconcile agent-
    # discovered plugs idempotently (the agent then adopts the assigned id).
    unique_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[PlugStatus] = mapped_column(SQLEnum(PlugStatus, name="plug_status", values_callable=lambda x: [e.value for e in x]), default=PlugStatus.OFFLINE, nullable=False)
    current_power_w: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Geolocation for the map. NULL = unknown → callers fall back to the plug's
    # gateway coordinates (a plug is physically at its gateway's site).
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    # [P2] Link each plug to a charger group. NULL = ungrouped/legacy (visible to all users).
    group_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("charger_groups.id", ondelete="SET NULL"), nullable=True)

    # Relationships
    gateway: Mapped[Gateway] = relationship("Gateway", back_populates="plugs")
    sessions: Mapped[List["ChargingSession"]] = relationship("ChargingSession", back_populates="plug", cascade="all, delete-orphan")
    # [P2] The charger group this plug belongs to
    group: Mapped[Optional["ChargerGroup"]] = relationship("ChargerGroup", back_populates="plugs")


class ChargingSession(Base):
    __tablename__ = "charging_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    plug_id: Mapped[int] = mapped_column(Integer, ForeignKey("plugs.id", ondelete="CASCADE"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    energy_kwh: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    peak_power_w: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Staleness signal for the session reaper: stamped by MQTTManager's
    # _persist_telemetry on every reading attributed to this session. NULL
    # until the first reading arrives (the reaper falls back to started_at).
    last_telemetry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Money: NUMERIC(12,2) → Decimal (energy_kwh/peak_power_w stay Float — measurements).
    coins_spent: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    status: Mapped[SessionStatus] = mapped_column(SQLEnum(SessionStatus, name="session_status", values_callable=lambda x: [e.value for e in x]), default=SessionStatus.ACTIVE, nullable=False)

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="sessions")
    user: Mapped[User] = relationship("User", back_populates="sessions")
    plug: Mapped[Plug] = relationship("Plug", back_populates="sessions")
    ledger_transactions: Mapped[List["LedgerTransaction"]] = relationship("LedgerTransaction", back_populates="session")


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("charging_sessions.id", ondelete="SET NULL"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False) # money: positive (topup), negative (debit)
    transaction_type: Mapped[TransactionType] = mapped_column(SQLEnum(TransactionType, name="tx_type", values_callable=lambda x: [e.value for e in x]), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Razorpay payment id for topups (e.g. "pay_XXionia"). UNIQUE so a concurrent
    # /verify + webhook for the same payment can't both credit — the second
    # INSERT hits the constraint and is treated as already-credited. NULL for
    # non-topup rows (session debits), and Postgres allows many NULLs under a
    # UNIQUE constraint, so debits are unaffected.
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)  # money
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="transactions")
    session: Mapped[Optional[ChargingSession]] = relationship("ChargingSession", back_populates="ledger_transactions")


# --- [P2] Charger Group Models (Public vs Private Access-Code-Gated) ---

class ChargerGroup(Base):
    """
    Represents a named group of charger plugs managed by a CPO.
    - Public groups: visible and usable by any registered user.
    - Private groups: require an access_code to join. Only members can see
      and use the plugs in the group.
    """
    __tablename__ = "charger_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # is_public: true = open to all registered users, false = access code required
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # access_code: shareable code for private groups (e.g. "SUNRISE2024").
    # NULL for public groups. Must be unique across the platform.
    access_code: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="charger_groups")
    # All plugs assigned to this group
    plugs: Mapped[List[Plug]] = relationship("Plug", back_populates="group")
    # All user memberships (for private groups)
    memberships: Mapped[List["GroupMembership"]] = relationship("GroupMembership", back_populates="group", cascade="all, delete-orphan")


class GroupMembership(Base):
    """
    Many-to-many join table: tracks which users have joined which private groups.
    Created when a user submits a valid access code via POST /api/groups/join.
    Not needed for public groups — those are accessible to all users automatically.
    """
    __tablename__ = "group_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("charger_groups.id", ondelete="CASCADE"), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="group_memberships")
    group: Mapped[ChargerGroup] = relationship("ChargerGroup", back_populates="memberships")


# --- Time-Series Telemetry Persistence ---

class TelemetryReading(Base):
    """
    Append-only time-series of raw plug telemetry samples (~1 row/plug/~15s).

    Feeds CPO analytical charts (load graphs, historical energy audits) via
    date_trunc aggregation. Written via a buffered background batch-flush task
    (services/telemetry_persistence.py), NOT per-message, so the live SSE path
    (services/telemetry.py TelemetryStore) is never blocked by DB writes.

    Design notes:
    - BigInteger PK: append-only, high row count over the table's lifetime.
    - tenant_id is denormalized (plug -> gateway -> tenant) so CPO charts can
      filter without a join on a hot analytical query. FK + CASCADE matches the
      Gateway/ChargingSession convention.
    - session_id is nullable / SET NULL: telemetry can arrive with no active
      session (idle plug), and deleting a session must not erase audit history.
    - status is a plain String (not a PG enum): it is a raw firmware signal that
      may evolve; a PG enum would need a migration to extend.
    - No relationship() back-refs on Plug/ChargingSession/Tenant: this is a
      high-cardinality child that should never be exposed as a lazy-loadable
      collection.
    """
    __tablename__ = "telemetry_readings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    plug_id: Mapped[int] = mapped_column(Integer, ForeignKey("plugs.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("charging_sessions.id", ondelete="SET NULL"), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    power_w: Mapped[float] = mapped_column(Float, nullable=False)
    energy_kwh: Mapped[float] = mapped_column(Float, nullable=False)   # session-relative kWh, as reported by firmware
    voltage_v: Mapped[float] = mapped_column(Float, nullable=False)
    current_a: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # raw firmware signal

    # Indexes MUST be declared here: create_all() (db.py init_db) is the only
    # schema path; schema.sql is never executed. Each composite maps 1:1 to a
    # query shape: plug load graph, per-session series, tenant-scoped aggregation.
    __table_args__ = (
        Index("idx_telemetry_plug_recorded", "plug_id", "recorded_at"),
        Index("idx_telemetry_session_recorded", "session_id", "recorded_at"),
        Index("idx_telemetry_tenant_recorded", "tenant_id", "recorded_at"),
    )


# --- Gateway / Plug Events (alarms, faults, operational notices) ---

class GatewayEvent(Base):
    """
    Operational events raised by a gateway/plug: firmware safety alarms
    (THERMAL_CUTOFF, OVERCURRENT_CUTOFF, UNAUTHORIZED_ON), OTA lifecycle
    notices, and backend-detected conditions. Surfaced to the CPO portal as an
    alert feed so an operator can see, e.g., a plug that was switched on
    out-of-band (physical button / Tapo app) with no authorized session.

    Design notes mirror TelemetryReading:
    - event_type / severity are plain Strings, not PG enums: they are raw
      firmware/operational signals that evolve without a schema migration.
    - tenant_id is denormalized (gateway -> tenant) so the CPO feed filters
      without a join. plug_id is nullable (some events are gateway-wide).
    - acknowledged lets an operator clear an alert from the active feed
      without deleting the audit row.
    """
    __tablename__ = "gateway_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    gateway_id: Mapped[str] = mapped_column(String(50), ForeignKey("gateways.id", ondelete="CASCADE"), nullable=False)
    plug_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("plugs.id", ondelete="SET NULL"), nullable=True)
    # e.g. "UNAUTHORIZED_ON", "THERMAL_CUTOFF", "OVERCURRENT_CUTOFF", "OTA_FAILED"
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    # "critical" | "warning" | "info" — drives feed styling / prioritization.
    severity: Mapped[str] = mapped_column(String(16), default="warning", nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    __table_args__ = (
        Index("idx_gateway_events_tenant_created", "tenant_id", "created_at"),
        Index("idx_gateway_events_gateway_created", "gateway_id", "created_at"),
    )


# --- CPO Payout / Settlement Ledger ---
#
# Record-keeping only: there is NO bank/UPI/payment-gateway integration here.
# A CPO requests a payout of its unsettled driver-coin earnings (snapshotted
# into a REQUESTED row); the platform operator (admin) marks it PAID once the
# transfer has happened out-of-band (bank/UPI, outside this app). This is the
# other half of the money loop from the driver-side Razorpay top-up — see
# services/payouts.py for the earnings/watermark math and routers/cpo.py for
# the endpoints.

class PayoutStatus(str, enum.Enum):
    REQUESTED = "requested"
    PAID = "paid"
    CANCELLED = "cancelled"


class Payout(Base):
    """
    A settlement snapshot for one tenant over one window [period_start,
    period_end): the gross coins collected from that tenant's COMPLETED
    sessions ending in the window, the platform's cut, and the net owed to
    the CPO. Windows are computed from a rolling per-tenant watermark (see
    services.payouts.tenant_settlement_watermark) — MAX(period_end) over the
    tenant's non-CANCELLED payouts — so consecutive requests cover disjoint,
    contiguous ranges and a CANCELLED payout frees its window for a later
    request.

    Status lifecycle: REQUESTED -> PAID (admin marks paid, out-of-band
    transfer already happened) or REQUESTED -> CANCELLED (CPO or admin frees
    the window without paying). Both transitions are made under a row lock
    (SELECT ... FOR UPDATE on this row) with the current status re-checked,
    so a double mark_paid/cancel (or a race between the two) settles exactly
    once — mirrors the finalize_charging_session double-stop guard. Terminal
    states (PAID/CANCELLED) never transition further. The REQUESTED-payout
    uniqueness-per-tenant check and the watermark read that seeds a new
    request are themselves serialized by row-locking the tenant (see
    routers/cpo.py) so two concurrent requests can't double-settle the same
    window.

    Design notes mirror GatewayEvent/TelemetryReading: no relationship()
    back-refs on Tenant/User — this is an append-mostly financial log that
    should never be a lazy-loadable collection.
    """
    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    # Half-open settlement window [period_start, period_end) this payout
    # covers, so a boundary timestamp can never be double-counted across two
    # payouts (see services.payouts.sum_completed_session_coins).
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Money: NUMERIC(12,2) -> Decimal, via services.money.to_money (see the
    # money note in that module). platform_fee_coins + net_coins == gross_coins.
    gross_coins: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    platform_fee_coins: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    net_coins: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[PayoutStatus] = mapped_column(SQLEnum(PayoutStatus, name="payout_status", values_callable=lambda x: [e.value for e in x]), default=PayoutStatus.REQUESTED, nullable=False)
    requested_by_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Composite covers both the "does tenant X have a REQUESTED payout"
    # existence check and plain tenant-scoped listing (tenant_id is the
    # leading column, so it also serves tenant_id-only lookups).
    __table_args__ = (
        Index("idx_payouts_tenant_status", "tenant_id", "status"),
    )
