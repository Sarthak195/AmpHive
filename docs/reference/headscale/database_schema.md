# Headscale Database Schema Documentation

Headscale supports SQLite and PostgreSQL backends managed via the GORM ORM. The SQLite schema (`hscontrol/db/schema.sql`) represents the system's database structure and serves as the validation source of truth.

---

## 1. Core Tables

### 1.1. `users` Table
Stores local and federated (OIDC) user names and credentials.

| Field Name | Type | Key | Description |
| :--- | :--- | :---: | :--- |
| `id` | integer | PK | Auto-incrementing primary key. |
| `name` | text | | Username or namespace identifier. |
| `display_name` | text | | Optional human-readable display name. |
| `email` | text | | User's email address. |
| `provider_identifier`| text | | External provider identifier (OIDC). |
| `provider` | text | | External provider name (OIDC). |
| `profile_pic_url` | text | | URL path to user profile picture. |
| `created_at` | datetime | | Record creation timestamp. |
| `updated_at` | datetime | | Record update timestamp. |
| `deleted_at` | datetime | | Soft deletion timestamp (soft-delete). |

### 1.2. `pre_auth_keys` Table
Used to pre-approve node registrations without interactive logins.

| Field Name | Type | Key | Description |
| :--- | :--- | :---: | :--- |
| `id` | integer | PK | Auto-incrementing primary key. |
| `key` | text | | Plain text key format. |
| `prefix` | text | UK | Key prefix. |
| `hash` | blob | | Cryptographic hash of the key. |
| `user_id` | integer | FK | User owner of the key (`users.id`). |
| `reusable` | numeric | | Flag checking if the key can register multiple nodes. |
| `ephemeral` | numeric | | True if node should deregister when going offline. |
| `used` | numeric | | True if a single-use key has been redeemed. |
| `tags` | text | | Pre-assigned node ACL tags. |
| `expiration` | datetime | | Key expiration date. |
| `created_at` | datetime | | Key generation timestamp. |

### 1.3. `api_keys` Table
Allows administrative authentication when CLI or HTTP clients query the gRPC APIs.

| Field Name | Type | Key | Description |
| :--- | :--- | :---: | :--- |
| `id` | integer | PK | Auto-incrementing primary key. |
| `prefix` | text | UK | API key prefix for lookup. |
| `hash` | blob | | Cryptographic hash of the API key. |
| `expiration` | datetime | | Key expiration date. |
| `last_seen` | datetime | | Timestamp of last API usage. |
| `created_at` | datetime | | Key generation timestamp. |

### 1.4. `nodes` Table
Stores node connection parameters, IP allocations, WireGuard public keys, and routing approvals.

| Field Name | Type | Key | Description |
| :--- | :--- | :---: | :--- |
| `id` | integer | PK | Auto-incrementing primary key. |
| `machine_key` | text | | Noise handshake machine key. |
| `node_key` | text | | WireGuard static node key. |
| `disco_key` | text | | Tailscale discovery protocol public key. |
| `endpoints` | text | | JSON/Text array of public IP endpoints. |
| `host_info` | text | | JSON metadata representing host OS details. |
| `ipv4` | text | | Allocated IPv4 address within the Tailnet range. |
| `ipv6` | text | | Allocated IPv6 address within the Tailnet range. |
| `hostname` | text | | Local device hostname. |
| `given_name` | varchar | | Admin-assigned node alias (max 63 chars). |
| `user_id` | integer | FK | Owner user namespace (`users.id`). NULL for tagged nodes. |
| `register_method` | text | | Method of registration (e.g. `preauthkey`, `cli`). |
| `tags` | text | | Assigned ACL tags. |
| `auth_key_id` | integer | FK | PreAuthKey ID used for registration (`pre_auth_keys.id`). |
| `last_seen` | datetime | | Timestamp of last polling connection update. |
| `expiry` | datetime | | Node expiration timestamp. |
| `approved_routes` | text | | Enabled subnet routes. |
| `created_at` | datetime | | Record creation timestamp. |
| `updated_at` | datetime | | Record update timestamp. |
| `deleted_at` | datetime | | Soft deletion timestamp. |

### 1.5. `policies` Table
Stores access control lists (ACLs) and network boundaries.

| Field Name | Type | Key | Description |
| :--- | :--- | :---: | :--- |
| `id` | integer | PK | Auto-incrementing primary key. |
| `data` | text | | Raw ACL data (YAML/JSON). |
| `created_at` | datetime | | Record creation timestamp. |
| `updated_at` | datetime | | Record update timestamp. |
| `deleted_at` | datetime | | Soft deletion timestamp. |

---

## 2. Constraints & Index Specifications

### Uniqueness Constraints (`users` table)
Three unique indexes enforce user identities and OIDC mapping:
1. `idx_provider_identifier`: Enforces uniqueness on `provider_identifier` across the system (non-null entries).
2. `idx_name_provider_identifier`: Prevents duplicate names under the same identity provider.
3. `idx_name_no_provider_identifier`: Enforces unique local names for users without OIDC configuration.

### Soft Delete Indexes
GORM soft deletion triggers require indexing on the `deleted_at` columns to maintain query performance:
- `idx_users_deleted_at`
- `idx_policies_deleted_at`

---

## 3. Relationships & Cascades

- **Node -> User**: Many-to-One (`nodes.user_id` -> `users.id`). Cascades on deletion: if a User is deleted, all owned nodes are removed.
- **PreAuthKey -> User**: Many-to-One (`pre_auth_keys.user_id` -> `users.id`). Set Null on deletion: if a User is deleted, the keys remain but their ownership is set to NULL.
- **Node -> PreAuthKey**: Many-to-One (`nodes.auth_key_id` -> `pre_auth_keys.id`).
