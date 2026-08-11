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
  VERIFY_EMAIL_RATE_LIMIT    (default 10/3600 — 10 verify-email submissions per hour per IP)
  RESEND_VERIFICATION_RATE_LIMIT (default 5/3600 — 5 resend-verification requests per hour per IP)

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
  LOGIN_ACCOUNT_RATE_LIMIT                 (default 10/60 — 10 FAILED login attempts per minute per email; a correct password never counts and clears the bucket)
  CPO_GATEWAY_CLAIM_ACCOUNT_RATE_LIMIT     (default 10/60 — 10 gateway-claim attempts per minute per CPO actor)
  GROUP_JOIN_ACCOUNT_RATE_LIMIT            (default 10/60 — 10 group-join attempts per minute per account)
  CPO_SETUP_ACCOUNT_RATE_LIMIT             (default 5/300 — 5 workspace-setup attempts per 5 minutes per account)
  FORGOT_PASSWORD_EMAIL_RATE_LIMIT         (default 3/3600 — 3 reset emails per hour per SUBMITTED email, across all source IPs)
  RESEND_VERIFICATION_EMAIL_RATE_LIMIT     (default 3/3600 — 3 verification emails per hour per SUBMITTED email, across all source IPs)
"""
import logging
import os
import time
from collections import deque

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.database.models import User
from backend.services.auth import get_current_user

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

    def record_failure(self, key: str):
        """Record a FAILED attempt for `key` and report whether it is now over
        budget. Same contract as ``check`` — None while within budget (the
        failure is counted, caller proceeds with the normal error), else the
        seconds until the oldest counted failure ages out (caller should 429).

        Named for its call site: unlike a pre-handler ``check``, this runs ONLY
        after a credential check has already FAILED, so a correct password never
        reaches it and can never be throttled. Over-budget failures are not
        re-recorded (inherited from ``check``), so the block lifts window_sec
        after the max_attempts-th failure and the per-key deque stays bounded.
        """
        return self.check(key)

    def reset(self, key: str) -> None:
        """Forget every recorded attempt for `key`. Called on a SUCCESSFUL
        login so a legitimate owner who mistyped a few times (or whose email an
        IP-rotating attacker was flooding with wrong passwords) is never locked
        out — the account bucket only ever reflects failures since the last
        success."""
        self._hits.pop(key, None)

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


# Production request path is: client → Caddy → frontend-nginx → backend. Both
# Caddy and frontend-nginx APPEND exactly one X-Forwarded-For entry (Caddy
# appends the real client it peers with; nginx appends Caddy), so the real
# client sits a fixed 2 hops from the RIGHT of the chain.
_DEFAULT_TRUSTED_PROXY_HOPS = 2


def _trusted_proxy_hops() -> int:
    """Number of trusted reverse-proxy hops that append to X-Forwarded-For
    between the real client and this backend (see ``client_ip``). Read per-call
    so it can be tuned for other topologies (an nginx-only stack, an extra CDN
    hop, the -NoTls rollback) via the TRUSTED_PROXY_HOPS env var, and overridden
    in tests. A non-positive / unparseable value clamps to the safe minimum 1."""
    raw = os.getenv("TRUSTED_PROXY_HOPS", str(_DEFAULT_TRUSTED_PROXY_HOPS)).strip()
    try:
        hops = int(raw)
    except ValueError:
        logger.warning(
            "Invalid TRUSTED_PROXY_HOPS=%r (expected a positive integer); using %d",
            raw, _DEFAULT_TRUSTED_PROXY_HOPS,
        )
        return _DEFAULT_TRUSTED_PROXY_HOPS
    return hops if hops >= 1 else 1


def client_ip(request: Request) -> str:
    """Best-effort client IP behind the Caddy → frontend-nginx proxy chain.

    X-Forwarded-For is a comma-separated chain that honest proxies APPEND to on
    the RIGHT — each proxy adds the address it received the connection from — so
    the LEFTMOST entry is the client-supplied, spoofable end. Trusting position
    0 lets an attacker mint an unlimited number of fresh per-IP rate-limit
    buckets (mailbomb forgot-password, walk past the login / register / blanket
    API floors) just by rotating a fabricated leftmost token. We therefore never
    trust the leftmost value.

    Instead we count a FIXED number of trusted hops from the RIGHT: with the
    Caddy + nginx stack the real client is ``chain[len - TRUSTED_PROXY_HOPS]``
    (default 2), and everything to its left is attacker-controlled and ignored.
    If the chain is shorter than the trusted-hop count (a direct hit on the
    backend's :8000, or a misconfigured proxy), no forwarded token is
    trustworthy and we fall back to the real peer address, then to "unknown".
    """
    hops = _trusted_proxy_hops()
    fwd = request.headers.get("x-forwarded-for", "")
    chain = [p.strip() for p in fwd.split(",") if p.strip()]
    if len(chain) >= hops:
        return chain[len(chain) - hops]
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


# NOTE: the per-account login limit is NOT a pre-handler dependency. Keying a
# hard pre-check on the VICTIM's email (as an earlier design did) let an
# IP-rotating attacker 429 a victim BEFORE the password was ever checked —
# locking the real owner out with the correct password (a targeted-lockout
# DoS). It now lives inside routers/auth.login: credentials are verified first,
# a SUCCESS clears the account's failure bucket (login_account_rate_limiter),
# and only FAILURES are counted — so a correct password is never rate-limited
# while distributed wrong-password brute force is still capped per email. See
# SlidingWindowRateLimiter.record_failure / .reset.


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
# Per-EMAIL forgot-password cap (routers/auth.forgot_password), layered ON TOP
# of the per-IP forgot_password_rate_limiter above. The per-IP limiter alone
# lets an attacker with an IP pool send 5×N genuine reset mails/hour to one
# victim's inbox (mailbomb + SMTP-reputation risk). Keyed on the SUBMITTED
# email and checked BEFORE the account lookup, so it fires identically whether
# or not the account exists — no enumeration oracle. In-process/per-worker,
# same caveat as the rest of this module.
forgot_password_email_rate_limiter = SlidingWindowRateLimiter(
    *_rule_from_env("FORGOT_PASSWORD_EMAIL_RATE_LIMIT", "3/3600")
)
# [Email verification] Per-IP cap on verify-email token submissions
# (routers/auth.verify_email). The 256-bit token makes online brute force
# academic, but this mirrors reset-password (which is rate-limited on the same
# reasoning) so an unauthenticated endpoint can't be hammered freely.
verify_email_rate_limiter = SlidingWindowRateLimiter(
    *_rule_from_env("VERIFY_EMAIL_RATE_LIMIT", "10/3600")
)
# [Email verification] Per-IP cap on resend-verification requests
# (routers/auth.resend_verification) — each allowed call with a real
# unverified account triggers an outbound email, so this is tighter than the
# submission limiters, exactly like forgot_password_rate_limiter.
resend_verification_rate_limiter = SlidingWindowRateLimiter(
    *_rule_from_env("RESEND_VERIFICATION_RATE_LIMIT", "5/3600")
)
# [Email verification] Per-EMAIL resend cap, layered ON TOP of the per-IP
# resend_verification_rate_limiter above (exact analogue of
# forgot_password_email_rate_limiter). Keyed on the normalized SUBMITTED email
# and checked BEFORE the account lookup, so it fires identically whether or not
# the account exists / is already verified — no enumeration oracle. Bounds how
# many verification mails any single inbox can be made to receive from all
# source IPs combined. In-process/per-worker, same caveat as the rest of this
# module.
resend_verification_email_rate_limiter = SlidingWindowRateLimiter(
    *_rule_from_env("RESEND_VERIFICATION_EMAIL_RATE_LIMIT", "3/3600")
)
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
# Per-EMAIL login FAILURE bucket (routers/auth.login). Counts only failed
# credential checks; a correct password clears it (.reset) and is never
# throttled — so this caps distributed (IP-rotating) brute force against one
# account WITHOUT ever locking out the real owner. Multi-worker caveat: like
# every limiter here it is in-process, so the effective cap is per worker.
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
# [Group join] Bounds guessing at private-group access codes (POST
# /api/groups/join): an 8-char alphanumeric code with no dedicated per-route
# limiter until now — only the blanket per-IP floor covered it. Same
# brute-force reasoning as the claim-code limiter above.
group_join_account_rate_limiter = SlidingWindowRateLimiter(
    *_rule_from_env("GROUP_JOIN_ACCOUNT_RATE_LIMIT", "10/60")
)
# [CPO setup] POST /api/cpo/setup leaks tenant-name existence (400 "a tenant
# with the name ... already exists"). The message stays — a driver setting up
# their own workspace needs to know to pick a different name — but this
# throttles using that oracle to probe for existing tenant names.
cpo_setup_account_rate_limiter = SlidingWindowRateLimiter(
    *_rule_from_env("CPO_SETUP_ACCOUNT_RATE_LIMIT", "5/300")
)

# Blanket /api floor (api_rate_limit_middleware above). 300/60 ≈ 5 req/s
# sustained per IP — far above what the SPA generates (a page-load fan-out is
# ~10-15 calls, the dashboard's usePoll backstop is one call per 30 s), with
# headroom for several drivers sharing one NAT'd hub Wi-Fi, while still
# bounding a single-IP scrape/flood. None when API_RATE_LIMIT=off.
api_rate_limiter = _optional_limiter("API_RATE_LIMIT", "300/60")
