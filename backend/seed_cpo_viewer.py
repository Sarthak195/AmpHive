"""
Create or update the read-only CPO demo login (feature F10)
===========================================================
Creates ONE user with role cpo_viewer on an existing tenant, or resets the
password of the cpo_viewer that already owns the email, and prints a fresh
random password exactly once (it is stored only as a bcrypt hash).

A viewer can open the tenant's CPO console but every write is refused with
403 (services/auth.py enforce_viewer_read_only), and driver emails and phone
numbers are masked in CPO responses.

Safety:
  - Never converts an existing driver/cpo/admin account: if the email belongs
    to any non-viewer user the script aborts and changes nothing.
  - Re-running for an existing viewer bumps token_version, so every token
    issued with the old password stops working.

Usage (operator only, inside the backend container, after migration
0045_cpo_viewer_role):
  python -m backend.seed_cpo_viewer --tenant-id <id> --email <address> [--name "Demo viewer"]
"""

import argparse
import asyncio
import os
import secrets
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from backend.database.models import Tenant, User, UserRole  # noqa: E402
from backend.services.auth import hash_password, normalize_email  # noqa: E402


class SeedError(Exception):
    """Refused before any write; the message says why."""


async def upsert_viewer(db: AsyncSession, tenant_id: int, email: str, full_name: str) -> str:
    """Create or reset the viewer and return its new plaintext password. The
    caller commits. Raises SeedError (nothing written) on a missing tenant or
    an email owned by a non-viewer account."""
    email = normalize_email(email)
    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise SeedError(f"No tenant with id {tenant_id}.")

    user = (
        await db.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if user is not None and user.role != UserRole.CPO_VIEWER:
        raise SeedError(
            f"{email} already belongs to a '{user.role.value}' account; refusing to "
            "turn it into a read-only viewer. Pick a different email."
        )

    password = secrets.token_urlsafe(18)
    if user is None:
        db.add(User(
            email=email,
            hashed_password=hash_password(password),
            full_name=full_name,
            role=UserRole.CPO_VIEWER,
            tenant_id=tenant_id,
            email_verified=True,
        ))
    else:
        user.hashed_password = hash_password(password)
        user.tenant_id = tenant_id
        user.full_name = full_name
        user.is_disabled = False
        user.token_version = (user.token_version or 0) + 1
    return password


async def _main(args: argparse.Namespace) -> int:
    from backend.database.db import async_session_factory

    async with async_session_factory() as db:
        try:
            password = await upsert_viewer(db, args.tenant_id, args.email, args.name)
        except SeedError as exc:
            print(f"[seed_cpo_viewer] {exc}", file=sys.stderr)
            return 1
        await db.commit()
    print(f"cpo_viewer ready: {normalize_email(args.email)} on tenant {args.tenant_id}")
    print(f"password (shown once, store it now): {password}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="Demo viewer")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
