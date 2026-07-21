"""
Seed script for AmpHive
=======================
Populates the database with initial development and test data, including
sample Tenants, CPOs, Drivers, Gateways, Plugs, Charger Groups, Charging Sessions,
and Ledger Transactions. This data provides immediate visibility into all dashboard
charts, maps, and lists.

Usage (run inside the backend directory or container):
  python seed.py

Or via Docker Compose:
  docker exec -it amphive-backend-dev python seed.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

# Add parent directory to sys.path so backend imports work
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from backend.database.db import async_session_factory, init_db
from backend.database.models import (
    ChargerGroup,
    ChargingSession,
    Gateway,
    GatewayStatus,
    GroupMembership,
    LedgerTransaction,
    Plug,
    PlugStatus,
    SessionStatus,
    Tenant,
    TransactionType,
    User,
    UserRole,
)
from backend.services.auth import hash_password


async def seed():
    print("[*] Initializing database tables...")
    await init_db()

    async with async_session_factory() as db:
        print("[*] Seeding sample data...")

        # --- 1. Tenants ---
        tenants = {}
        for tenant_name in ["VoltNetwork", "GreenCharge"]:
            existing = await db.execute(select(Tenant).where(Tenant.name == tenant_name))
            tenant = existing.scalar_one_or_none()
            if not tenant:
                tenant = Tenant(name=tenant_name)
                db.add(tenant)
                await db.flush()  # get tenant.id
                print(f"✓ Created Tenant: {tenant_name}")
            else:
                print(f"  Tenant {tenant_name} already exists")
            tenants[tenant_name] = tenant

        # --- 2. Users ---
        users_to_create = [
            # Admins
            {
                "email": "admin@amphive.com",
                "full_name": "System Administrator",
                "role": UserRole.ADMIN,
                "tenant_id": None,
                "coin_balance": 1000.0,
            },
            # CPOs
            {
                "email": "cpo@voltnetwork.com",
                "full_name": "VoltNetwork Operator",
                "role": UserRole.CPO,
                "tenant_id": tenants["VoltNetwork"].id,
                "coin_balance": 0.0,
            },
            {
                "email": "cpo@greencharge.com",
                "full_name": "GreenCharge Operator",
                "role": UserRole.CPO,
                "tenant_id": tenants["GreenCharge"].id,
                "coin_balance": 0.0,
            },
            # Drivers
            {
                "email": "driver1@gmail.com",
                "full_name": "Amit Sharma",
                "role": UserRole.DRIVER,
                "tenant_id": None,
                "coin_balance": 250.0,
            },
            {
                "email": "driver2@gmail.com",
                "full_name": "Priya Patel",
                "role": UserRole.DRIVER,
                "tenant_id": None,
                "coin_balance": 15.50,
            },
        ]

        users = {}
        for u_data in users_to_create:
            existing = await db.execute(select(User).where(User.email == u_data["email"]))
            user = existing.scalar_one_or_none()
            if not user:
                user = User(
                    email=u_data["email"],
                    hashed_password=hash_password("password123"),
                    full_name=u_data["full_name"],
                    role=u_data["role"],
                    tenant_id=u_data["tenant_id"],
                    coin_balance=u_data["coin_balance"]
                )
                db.add(user)
                await db.flush()
                print(f"✓ Created User: {user.email} ({user.role.value})")
            else:
                print(f"  User {user.email} already exists")
            users[u_data["email"]] = user

        # --- 3. Charger Groups ---
        charger_groups = {}
        groups_to_create = [
            {
                "name": "VoltNetwork Public Station",
                "tenant_id": tenants["VoltNetwork"].id,
                "is_public": True,
                "access_code": None
            },
            {
                "name": "VoltNetwork Corporate (Private)",
                "tenant_id": tenants["VoltNetwork"].id,
                "is_public": False,
                "access_code": "VOLT123"
            },
            {
                "name": "GreenCharge City Hub",
                "tenant_id": tenants["GreenCharge"].id,
                "is_public": True,
                "access_code": None
            }
        ]

        for g_data in groups_to_create:
            existing = await db.execute(select(ChargerGroup).where(ChargerGroup.name == g_data["name"]))
            group = existing.scalar_one_or_none()
            if not group:
                group = ChargerGroup(
                    name=g_data["name"],
                    tenant_id=g_data["tenant_id"],
                    is_public=g_data["is_public"],
                    access_code=g_data["access_code"]
                )
                db.add(group)
                await db.flush()
                print(f"✓ Created Charger Group: {group.name}")
            else:
                print(f"  Charger Group {g_data['name']} already exists")
            charger_groups[g_data["name"]] = group

        # --- 4. Group Memberships (Priya joins private group) ---
        existing_membership = await db.execute(
            select(GroupMembership)
            .where(
                GroupMembership.user_id == users["driver2@gmail.com"].id,
                GroupMembership.group_id == charger_groups["VoltNetwork Corporate (Private)"].id
            )
        )
        if not existing_membership.scalar_one_or_none():
            membership = GroupMembership(
                user_id=users["driver2@gmail.com"].id,
                group_id=charger_groups["VoltNetwork Corporate (Private)"].id
            )
            db.add(membership)
            print("✓ Added Priya Patel to private group: VoltNetwork Corporate (Private)")
        else:
            print("  Priya Patel already a member of private group")

        # --- 5. Gateways ---
        gateways = {}
        gateways_to_create = [
            {
                "id": "00:1A:2B:3C:4D:5E",
                "name": "Volt-Gateway-01",
                "vpn_ip": "10.8.0.2",
                "latitude": 28.6139,
                "longitude": 77.2090,
                "tenant_id": tenants["VoltNetwork"].id
            },
            {
                "id": "00:1A:2B:3C:4D:5F",
                "name": "Green-Gateway-01",
                "vpn_ip": "10.8.0.3",
                "latitude": 19.0760,
                "longitude": 72.8777,
                "tenant_id": tenants["GreenCharge"].id
            }
        ]

        for gw_data in gateways_to_create:
            existing = await db.execute(select(Gateway).where(Gateway.id == gw_data["id"]))
            gw = existing.scalar_one_or_none()
            if not gw:
                gw = Gateway(
                    id=gw_data["id"],
                    name=gw_data["name"],
                    vpn_ip=gw_data["vpn_ip"],
                    latitude=gw_data["latitude"],
                    longitude=gw_data["longitude"],
                    status=GatewayStatus.ONLINE,
                    tenant_id=gw_data["tenant_id"]
                )
                db.add(gw)
                await db.flush()
                print(f"✓ Created Gateway: {gw.name} ({gw.id})")
            else:
                print(f"  Gateway {gw_data['id']} already exists")
            gateways[gw_data["name"]] = gw

        # --- 6. Plugs ---
        plugs = {}
        plugs_to_create = [
            {
                "name": "Volt-FastPlug-01",
                "local_ip": "192.168.20.10",
                "gateway_id": gateways["Volt-Gateway-01"].id,
                "group_id": charger_groups["VoltNetwork Public Station"].id,
                "status": PlugStatus.AVAILABLE
            },
            {
                "name": "Volt-CorpPlug-02",
                "local_ip": "192.168.20.11",
                "gateway_id": gateways["Volt-Gateway-01"].id,
                "group_id": charger_groups["VoltNetwork Corporate (Private)"].id,
                "status": PlugStatus.AVAILABLE
            },
            {
                "name": "Green-CityPlug-01",
                "local_ip": "192.168.20.20",
                "gateway_id": gateways["Green-Gateway-01"].id,
                "group_id": charger_groups["GreenCharge City Hub"].id,
                "status": PlugStatus.AVAILABLE
            }
        ]

        for p_data in plugs_to_create:
            existing = await db.execute(
                select(Plug).where(Plug.gateway_id == p_data["gateway_id"], Plug.local_ip == p_data["local_ip"])
            )
            plug = existing.scalar_one_or_none()
            if not plug:
                plug = Plug(
                    name=p_data["name"],
                    local_ip=p_data["local_ip"],
                    gateway_id=p_data["gateway_id"],
                    group_id=p_data["group_id"],
                    status=p_data["status"],
                    current_power_w=0.0
                )
                db.add(plug)
                await db.flush()
                print(f"✓ Created Plug: {plug.name} ({plug.local_ip})")
            else:
                print(f"  Plug {p_data['name']} already exists")
            plugs[p_data["name"]] = plug

        # --- 7. Historical Analytics, Sessions & Ledger ---
        # Generate 15 days of analytics sessions
        # VoltNetwork: CPO Tenant ID = 1
        # GreenCharge: CPO Tenant ID = 2
        now = datetime.now(timezone.utc)
        print("[*] Generating historical sessions & ledger logs for CPO charts...")

        # Check if sessions already exist to prevent duplicate seed generation on rerun
        session_check = await db.execute(select(ChargingSession).limit(1))
        if not session_check.scalar():
            for i in range(15):
                date_offset = now - timedelta(days=15 - i)

                # VoltNetwork session
                s1 = ChargingSession(
                    tenant_id=tenants["VoltNetwork"].id,
                    user_id=users["driver1@gmail.com"].id,
                    plug_id=plugs["Volt-FastPlug-01"].id,
                    started_at=date_offset - timedelta(hours=2),
                    ended_at=date_offset - timedelta(hours=1),
                    energy_kwh=18.5 + (i % 3) * 2.5,
                    peak_power_w=7200.0,
                    coins_spent=18.5 * 10 + (i % 3) * 25, # e.g. 10 coins per kWh
                    status=SessionStatus.COMPLETED
                )
                db.add(s1)

                # GreenCharge session
                s2 = ChargingSession(
                    tenant_id=tenants["GreenCharge"].id,
                    user_id=users["driver2@gmail.com"].id,
                    plug_id=plugs["Green-CityPlug-01"].id,
                    started_at=date_offset - timedelta(hours=4),
                    ended_at=date_offset - timedelta(hours=3),
                    energy_kwh=12.0 + (i % 2) * 4.0,
                    peak_power_w=3600.0,
                    coins_spent=12.0 * 12 + (i % 2) * 48, # e.g. 12 coins per kWh
                    status=SessionStatus.COMPLETED
                )
                db.add(s2)

                # Create corresponding top-up ledger entries to justify driver wallet balances
                if i % 3 == 0:
                    t1 = LedgerTransaction(
                        user_id=users["driver1@gmail.com"].id,
                        amount=100.0 + i * 20,
                        transaction_type=TransactionType.TOPUP,
                        description=f"Razorpay Wallet Top-up (pay_volt_{i})",
                        balance_after=500.0 + i * 20
                    )
                    db.add(t1)

            # Add an active session to demo the live monitor
            active_s = ChargingSession(
                tenant_id=tenants["VoltNetwork"].id,
                user_id=users["driver1@gmail.com"].id,
                plug_id=plugs["Volt-CorpPlug-02"].id,
                started_at=now - timedelta(minutes=45),
                energy_kwh=8.4,
                peak_power_w=7400.0,
                coins_spent=84.0,
                status=SessionStatus.ACTIVE
            )
            db.add(active_s)

            # Lock Volt-CorpPlug-02 as occupied
            plugs["Volt-CorpPlug-02"].status = PlugStatus.OCCUPIED
            plugs["Volt-CorpPlug-02"].current_power_w = 7400.0

            print("✓ Generated 30 historical completed sessions, 5 ledger transactions, and 1 active session.")
        else:
            print("  Sessions database already populated with records.")

        await db.commit()
        print("\n[+] Seeding successfully completed!")
        print("-" * 50)
        print("Default accounts for testing:")
        print("  Admin:  admin@amphive.com    / password123")
        print("  CPO 1:  cpo@voltnetwork.com   / password123")
        print("  CPO 2:  cpo@greencharge.com  / password123")
        print("  Driver: driver1@gmail.com     / password123")
        print("  Driver: driver2@gmail.com     / password123")
        print("-" * 50)

if __name__ == "__main__":
    asyncio.run(seed())
