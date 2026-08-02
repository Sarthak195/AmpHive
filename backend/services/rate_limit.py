"""
In-process rate limiting for the auth endpoints (SECURITY.md §8.6).

Single-process by design: the backend runs as one uvicorn container, so an
in-memory sliding window is sufficient — no Redis/shared store. Counters reset
on restart, which is acceptable for brute-force / enumeration protection (an
attacker cannot trigger restarts).

Rules are env-configurable as "<attempts>/<window seconds>":
  LOGIN_RATE_LIMIT           (default 10/60   — 10 attempts per minute per IP)
  REGISTER_RATE_LIMIT        (default 10/3600 — 10 registrations per hour per IP)
  FORGOT_PASSWORD_RATE_LIMIT (default 5/3600  — 5 reset emails per hour per IP)
  RESET_PASSWORD_RATE_LIMIT  (default 10/3600 — 10 token submissions per hour per IP)

A blanket per-IP window additionally covers EVERY /api route (middleware, not a
per-route dependency) as the defense-in-depth floor under the dedicated rules:
  API_RATE_LIMIT             (default 300/60 — 300 requests per minute per IP;
                              "off"/"0" disables the blanket layer entirely)

Per-account limits (below) close the gap the per-IP limiters above leave open:
a single account rotating source IPs (e.g. a residential proxy pool, or just a
mobile client hopping cell towers) is invisible to a limiter keyed on IP alone.
These are layered ON TOP of the per-IP limiters, not a replacement:
  SESSION_START_ACCOUNT_RATE_LIMIT         (default 20/60 — 20 session starts per minute per account)
  SESSION_STOP_ACCOUNT_RATE_LIMIT          (default 20/60 — 20 session stops per minute per account)
  PAYMENTS_CREATE_ORDER_ACCOUNT_RATE_LIMIT (default 10/60 — 10 payment orders per minute per account)
  CPO_TOPUP_ACCOUNT_RATE_LIMIT             (default 20/60 — 20 offline top-ups per minute per CPO actor)
  LOGIN_ACCOUNT_RATE_LIMIT                 (default 10/60 — 10 login attempts per minute per account/email)
  CPO_GATEWAY_CLAIM_ACCOUNT_RATE_LIMIT     (default 10/60 — 10 gateway-claim attempts per minute per CPO actor)
"""
import logging
import os
import time
from collections import deque

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.database.models import User
from backend.schemas import LoginRequest
from backend.services.auth import get_current_user, normalize_email

logger = logging.getLogger("amphive.rate_limit")


class SlidingWindowRateLimiter:
    """Allow at most `max_attempts` calls per `window_sec` per key.

    Blocked calls are not recorded, so a client that keeps retrying is not
    punished beyond the window — the guarantee is simply "no more than
    max_attempts succeed per window". The clock is injectable for tests.
    """

    def __init__(self, max_attempts: int, window_sec: float, clock=time.monotonic):
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self._clock = clock
        self._hits: dict = {}
        self._last_sweep = clock()

    def check(self, key: str):
        """Record an attempt for `key` if allowed.

        Returns None when the attempt is allowed, else the number of seconds
        until the oldest counted attempt leaves the window (> 0).
        """
        now = self._clock()
        cutoff = now - self.window_sec
        dq = self._hits.get(key)
        if dq is None:
            dq = self._hits[key] = deque()
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= self.max_attempts:
            return dq[0] + self.window_sec - now
        dq.append(now)
        self._maybe_sweep(now)
        return None

    def _maybe_sweep(self, now):
        # Drop idle keys so the map can't grow without bound under an
        # address-rotating client.
        if now - self._last_sweep < self.window_sec:
            return
        self._last_sweep = now
        cutoff = now - self.window_sec
        for key in list(self._hits):
            dq = self._hits[key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if not dq:
                del self._hits[key]


def client_ip(request: Request) -> str:
    """Best-effort client IP behind the Caddy → frontend-nginx chain.

    Caddy (≥ 2.5) replaces a client-supplied X-Forwarded-For with the real
    peer address unless the peer is a configured trusted proxy, and the nginx
    hop appends its own upstream — so the first entry is the real client.
    With the public :8000 firewall port closed (2026-07-11) no untrusted peer
    reaches the backend directly; in the -NoTls rollback stack the header is
    client-forgeable, which only lets an attacker shard their own limit.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    first = fwd.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"


def _optional_limiter(env_name: str, default: str):
    """A SlidingWindowRateLimiter from env, or None when explicitly disabled
    ("off"/"0"/"none"/"disabled") — for layers that are safe to switch off,
    unlike the per-route rules above which fall back to their defaults."""
    raw = os.getenv(env_name, default).strip().lower()
    if raw in {"off", "0", "none", "disabled"}:
        logger.warning("%s=%r — blanket per-IP API rate limit DISABLED", env_name, raw)
        return None
    return SlidingWindowRateLimiter(*_rule_from_env(env_name, default))


def _rule_from_env(env_name: str, default: str):
    raw = os.getenv(env_name, default)
    try:
        attempts_s, window_s = raw.split("/", 1)
        attempts, window = int(attempts_s), float(window_s)
        if attempts < 1 or window <= 0:
            raise ValueError(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r (expected '<attempts>/<window seconds>'); using default %s",
            env_name, raw, default,
        )
        attempts_s, window_s = default.split("/", 1)
        attempts, window = int(attempts_s), float(window_s)
    return attempts, window


def rate_limit_dependency(limiter: SlidingWindowRateLimiter, action: str):
    """FastAPI dependency enforcing `limiter` per client IP; raises 429."""
    async def dependency(request: Request):
        retry_after = limiter.check(client_ip(request))
        if retry_after is not None:
            seconds = int(retry_after) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Too many {action} attempts. Try again in {seconds} s.",
                headers={"Retry-After": str(seconds)},
            )
    return dependency


def account_rate_limit_dependency(limiter: SlidingWindowRateLimiter, action: str):
    """FastAPI dependency enforcing `limiter` per authenticated account (keyed
    by user id, not IP) — layered ON TOP of a route's existing per-IP
    dependency, not a replacement for it. Closes the "one account, rotating
    IPs" gap: SlidingWindowRateLimiter's per-IP keying can't see repeated
    attempts from the same account arriving over different source addresses.

    Depends on `get_current_user`, which FastAPI resolves once per request and
    caches by default (Depends(..., use_cache=True) is the default) — a route
    that also takes `user: User = Depends(get_current_user)` (directly, or via
    require_role()) shares that cached User with this dependency, so wiring
    this in does not add a second DB hit.
    """
    async def dependency(user: User = Depends(get_current_user)) -> User:
        retry_after = limiter.check(f"user:{user.id}")
        if retry_after is not None:
            seconds = int(retry_after) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Too many {action} attempts on this account. Try again in {seconds} s.",
                headers={"Retry-After": str(seconds)},
            )
        return user
    return dependency


def login_account_rate_limit_dependency(limiter: SlidingWindowRateLimiter):
    """FastAPI dependency enforcing `limiter` per normalized login email —
    layered ON TOP of the existing per-IP login dependency, not a replacement
    for it. Closes the "one account, rotating IPs" gap for credential
    stuffing/brute force targeted at a single account.

    There is no authenticated user yet at /login, so this keys off the
    request body instead of get_current_user. It takes the body as `req:
    LoginRequest` — the SAME parameter name and type the route itself
    declares. FastAPI's dependency resolver merges body parameters that share
    a name across a route and its sub-dependencies into a single parsed body
    ("more than one dependency could have the same field... count them by
    name" — fastapi.dependencies.utils._should_embed_body_fields) instead of
    embedding each under its own key, so this does not change the request
    shape or double-parse the body. See test_rate_limiting.py for a wiring
    test that calls the route through FastAPI's own dependency resolution
    (TestClient) to prove it. (If this dependency's parameter were named
    anything other than `req`, FastAPI would instead require the client to
    send `{"req": {...}, "<other-name>": {...}}` and every existing login
    caller would start getting 422s — the matching name is load-bearing.)

    The 429 copy is deliberately IDENTICAL in shape to the per-IP login
    limiter's generic message — it must never let a caller distinguish "this
    email doesn't exist" from "this email is rate-limited" (no account
    enumeration oracle).
    """
    async def dependency(req: LoginRequest) -> None:
        retry_after = limiter.check(f"login:{normalize_email(req.email)}")
        if retry_after is not None:
            seconds = int(retry_after) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Too many login attempts. Try again in {seconds} s.",
                headers={"Retry-After": str(seconds)},
            )
    return dependency


# Health probes (Docker healthcheck, uptime monitors, the deploy smoke) must
# never 429 — an exhausted attacker bucket shading a monitor's IP would read
# as a fake outage.
API_RATE_LIMIT_EXEMPT_PATHS = frozenset({"/api/health"})


async def api_rate_limit_middleware(request: Request, call_next):
    """Blanket per-IP sliding window over every /api route — the
    defense-in-depth floor under the per-route dependencies above, so an
    endpoint without a dedicated rule can't be hammered freely (scraping,
    guessing, DB exhaustion). Layered UNDER those rules: a request that
    passes this floor still hits its route's own tighter limiter.

    Starlette HTTP middleware, not a dependency — it runs before routing, so
    unmatched /api/* probes (404 floods) spend budget too, and it must build
    its own JSONResponse (an HTTPException raised here would not reach
    FastAPI's exception handlers). Keyed by client_ip() like the per-IP
    dependencies. Non-/api paths (the Socket.io mount) pass through
    untouched. `api_rate_limiter` is looked up per-call so tests can patch
    it; None (API_RATE_LIMIT=off) disables the layer.
    """
    path = request.url.path
    if (
        api_rate_limiter is None
        or not path.startswith("/api/")
        or path in API_RATE_LIMIT_EXEMPT_PATHS
    ):
        return await call_next(request)
    retry_after = api_rate_limiter.check(client_ip(request))
    if retry_after is not None:
        seconds = int(retry_after) + 1
        return JSONResponse(
            status_code=429,
            content={"detail": f"Too many requests. Try again in {seconds} s."},
            headers={"Retry-After": str(seconds)},
        )
    return await call_next(request)


login_rate_limiter = SlidingWindowRateLimiter(*_rule_from_env("LOGIN_RATE_LIMIT", "10/60"))
register_rate_limiter = SlidingWindowRateLimiter(*_rule_from_env("REGISTER_RATE_LIMIT", "10/3600"))
# Password reset: forgot-password is tighter than login (each allowed call can
# trigger an outbound email), reset-password bounds online token guessing —
# though the 256-bit token makes brute force academic anyway.
forgot_password_rate_limiter = SlidingWindowRateLimiter(*_rule_from_env("FORGOT_PASSWORD_RATE_LIMIT", "5/3600"))
reset_password_rate_limiter = SlidingWindowRateLimiter(*_rule_from_env("RESET_PASSWORD_RATE_LIMIT", "10/3600"))
# Public, unauthenticated discovery map (GET /api/plugs/public). Generous — a
# browsing visitor may refresh/poll live availability — but bounded so the
# open endpoint can't be hammered to enumerate/scrape or exhaust the DB.
public_map_rate_limiter = SlidingWindowRateLimiter(*_rule_from_env("PUBLIC_MAP_RATE_LIMIT", "60/60"))

# --- Per-account limiters (layered on top of the per-IP limiters above) ---
session_start_account_rate_limiter = SlidingWindowRateLimiter(
    *_rule_from_env("SESSION_START_ACCOUNT_RATE_LIMIT", "20/60")
)
session_stop_account_rate_limiter = SlidingWindowRateLimiter(
    *_rule_from_env("SESSION_STOP_ACCOUNT_RATE_LIMIT", "20/60")
)
payments_create_order_account_rate_limiter = SlidingWindowRateLimiter(
    *_rule_from_env("PAYMENTS_CREATE_ORDER_ACCOUNT_RATE_LIMIT", "10/60")
)
cpo_topup_account_rate_limiter = SlidingWindowRateLimiter(
    *_rule_from_env("CPO_TOPUP_ACCOUNT_RATE_LIMIT", "20/60")
)
login_account_rate_limiter = SlidingWindowRateLimiter(
    *_rule_from_env("LOGIN_ACCOUNT_RATE_LIMIT", "10/60")
)
# [Claim-code onboarding] Bounds guessing at claim codes: the codes
# themselves are drawn from a large unambiguous alphabet (see
# routers/admin.py's inventory-mint helper) and every failure path returns
# the same generic 404 (routers/cpo/_gateways.py cpo_claim_gateway), but a
# rate limit is the practical anti-brute-force control regardless.
cpo_gateway_claim_account_rate_limiter = SlidingWindowRateLimiter(
    *_rule_from_env("CPO_GATEWAY_CLAIM_ACCOUNT_RATE_LIMIT", "10/60")
)

# Blanket /api floor (api_rate_limit_middleware above). 300/60 ≈ 5 req/s
# sustained per IP — far above what the SPA generates (a page-load fan-out is
# ~10-15 calls, the dashboard's usePoll backstop is one call per 30 s), with
# headroom for several drivers sharing one NAT'd hub Wi-Fi, while still
# bounding a single-IP scrape/flood. None when API_RATE_LIMIT=off.
api_rate_limiter = _optional_limiter("API_RATE_LIMIT", "300/60")
