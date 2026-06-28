"""
AmpHive Role-Based Access Control (RBAC) Service
==================================================
Provides FastAPI dependencies for enforcing user role requirements
on protected endpoints.

Usage:
    from backend.services.rbac import require_role

    @app.get("/api/cpo/dashboard")
    async def cpo_dashboard(user: User = Depends(require_role("cpo"))):
        ...

    # Multiple roles allowed:
    @app.get("/api/admin/users")
    async def admin_users(user: User = Depends(require_role("admin", "cpo"))):
        ...

Design decisions:
- Wraps the existing `get_current_user` dependency — authentication is
  checked first, then role authorization. This keeps auth and authz cleanly
  separated.
- Returns HTTP 403 Forbidden (not 401) when the user is authenticated but
  lacks the required role. This matches REST conventions: 401 = "who are you?",
  403 = "you can't do that".
- Accepts multiple roles via *args so endpoints can be opened to several
  roles without separate dependencies (e.g., admin OR cpo).
"""

from fastapi import Depends, HTTPException, status
from backend.database.models import User
from backend.services.auth import get_current_user


def require_role(*allowed_roles: str):
    """
    Factory that returns a FastAPI dependency enforcing role-based access.

    Args:
        *allowed_roles: One or more role strings (e.g., "cpo", "admin").
                        The user must have at least one of these roles.

    Returns:
        A FastAPI dependency function that returns the authenticated User
        if they have a permitted role, or raises HTTP 403 if not.

    Example:
        cpo_only = require_role("cpo")
        admin_or_cpo = require_role("admin", "cpo")
    """

    async def role_checker(user: User = Depends(get_current_user)) -> User:
        """
        Inner dependency: checks the authenticated user's role against
        the allowed roles list. Raises 403 if the user's role is not
        in the permitted set.
        """
        if user.role.value not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. This endpoint requires one of these roles: {', '.join(allowed_roles)}. "
                       f"Your role is '{user.role.value}'.",
            )
        return user

    return role_checker
