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
    # [Tariffs] The tenant-wide fallback tariff, used by services/pricing.py's
    # resolve_rate_for_plug() when a plug has no tariff of its own and (if
    # grouped) its charger group has none either. NULL = no tenant default
    # configured yet -> resolution falls through to the global COINS_PER_KWH
    # env var. ON DELETE SET NULL: deleting the referenced Tariff must not
    # break the tenant row, just drop back a link in the resolution chain.
    # use_alter breaks the tariffs<->tenants FK cycle for DDL sorting (tariffs
    # already references tenants via tariffs.tenant_id): the back-reference is
    # added via ALTER after both tables exist, otherwise create_all/drop_all
    # (the DB-gated test fixtures) raise CircularDependencyError. Named to match
    # the auto-generated name from the migration's inline REFERENCES clause.
    default_tariff_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("tariffs.id", ondelete="SET NULL", use_alter=True,
                   name="tenants_default_tariff_id_fkey"),
        nullable=True,
    )
    # [GST Invoices] Optional GST registration identity for this tenant
    # (CPO) — rendered as the invoice "seller" block and SNAPSHOTTED onto
    # every Invoice at issue time (services/invoices.py
    # issue_invoice_for_session), so a later edit here never rewrites an
    # already-issued invoice. NULL/blank is legal: a CPO not yet
    # GST-registered can still issue an invoice (the GSTIN line just prints
    # blank) — deliberately not NOT NULL, since retrofitting GST config onto
    # existing tenants is an operational rollout step, not a schema
    # requirement.
    gstin: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    legal_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # Prefix for this tenant's invoice numbers (e.g. "ACME"); NULL falls
    # back to a tenant-scoped default ("INV<tenant_id>") at issue time, not
    # a bare "INV" — invoice_number is globally unique, so two different
    # unconfigured tenants must not collide on the same fallback (see
    # services/invoices.py issue_invoice_for_session).
    invoice_prefix: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    # The next sequential invoice number to allocate for this tenant.
    # Incremented under a `SELECT ... FOR UPDATE` on this row at issue time
    # so two concurrent issues for this tenant never mint the same
    # invoice_number — read/updated as plain columns (not a mapped-entity
    # mutation) the same way services/wallet.py's debit_wallet_clamped
    # bypasses the identity map; see that module's docstring for why.
    next_invoice_seq: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)

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
    # [Tariffs] Per-plug pricing override. NULL = fall back to the plug's
    # group tariff, then the tenant default, then the global COINS_PER_KWH env
    # var — see services/pricing.py resolve_rate_for_plug(). ON DELETE SET
    # NULL so deleting a Tariff just drops this plug back a link in the chain.
    tariff_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("tariffs.id", ondelete="SET NULL"), nullable=True)

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
    # [Tariffs] The coins-per-kWh rate resolved (services/pricing.py
    # resolve_rate_for_plug) and SNAPSHOTTED at session start. Every billing
    # path (finalize_charging_session, the mqtt_manager balance-exhaustion
    # auto-stop, the live TelemetryStore cost calc) must read this instead of
    # re-resolving, so a tariff edit or reassignment mid-session never
    # retroactively changes what an in-flight or already-billed session is
    # charged. NULL only for legacy sessions started before this column
    # existed — those fall back to the global COINS_PER_KWH env var.
    rate_coins_per_kwh: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
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
    # [Tariffs] Group-level pricing override, used for any plug in this group
    # that has no tariff of its own. NULL = fall back to the tenant default,
    # then the global COINS_PER_KWH env var — see services/pricing.py
    # resolve_rate_for_plug(). ON DELETE SET NULL, same rationale as Plug.tariff_id.
    tariff_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("tariffs.id", ondelete="SET NULL"), nullable=True)
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


# --- Driver Notifications ---

class Notification(Base):
    """
    Per-user notification feed (drivers first; the CPO alarm feed stays on
    gateway_events). Written by services/notifications.py at the session
    lifecycle / wallet / safety emit points, delivered live over Socket.io
    (user room) + Web Push, and listed by GET /api/notifications.

    Design notes mirror GatewayEvent:
    - type/severity are plain Strings, not PG enums — the set evolves without
      a schema migration ("session_stopped", "low_balance", "charger_offline",
      "safety_cutoff", "topup_credited", ...).
    - plug_id/session_id are nullable SET NULL context refs: deleting a plug
      or session must not erase a user's notification history.
    - read (not "acknowledged") — driver-facing wording; flipping it hides the
      row from the unread badge without deleting the audit trail.
    """
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(String(500), nullable=False)
    plug_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("plugs.id", ondelete="SET NULL"), nullable=True)
    session_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("charging_sessions.id", ondelete="SET NULL"), nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    __table_args__ = (
        # Feed query: newest N for a user; unread-count query filters read=false.
        Index("idx_notifications_user_created", "user_id", "created_at"),
    )


class PushSubscription(Base):
    """
    A browser Web-Push subscription (one row per browser/device the user
    enabled push on). endpoint is the push service URL and is globally unique
    by construction; p256dh/auth are the client keys pywebpush encrypts
    against. Rows are pruned when the push service reports the subscription
    gone (404/410) or the user disables push.
    """
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)


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


# --- Tariffs (per-CPO/per-site pricing) ---

class Tariff(Base):
    """
    A named coins-per-kWh pricing plan a tenant (CPO) defines and assigns to
    a plug, a charger group, or as the tenant's default — replacing the old
    single global COINS_PER_KWH env var with per-CPO/per-site rates.

    Resolution (services/pricing.py resolve_rate_for_plug), first match wins:
        plug.tariff_id -> plug.group.tariff_id -> tenant.default_tariff_id
        -> the global COINS_PER_KWH env var (legacy fallback)

    The resolved rate is SNAPSHOTTED onto ChargingSession.rate_coins_per_kwh
    at session start and never re-resolved for that session: editing a
    Tariff's price_per_kwh, or reassigning a plug/group/tenant to a different
    tariff, must not retroactively change the bill for a session already in
    flight or already completed. All billing paths (finalize_charging_session,
    the mqtt_manager balance-exhaustion auto-stop, the live TelemetryStore
    cost calc) read the session's snapshot, not this table, directly.

    No relationship() back-refs to Plug/ChargerGroup/Tenant: those tables also
    hold a Tenant<->Tariff-style FK pair in the opposite direction
    (Tenant.default_tariff_id -> Tariff.id vs. Tariff.tenant_id ->
    Tenant.id), which SQLAlchemy can't auto-disambiguate into a relationship()
    without explicit foreign_keys= wiring on both sides. Every call site
    resolves via explicit `select(Tariff)...` queries instead (matching how
    the rest of this codebase already reads Plug.group_id / ChargerGroup
    without relying on lazy relationship attributes across the async
    boundary).
    """
    __tablename__ = "tariffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Money: NUMERIC(12,2) → Decimal, same convention as the wallet columns
    # (services/money.to_money). Coins charged per kWh consumed under this plan.
    price_per_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), onupdate=text("CURRENT_TIMESTAMP"), nullable=False)


# --- GST Tax Invoices ---

class Invoice(Base):
    """
    A formal numbered GST tax invoice for one finished, billed
    ChargingSession — the legally-required document Indian commercial
    charging must issue that the session receipt payload
    (finalize_charging_session's return dict) has never itself been.

    India intra-state GST ONLY: a single combined rate (env `GST_RATE_PCT`,
    default 18.0 — services/invoices.py gst_rate_pct()) is charged
    INCLUSIVE in the coins the driver already paid (1 coin = ₹1); the
    CGST/SGST (intra-state) vs. IGST (inter-state) split is explicitly OUT
    OF SCOPE — this app doesn't geo-verify the driver and CPO are in the
    same state, so it can't determine which applies. The combined-rate
    total/tax figures are still legally correct, just not itemized into
    their two intra-state halves on the printed line.

    One invoice per session (UNIQUE session_id) — services/invoices.py
    issue_invoice_for_session() is idempotent: a repeat call for an
    already-invoiced session returns the existing row rather than minting a
    second one, enforced by this UNIQUE constraint (+ an IntegrityError
    catch for the concurrent-first-issue race), not just a pre-check.
    invoice_number is allocated sequentially per tenant under a
    `SELECT ... FOR UPDATE` on Tenant.next_invoice_seq, so two concurrent
    first-issues for the same tenant never mint the same number.

    Immutable snapshot: seller identity (seller_legal_name/seller_gstin,
    copied from Tenant.legal_name/gstin) and the billed line
    (energy_kwh/rate_coins_per_kwh, copied from the session) are frozen onto
    the invoice at issue time so a later Tenant profile edit — or, in
    principle, any change to the underlying session — never rewrites an
    already-issued invoice. Same immutability rationale as
    ChargingSession.rate_coins_per_kwh.

    FKs all CASCADE (tenant_id/session_id/driver_user_id), mirroring
    ChargingSession's own FK choices: an Invoice is a child record of a
    session which itself already cascades away with its tenant/user/plug,
    and session_id's NOT NULL + UNIQUE pairing (every invoice has exactly
    one owning session) rules out ON DELETE SET NULL.
    """
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("charging_sessions.id", ondelete="CASCADE"), unique=True, nullable=False)
    driver_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # e.g. "ACME-2026-27-00001": {tenant.invoice_prefix, or a tenant-scoped
    # fallback if unset}-{financial year}-{seq:05d}. UNIQUE is GLOBAL (one
    # numbering namespace across all tenants) even though `seq` counts per
    # tenant, so services/invoices.py's fallback prefix folds in the tenant
    # id rather than a bare constant — see that module's
    # issue_invoice_for_session for why a shared constant fallback would let
    # two different unconfigured tenants collide on the same number.
    invoice_number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    # Money: NUMERIC(12,2) → Decimal, via services.money.to_money (see the
    # money note in that module). amount_coins == total_inr (1 coin = ₹1);
    # kept as two columns because "coins debited from the wallet" and "the
    # invoice's face total in rupees" are distinct legal/accounting concepts
    # even though numerically identical under the current 1:1 peg.
    amount_coins: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    taxable_value_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gst_rate_pct: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gst_amount_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # Seller snapshot — immune to a later Tenant.legal_name/gstin edit.
    seller_legal_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    seller_gstin: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)

    # Billed-line snapshot — energy_kwh/rate_coins_per_kwh are already
    # immutable on the session itself post-completion, but copied here too
    # so rendering an invoice never needs a join back to charging_sessions.
    energy_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    rate_coins_per_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    # Leading tenant_id also serves plain tenant-scoped lookups (GET
    # /api/cpo/invoices) — same "one composite covers both" reasoning as
    # idx_payouts_tenant_status / idx_notifications_user_created.
    __table_args__ = (
        Index("idx_invoices_tenant_issued", "tenant_id", "issued_at"),
    )
