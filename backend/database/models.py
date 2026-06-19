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
from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey, DateTime, Enum as SQLEnum, text
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    role: Mapped[UserRole] = mapped_column(SQLEnum(UserRole), default=UserRole.DRIVER, nullable=False)
    coin_balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
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
    status: Mapped[GatewayStatus] = mapped_column(SQLEnum(GatewayStatus), default=GatewayStatus.OFFLINE, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), onupdate=datetime.now, nullable=False)
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
    status: Mapped[PlugStatus] = mapped_column(SQLEnum(PlugStatus), default=PlugStatus.OFFLINE, nullable=False)
    current_power_w: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
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
    coins_spent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[SessionStatus] = mapped_column(SQLEnum(SessionStatus), default=SessionStatus.ACTIVE, nullable=False)

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
    amount: Mapped[float] = mapped_column(Float, nullable=False) # positive (topup), negative (debit)
    transaction_type: Mapped[TransactionType] = mapped_column(SQLEnum(TransactionType), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    balance_after: Mapped[float] = mapped_column(Float, nullable=False)
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
