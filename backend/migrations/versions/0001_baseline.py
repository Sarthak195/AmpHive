"""Baseline: full schema as of 2026-07-07 (Alembic adoption).

Frozen PostgreSQL DDL compiled from backend/database/models.py at adoption
time — it captures everything create_all() + the retired _INPLACE_UPGRADES
produced: NUMERIC(12,2) money columns, plug/gateway geolocation,
charging_sessions.last_telemetry_at, the telemetry_readings indexes, and the
razorpay_payment_id uniqueness. DO NOT edit or regenerate this file as models
evolve; every later change gets its own revision.

Pre-existing databases (created by the old create_all path) are STAMPED to
this revision by init_db() instead of executing it — see
backend/database/db.py. One cosmetic divergence on such databases: their
razorpay uniqueness lives in an explicitly named unique index
(uq_ledger_razorpay_payment_id, from the old in-place upgrade) rather than
this baseline's inline UNIQUE constraint. Functionally identical.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-07
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


CREATE_STATEMENTS = [
    """CREATE TYPE gateway_status AS ENUM ('online', 'offline')""",
    """CREATE TYPE plug_status AS ENUM ('available', 'occupied', 'offline', 'maintenance')""",
    """CREATE TYPE session_status AS ENUM ('active', 'completed', 'paid', 'cancelled')""",
    """CREATE TYPE tx_type AS ENUM ('topup', 'session_debit', 'refund')""",
    """CREATE TYPE user_role AS ENUM ('admin', 'cpo', 'driver')""",
    """CREATE TABLE tenants (
    id SERIAL NOT NULL,
    name VARCHAR(100) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (name)
)""",
    """CREATE TABLE charger_groups (
    id SERIAL NOT NULL,
    tenant_id INTEGER NOT NULL,
    name VARCHAR(100) NOT NULL,
    is_public BOOLEAN NOT NULL,
    access_code VARCHAR(20),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE,
    UNIQUE (access_code)
)""",
    """CREATE TABLE gateways (
    id VARCHAR(50) NOT NULL,
    tenant_id INTEGER NOT NULL,
    name VARCHAR(100) NOT NULL,
    vpn_ip VARCHAR(45) NOT NULL,
    status gateway_status NOT NULL,
    latitude FLOAT,
    longitude FLOAT,
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE,
    UNIQUE (vpn_ip)
)""",
    """CREATE TABLE users (
    id SERIAL NOT NULL,
    tenant_id INTEGER,
    email VARCHAR(255) NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    role user_role NOT NULL,
    coin_balance NUMERIC(12, 2) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE SET NULL,
    UNIQUE (email)
)""",
    """CREATE TABLE group_memberships (
    id SERIAL NOT NULL,
    user_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    joined_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(group_id) REFERENCES charger_groups (id) ON DELETE CASCADE
)""",
    """CREATE TABLE plugs (
    id SERIAL NOT NULL,
    gateway_id VARCHAR(50) NOT NULL,
    name VARCHAR(100) NOT NULL,
    local_ip VARCHAR(45) NOT NULL,
    plug_model VARCHAR(50) NOT NULL,
    status plug_status NOT NULL,
    current_power_w FLOAT NOT NULL,
    latitude FLOAT,
    longitude FLOAT,
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    group_id INTEGER,
    PRIMARY KEY (id),
    FOREIGN KEY(gateway_id) REFERENCES gateways (id) ON DELETE CASCADE,
    FOREIGN KEY(group_id) REFERENCES charger_groups (id) ON DELETE SET NULL
)""",
    """CREATE TABLE charging_sessions (
    id SERIAL NOT NULL,
    tenant_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    plug_id INTEGER NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    ended_at TIMESTAMP WITH TIME ZONE,
    energy_kwh FLOAT NOT NULL,
    peak_power_w FLOAT NOT NULL,
    last_telemetry_at TIMESTAMP WITH TIME ZONE,
    coins_spent NUMERIC(12, 2) NOT NULL,
    status session_status NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(plug_id) REFERENCES plugs (id) ON DELETE CASCADE
)""",
    """CREATE TABLE ledger_transactions (
    id SERIAL NOT NULL,
    user_id INTEGER NOT NULL,
    session_id INTEGER,
    amount NUMERIC(12, 2) NOT NULL,
    transaction_type tx_type NOT NULL,
    description VARCHAR(255),
    razorpay_payment_id VARCHAR(64),
    balance_after NUMERIC(12, 2) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES charging_sessions (id) ON DELETE SET NULL,
    UNIQUE (razorpay_payment_id)
)""",
    """CREATE TABLE telemetry_readings (
    id BIGSERIAL NOT NULL,
    tenant_id INTEGER NOT NULL,
    plug_id INTEGER NOT NULL,
    session_id INTEGER,
    recorded_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    power_w FLOAT NOT NULL,
    energy_kwh FLOAT NOT NULL,
    voltage_v FLOAT NOT NULL,
    current_a FLOAT NOT NULL,
    status VARCHAR(20),
    PRIMARY KEY (id),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE,
    FOREIGN KEY(plug_id) REFERENCES plugs (id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES charging_sessions (id) ON DELETE SET NULL
)""",
    """CREATE INDEX idx_telemetry_plug_recorded ON telemetry_readings (plug_id, recorded_at)""",
    """CREATE INDEX idx_telemetry_session_recorded ON telemetry_readings (session_id, recorded_at)""",
    """CREATE INDEX idx_telemetry_tenant_recorded ON telemetry_readings (tenant_id, recorded_at)""",
]

DROP_STATEMENTS = [
    "DROP TABLE IF EXISTS telemetry_readings CASCADE",
    "DROP TABLE IF EXISTS ledger_transactions CASCADE",
    "DROP TABLE IF EXISTS charging_sessions CASCADE",
    "DROP TABLE IF EXISTS plugs CASCADE",
    "DROP TABLE IF EXISTS group_memberships CASCADE",
    "DROP TABLE IF EXISTS users CASCADE",
    "DROP TABLE IF EXISTS gateways CASCADE",
    "DROP TABLE IF EXISTS charger_groups CASCADE",
    "DROP TABLE IF EXISTS tenants CASCADE",
    "DROP TYPE IF EXISTS gateway_status",
    "DROP TYPE IF EXISTS plug_status",
    "DROP TYPE IF EXISTS session_status",
    "DROP TYPE IF EXISTS tx_type",
    "DROP TYPE IF EXISTS user_role",
]


def upgrade() -> None:
    for stmt in CREATE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DROP_STATEMENTS:
        op.execute(stmt)
