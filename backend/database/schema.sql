-- AmpHive Database Schema for PostgreSQL

-- Create Custom Enum Types for Statuses and Roles
CREATE TYPE user_role AS ENUM ('admin', 'cpo', 'driver');
CREATE TYPE gateway_status AS ENUM ('online', 'offline');
CREATE TYPE plug_status AS ENUM ('available', 'occupied', 'offline', 'maintenance');
CREATE TYPE session_status AS ENUM ('active', 'completed', 'paid', 'cancelled');
CREATE TYPE tx_type AS ENUM ('topup', 'session_debit', 'refund');

-- 1. Tenants Table (Charging Point Operators/Organizations)
CREATE TABLE tenants (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- 2. Users Table
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    role user_role DEFAULT 'driver'::user_role NOT NULL,
    coin_balance DOUBLE PRECISION DEFAULT 0.0 NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- 3. Gateways Table (ESP32 Gateway hardware node)
CREATE TABLE gateways (
    id VARCHAR(50) PRIMARY KEY, -- Usually MAC address or unique hardware uuid
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    vpn_ip VARCHAR(45) UNIQUE NOT NULL, -- Tailscale/WireGuard allocated IP
    status gateway_status DEFAULT 'offline'::gateway_status NOT NULL,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- 4. Plugs Table (Smart Plugs connected to the Gateway locally)
CREATE TABLE plugs (
    id SERIAL PRIMARY KEY,
    gateway_id VARCHAR(50) NOT NULL REFERENCES gateways(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    local_ip VARCHAR(45) NOT NULL, -- IP on local VLAN 20
    plug_model VARCHAR(50) DEFAULT 'tapo_p110' NOT NULL, -- e.g., 'tapo_p110', 'shelly_plug_us'
    status plug_status DEFAULT 'offline'::plug_status NOT NULL,
    current_power_w DOUBLE PRECISION DEFAULT 0.0 NOT NULL,
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT unique_gateway_plug_ip UNIQUE (gateway_id, local_ip)
);

-- 5. Charging Sessions Table
CREATE TABLE charging_sessions (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plug_id INTEGER NOT NULL REFERENCES plugs(id) ON DELETE CASCADE,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    ended_at TIMESTAMP WITH TIME ZONE,
    energy_kwh DOUBLE PRECISION DEFAULT 0.0 NOT NULL,
    peak_power_w DOUBLE PRECISION DEFAULT 0.0 NOT NULL,
    coins_spent DOUBLE PRECISION DEFAULT 0.0 NOT NULL,
    status session_status DEFAULT 'active'::session_status NOT NULL
);

-- 6. Ledger Transactions Table
CREATE TABLE ledger_transactions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES charging_sessions(id) ON DELETE SET NULL,
    amount DOUBLE PRECISION NOT NULL, -- positive for credits, negative for debits
    transaction_type tx_type NOT NULL,
    description VARCHAR(255),
    balance_after DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- 7. Telemetry Readings Table (append-only time-series of raw plug samples)
-- Written via a buffered background batch-flush from the MQTT handler
-- (backend/services/telemetry_persistence.py). tenant_id is denormalized so CPO
-- charts filter without a plug->gateway->tenant join; session_id is nullable
-- (SET NULL) since telemetry can arrive with no active session.
CREATE TABLE telemetry_readings (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plug_id INTEGER NOT NULL REFERENCES plugs(id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES charging_sessions(id) ON DELETE SET NULL,
    recorded_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    power_w DOUBLE PRECISION NOT NULL,
    energy_kwh DOUBLE PRECISION NOT NULL, -- cumulative, as reported by firmware
    voltage_v DOUBLE PRECISION NOT NULL,
    current_a DOUBLE PRECISION NOT NULL,
    status VARCHAR(20) -- raw firmware signal (nullable)
);

-- Indexes for performance
CREATE INDEX idx_users_tenant ON users(tenant_id);
CREATE INDEX idx_gateways_tenant ON gateways(tenant_id);
CREATE INDEX idx_plugs_gateway ON plugs(gateway_id);
CREATE INDEX idx_sessions_user ON charging_sessions(user_id);
CREATE INDEX idx_sessions_status ON charging_sessions(status);
CREATE INDEX idx_ledger_user ON ledger_transactions(user_id);
CREATE INDEX idx_telemetry_plug_recorded ON telemetry_readings(plug_id, recorded_at);
CREATE INDEX idx_telemetry_session_recorded ON telemetry_readings(session_id, recorded_at);
CREATE INDEX idx_telemetry_tenant_recorded ON telemetry_readings(tenant_id, recorded_at);
