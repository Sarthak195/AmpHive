"""
Pluggable outbound email for the password-reset flow.

Two senders, selected by environment at call time (not import time, so tests
can monkeypatch os.environ):

- SMTP (STARTTLS via smtplib) when SMTP_HOST is set — SMTP_HOST / SMTP_PORT
  (default 587) / SMTP_USER / SMTP_PASSWORD / SMTP_FROM. Login is skipped when
  SMTP_USER is empty (e.g. an unauthenticated relay).
- Console fallback otherwise: a loud WARNING that mail is NOT being delivered.
  The message BODY — which for the reset/verification mails contains a live,
  single-use bearer token that takes over the account — is REDACTED by
  default, because "SMTP is misconfigured" is exactly the situation in which
  those tokens would otherwise be written into `docker logs`, journald and
  whatever log shipper is downstream. Set EMAIL_CONSOLE_FALLBACK=full to print
  the body (i.e. the link) when developing without a mail provider; the
  redacted log line names the variable, so the affordance stays discoverable.

Reset links point at FRONTEND_ORIGIN (default https://amphive.duckdns.org —
the Caddy front door; localhost dev sets FRONTEND_ORIGIN=http://localhost:5173).

smtplib is blocking, so the async wrapper runs it in a worker thread
(asyncio.to_thread) to keep the event loop free. Send failures are logged and
swallowed — the forgot-password endpoint must return the same generic 200
whether or not delivery worked (no enumeration via error/timing).
"""
import asyncio
import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger("amphive.email")


def frontend_origin() -> str:
    """Base URL the frontend is served from, for links in outbound email."""
    return os.getenv("FRONTEND_ORIGIN", "https://amphive.app").rstrip("/")


def _send_via_smtp(to_addr: str, subject: str, body: str) -> None:
    """Blocking STARTTLS send. Caller decides threading + error policy."""
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", user or "noreply@amphive.local")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def _console_fallback_shows_body() -> bool:
    """Whether the no-SMTP fallback may print the message body.

    Default NO: the password-reset and email-verification bodies carry a live
    single-use token, and a log stream is not an access-controlled channel.
    Local development opts in with EMAIL_CONSOLE_FALLBACK=full.
    """
    return os.getenv("EMAIL_CONSOLE_FALLBACK", "").strip().lower() in (
        "full", "1", "true", "yes", "on",
    )


def send_email(to_addr: str, subject: str, body: str) -> None:
    """Send via SMTP if configured, else log at WARNING (fallback)."""
    if os.getenv("SMTP_HOST"):
        try:
            _send_via_smtp(to_addr, subject, body)
            logger.info("Email sent via SMTP", extra={"to": to_addr, "subject": subject})
        except Exception:
            # Deliberately swallowed: callers (forgot-password) must not leak
            # delivery failures to the client. The full traceback goes to logs.
            logger.exception(
                "SMTP send failed", extra={"to": to_addr, "subject": subject}
            )
    elif _console_fallback_shows_body():
        logger.warning(
            "SMTP not configured (SMTP_HOST unset) — email NOT sent. "
            "To: %s | Subject: %s | Body:\n%s",
            to_addr, subject, body,
        )
    else:
        logger.warning(
            "SMTP not configured (SMTP_HOST unset) — email NOT sent. "
            "To: %s | Subject: %s | Body REDACTED (it can contain a live "
            "single-use token). Set EMAIL_CONSOLE_FALLBACK=full to print it "
            "for local development.",
            to_addr, subject,
        )


async def send_password_reset(to_addr: str, reset_link: str, ttl_min: int) -> None:
    """Email a password-reset link (or log it when SMTP is unconfigured)."""
    subject = "AmpHive password reset"
    body = (
        "Someone (hopefully you) requested a password reset for your AmpHive "
        "account.\n\n"
        f"Reset your password here (link expires in {ttl_min} minutes, "
        "single use):\n\n"
        f"{reset_link}\n\n"
        "If you didn't request this, you can safely ignore this email — your "
        "password is unchanged."
    )
    await asyncio.to_thread(send_email, to_addr, subject, body)


async def send_email_verification(to_addr: str, verify_link: str) -> None:
    """Email an address-verification link (or log it when SMTP is
    unconfigured). Mirrors send_password_reset — the whole registration flow
    must work without a provider, so the link is logged at WARNING as the
    console fallback when SMTP_HOST is unset."""
    subject = "Verify your AmpHive email address"
    body = (
        "Welcome to AmpHive! Please confirm this is your email address to "
        "finish creating your account.\n\n"
        "Verify your email here (single use):\n\n"
        f"{verify_link}\n\n"
        "If you didn't create an AmpHive account, you can safely ignore this "
        "email."
    )
    await asyncio.to_thread(send_email, to_addr, subject, body)
