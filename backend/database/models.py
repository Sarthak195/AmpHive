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


# --- CPO Admin Audit Trail ---

class AuditLog(Base):
    """
    Record of CPO admin actions in this multi-tenant billing system: gateway/
    plug/group create-delete, status changes, and access-code regeneration
    (TD#26 — there was previously no accountability trail for admin actions).
    Written by services/audit.py (record_audit / try_record_audit), called
    from routers/cpo.py after each mutating admin action's own commit has
    already landed. Read via GET /api/cpo/audit.

    Design notes mirror GatewayEvent:
    - action / target_type are plain Strings, not PG enums: the action
      taxonomy (e.g. "gateway.create", "plug.status_change",
      "access_code.regen") is expected to grow without a schema migration.
    - tenant_id is NOT NULL + CASCADE, matching Gateway/ChargingSession/
      ChargerGroup/GatewayEvent — deleting a tenant deletes its audit trail
      along with everything else it owns.
    - actor_user_id is nullable + SET NULL: the acting user's account must
      stay deletable without erasing the audit trail (same rationale as
      LedgerTransaction.session_id's nullable-on-delete).
    - target_id is a String even though gateway ids are natively strings and
      plug/group ids are ints — one column that fits either FK'd resource
      without a polymorphic-FK setup.
    - detail is free-form Text, not a fixed-shape column: different actions
      carry different context (e.g. an old -> new status transition).
    - No relationship() back-refs on Tenant/User: like TelemetryReading and
      GatewayEvent, this is a high-cardinality append-only log that should
      never be a lazy-loadable collection.
    """
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    actor_user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # e.g. "gateway.create", "gateway.delete", "plug.create", "plug.delete",
    # "plug.status_change", "group.create", "group.delete", "access_code.regen"
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    # e.g. "gateway", "plug", "group"
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    __table_args__ = (
        Index("idx_audit_logs_tenant_created", "tenant_id", "created_at"),
    )


# --- Session Disputes / Refunds (coins-only remedy) ---

class DisputeStatus(str, enum.Enum):
    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"


class SessionDispute(Base):
    """
    Driver-initiated dispute against a finished ChargingSession, resolved by
    the CPO that owns the session's plug (backend/routers/sessions.py POST
    /api/sessions/{id}/dispute to file; backend/routers/cpo.py GET
    /api/cpo/disputes + POST /api/cpo/disputes/{id}/resolve to list/resolve).

    Coins-only remedy: there is no Razorpay money-out path. An APPROVED
    dispute credits the driver's coin wallet via services/wallet.credit_wallet
    and writes a REFUND LedgerTransaction referencing the session — the same
    wallet the driver spent from, never a card/UPI reversal (see
    MARKET_GAP_ANALYSIS.md §3 "Refunds").

    At most one OPEN dispute may exist per session — enforced below by a
    partial unique index (DB-level backstop, not just app-level
    check-then-insert), so a double-submit race can't create two. A session
    can still accumulate several *resolved* disputes over time (e.g. re-filed
    after a REJECTED one; each APPROVED one carries its own refund_coins) —
    the resolve endpoint enforces that the sum of a session's APPROVED
    refund_coins never exceeds that session's coins_spent, row-locking the
    session so two concurrent approvals on the same session serialize.

    tenant_id is denormalized from the session's plug -> gateway -> tenant
    chain (mirrors TelemetryReading/GatewayEvent/AuditLog) so the CPO-scoped
    list/resolve endpoints filter with a single indexed equality, no join.
    """
    __tablename__ = "session_disputes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("charging_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    driver_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[DisputeStatus] = mapped_column(SQLEnum(DisputeStatus, name="dispute_status", values_callable=lambda x: [e.value for e in x]), default=DisputeStatus.OPEN, nullable=False)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Money: NUMERIC(12,2) -> Decimal, same convention as every other coin
    # amount (services/money.to_money). NULL until resolved, and stays NULL
    # on REJECTED — only an APPROVED dispute ever carries a refund amount.
    refund_coins: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        # DB-level backstop for "at most one OPEN dispute per session": a
        # partial unique index over rows where status = 'open'. Resolved rows
        # fall outside the predicate, so this never blocks a session from
        # accumulating multiple resolved disputes over time — only a second
        # simultaneously-open one collides. The router catches the resulting
        # IntegrityError on a double-submit race and returns 409 instead of
        # a raw 500 (same pattern as _credit_topup / cpo_setup).
        Index(
            "ix_session_disputes_one_open_per_session",
            "session_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )
