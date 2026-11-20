"""[C9] Per-member price override + email invites to a private group.

1. DB-free: the override at session start (rate, frozen segment, hold), the
   reprice skip, the display/estimate threading, the invite rules (expiry,
   single use, wrong or unverified email, rate limit, email body) and the
   cpo_viewer masking of pending invite emails.
2. DB-gated (TEST_DATABASE_URL, CI's postgres service; skipped locally): the
   mark-then-reprice path against a real schema, the membership-override
   query, and an invite accept end to end (membership created, replay
   refused, the daily cap counted from the table).
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.database.models import UserRole

PLUG = 7
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


# =============================================================================
# 1a. Session start: the override beats the tariff and its TOU slots
# =============================================================================

async def _begin(override, max_kwh=10.0):
    from backend.services import session_start

    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    user = MagicMock(id=3, coin_balance=Decimal("500"))
    plug = MagicMock(id=PLUG, gateway_id="gw-1", local_ip="10.0.0.2", group_id=11)
    plug.name = "Bay 1"
    gateway = MagicMock(tenant_id=1)
    mqtt = MagicMock()
    mqtt.send_plug_command.return_value = True
    boundary = NOW + timedelta(hours=2)
    resolve = AsyncMock(return_value=(Decimal("5.00"), boundary))
    max_rate = AsyncMock(return_value=Decimal("9.00"))
    with patch.object(session_start, "check_circuit_admission", AsyncMock()), \
         patch.object(session_start, "member_price_override", AsyncMock(return_value=override)), \
         patch.object(session_start, "resolve_rate_window", resolve), \
         patch.object(session_start, "available_balance", AsyncMock(return_value=Decimal("500"))), \
         patch.object(session_start, "max_rate_over_window", max_rate), \
         patch.object(session_start, "set_plug_telemetry_interval", AsyncMock()), \
         patch.object(session_start.state, "mqtt_manager", mqtt), \
         patch.object(session_start.state, "telemetry_store", MagicMock()), \
         patch("backend.services.socketio_manager.emit_plug_status", AsyncMock()), \
         patch("backend.services.caps.effective_plug_cap", return_value=16.0), \
         patch("backend.services.notifications.notify", AsyncMock()):
        session = await session_start.begin_active_session(
            db, user, plug, gateway, max_kwh=max_kwh, max_duration_seconds=None,
        )
    return session, resolve, max_rate, boundary


@pytest.mark.asyncio
async def test_override_beats_plug_tariff_and_tou_and_sizes_the_hold():
    session, resolve, max_rate, _ = await _begin(Decimal("3.00"))
    assert session.rate_coins_per_kwh == Decimal("3.00")
    # Frozen: one flat segment, no TOU boundary to reprice at.
    assert session.rate_valid_until is None
    assert session.member_price_override is True
    resolve.assert_not_awaited()
    max_rate.assert_not_awaited()
    # Hold sized at the override (10 kWh x 3), not the TOU worst case (x 9).
    assert session.hold_coins == Decimal("30.00")


@pytest.mark.asyncio
async def test_non_member_pays_the_normal_rate():
    session, resolve, max_rate, boundary = await _begin(None)
    assert session.rate_coins_per_kwh == Decimal("5.00")
    assert session.rate_valid_until == boundary
    assert session.member_price_override is None
    resolve.assert_awaited_once()
    assert session.hold_coins == Decimal("90.00")


# =============================================================================
# 1b. Repricing skips an override session
# =============================================================================

@pytest.mark.asyncio
async def test_override_session_is_never_repriced(monkeypatch):
    import backend.services.pricing as pricing

    called = {"n": 0}

    async def _fake(db, plug, at=None):
        called["n"] += 1
        return Decimal("12.00"), None

    monkeypatch.setattr(pricing, "resolve_rate_window", _fake)
    # A tariff edit stamped rate_valid_until = now on it (the old behavior).
    session = SimpleNamespace(
        member_price_override=True,
        rate_coins_per_kwh=Decimal("3.00"),
        settled_cost_coins=Decimal("0.00"),
        rate_segment_start_kwh=0.0,
        rate_valid_until=NOW - timedelta(minutes=1),
        energy_kwh=4.0,
    )
    assert await pricing.reprice_session_if_due(MagicMock(), session, MagicMock(), at=NOW) is None
    assert session.rate_coins_per_kwh == Decimal("3.00")
    assert session.rate_valid_until is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_tariff_edit_mark_skips_override_sessions(monkeypatch):
    import backend.services.pricing as pricing

    monkeypatch.setattr(pricing, "AUTO_REPRICE_ACTIVE_SESSIONS", True)
    db = AsyncMock()
    await pricing.mark_tenant_sessions_for_reprice(db, tenant_id=1)
    stmt = db.execute.await_args.args[0]
    assert "member_price_override IS NOT true" in str(stmt.compile()).replace("\n", " ")


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["remove_member", "clear_price"])
async def test_revoke_during_a_session_leaves_it_running_at_the_frozen_rate(revoke, monkeypatch):
    import backend.services.pricing as pricing
    from backend.routers.cpo import _groups
    from backend.services.billing import session_cost

    session, _, _, _ = await _begin(Decimal("3.00"))
    hold = session.hold_coins

    # The CPO revokes the member (or clears their price) mid-session.
    membership = SimpleNamespace(price_per_kwh_override=Decimal("3.00"))
    db = AsyncMock()
    db.delete = AsyncMock()
    cpo = SimpleNamespace(id=2, tenant_id=1, email="cpo@example.com", role=UserRole.CPO)
    with patch.object(_groups, "try_record_audit", AsyncMock()):
        if revoke == "remove_member":
            db.execute = AsyncMock(side_effect=[_scalar(PRIVATE_GROUP), _scalar(membership)])
            await _groups.cpo_remove_group_member(11, 3, user=cpo, db=db)
            db.delete.assert_awaited_once_with(membership)
        else:
            db.execute = AsyncMock(side_effect=[_scalar(membership)])
            with patch.object(_groups, "_own_group", AsyncMock(return_value=PRIVATE_GROUP)):
                await _groups.cpo_set_member_price(
                    11, 3, _groups.MemberPriceRequest(price_per_kwh_override=None),
                    user=cpo, db=db,
                )
            assert membership.price_per_kwh_override is None

    # Anything that re-resolved the override or the tariff now would see "no
    # override" (or blow up): a running session must not ask either.
    gone = AsyncMock(return_value=None)
    tariff = AsyncMock(side_effect=AssertionError("reprice re-resolved the tariff"))
    monkeypatch.setattr(pricing, "member_price_override", gone)
    monkeypatch.setattr(pricing, "resolve_rate_window", tariff)
    session.rate_valid_until = NOW - timedelta(minutes=1)
    session.energy_kwh = 4.0
    assert await pricing.reprice_session_if_due(MagicMock(), session, MagicMock(), at=NOW) is None
    gone.assert_not_awaited()
    tariff.assert_not_awaited()

    assert session.rate_coins_per_kwh == Decimal("3.00")
    assert session.member_price_override is True
    assert session.hold_coins == hold
    # Finalize prices the later reading at the frozen override (8 kWh x 3).
    assert session_cost(session, 8.0) == Decimal("24.00")


# =============================================================================
# 1c. Display, preview and estimate use the member's override
# =============================================================================

def _rows(rows):
    r = MagicMock()
    r.all.return_value = rows
    return r


@pytest.mark.asyncio
async def test_member_price_overrides_maps_groups_and_skips_query_when_ungrouped():
    from backend.services.pricing import member_price_override, member_price_overrides

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_rows([(11, Decimal("3.5"))]))
    assert await member_price_overrides(db, 3, [11, 12, None]) == {11: Decimal("3.50")}
    assert await member_price_override(db, 3, SimpleNamespace(group_id=11)) == Decimal("3.50")

    db2 = AsyncMock()
    assert await member_price_overrides(db2, 3, [None]) == {}
    assert await member_price_overrides(db2, None, [11]) == {}
    db2.execute.assert_not_called()


@pytest.mark.asyncio
async def test_estimate_prices_the_whole_charge_at_the_override():
    from backend.services import estimate

    peak = SimpleNamespace(start_min=0, end_min=1440, days_mask=127, price_per_kwh=Decimal("15"))
    tariff = SimpleNamespace(id=1, price_per_kwh=Decimal("10"))
    db = AsyncMock()
    first = MagicMock()
    first.first.return_value = None
    slots = MagicMock()
    slots.scalars.return_value.all.return_value = [peak]
    # Power-basis history, then (only without an override) the tariff slots.
    db.execute = AsyncMock(side_effect=[first, slots])
    plug = SimpleNamespace(id=3, group_id=11, max_current_a=None)
    with patch.object(estimate, "_resolve_tariff_and_tz",
                      AsyncMock(return_value=(tariff, "Asia/Kolkata"))), \
         patch.object(estimate, "member_price_override", AsyncMock(return_value=Decimal("2.00"))), \
         patch.object(estimate, "_zone", lambda _n: timezone(timedelta(hours=5, minutes=30))):
        out = await estimate.estimate_charge(
            db, plug, 3, max_kwh=None, max_duration_min=60, start_at=NOW,
        )
    # 16 A x 230 V for 1 h = 3.68 kWh at the flat override 2.00, not the 15 slot.
    assert Decimal(out["cost_coins"]) == Decimal("7.36")
    assert out["rate_changes"] == []


async def _preview_with_override(tariff):
    from backend.routers import plugs as plugs_router
    from backend.services import pricing

    plug = SimpleNamespace(id=3, group_id=11, gateway_id="gw-1")
    res = MagicMock()
    res.scalar_one_or_none.return_value = plug
    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    user = SimpleNamespace(id=3, role=UserRole.DRIVER)
    # The route imports the resolver from services/pricing inside the call,
    # so the patch goes on that module.
    with patch.object(plugs_router, "ensure_plug_group_access", AsyncMock()), \
         patch.object(plugs_router, "member_price_override", AsyncMock(return_value=Decimal("2.50"))), \
         patch.object(pricing, "_resolve_tariff_and_tz",
                      AsyncMock(return_value=(tariff, "Asia/Kolkata"))):
        return await plugs_router.get_plug_tariff_preview(3, user=user, db=db)


@pytest.mark.asyncio
async def test_tariff_preview_shows_the_flat_override():
    out = await _preview_with_override(None)
    assert out == {"base_price_per_kwh": 2.5, "price_now": 2.5, "slots": [],
                   "cheapest_window": None, "idle_fee": None}


@pytest.mark.asyncio
async def test_tariff_preview_override_carries_the_idle_terms():
    # [C1 + C9] A member-priced session still snapshots the tariff's idle fee,
    # so the preview shows those terms next to the flat member price.
    tariff = SimpleNamespace(id=1, price_per_kwh=Decimal("10"), idle_fee_per_min=Decimal("1.50"),
                             idle_fee_cap=Decimal("60"), idle_grace_min=20)
    out = await _preview_with_override(tariff)
    assert out["price_now"] == 2.5
    assert out["slots"] == []
    assert out["idle_fee"] == {"grace_min": 20, "per_min": 1.5, "cap": 60.0}


# =============================================================================
# 1d. Invites: DB-free rules
# =============================================================================

def _invite(**kw):
    base = dict(
        id=5, group_id=11, tenant_id=1, email="friend@example.com",
        accepted_at=None, revoked_at=None, expires_at=NOW + timedelta(days=7),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _scalar(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    r.scalar_one.return_value = value
    r.scalar.return_value = value
    return r


def _driver(email="Friend@Example.com", verified=True):
    return SimpleNamespace(id=9, email=email, email_verified=verified, role=UserRole.DRIVER)


async def _accept(invite, user, *more):
    from backend.services import group_invites

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar(invite), *more])
    db.add = MagicMock()
    out = None
    err = None
    try:
        out = await group_invites.accept_invite(db, user, "raw-token")
    except HTTPException as exc:
        err = exc
    return db, out, err


@pytest.mark.asyncio
async def test_accept_creates_membership_for_the_verified_matching_email():
    consumed = MagicMock(rowcount=1)
    group = SimpleNamespace(id=11, name="Family")
    db, out, err = await _accept(_invite(), _driver(), consumed, _scalar(group), _scalar(None))
    assert err is None
    _, got_group, created = out
    assert got_group is group and created is True
    added = db.add.call_args.args[0]
    assert (added.user_id, added.group_id) == (9, 11)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("invite", [
    None,
    _invite(accepted_at=NOW - timedelta(minutes=5)),          # replay
    _invite(revoked_at=NOW - timedelta(minutes=5)),           # superseded / revoked
    _invite(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)),  # expired
])
async def test_unknown_used_revoked_or_expired_invite_is_one_generic_400(invite):
    db, _, err = await _accept(invite, _driver())
    assert err.status_code == 400
    assert err.detail == "Invalid or expired invite link."
    assert db.execute.await_count == 1
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_email_is_refused_without_consuming_or_leaking():
    db, _, err = await _accept(_invite(), _driver(email="someone@else.com"))
    assert err.status_code == 403
    assert "friend@example.com" not in err.detail
    assert db.execute.await_count == 1  # no consuming UPDATE ran
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_email_is_refused_without_consuming():
    db, _, err = await _accept(_invite(), _driver(verified=False))
    assert err.status_code == 403
    assert db.execute.await_count == 1
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_accept_loser_gets_the_generic_400():
    db, _, err = await _accept(_invite(), _driver(), MagicMock(rowcount=0))
    assert err.status_code == 400
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_invite_rate_limit_per_cpo(monkeypatch):
    from backend.services import group_invites

    monkeypatch.setattr(group_invites, "GROUP_INVITES_PER_DAY", 20)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar(20))
    group = SimpleNamespace(id=11, tenant_id=1)
    with pytest.raises(HTTPException) as exc:
        await group_invites.create_invite(db, SimpleNamespace(id=2), group, "a@b.co")
    assert exc.value.status_code == 429
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_invite_stores_only_the_digest_with_a_7_day_expiry():
    from backend.services import group_invites

    db = AsyncMock()
    # gateway check, per-CPO count, global count, supersede UPDATE
    db.execute = AsyncMock(side_effect=[_scalar("gw-1"), _scalar(0), _scalar(0), MagicMock()])
    db.add = MagicMock()
    group = SimpleNamespace(id=11, tenant_id=1)
    invite, token = await group_invites.create_invite(db, SimpleNamespace(id=2), group, "a@b.co")
    assert invite.token_hash == group_invites.hash_invite_token(token)
    assert token not in invite.token_hash
    assert invite.email == "a@b.co"
    ttl = invite.expires_at - datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < ttl <= timedelta(days=7)


@pytest.mark.asyncio
async def test_invite_global_daily_cap(monkeypatch):
    from backend.services import group_invites

    monkeypatch.setattr(group_invites, "GROUP_INVITES_GLOBAL_PER_DAY", 50)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar("gw-1"), _scalar(0), _scalar(50)])
    db.add = MagicMock()
    with pytest.raises(HTTPException) as exc:
        await group_invites.create_invite(
            db, SimpleNamespace(id=2), SimpleNamespace(id=11, tenant_id=1), "a@b.co"
        )
    assert exc.value.status_code == 429
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_invite_needs_a_tenant_that_owns_a_gateway():
    from backend.services import group_invites

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar(None)])
    db.add = MagicMock()
    with pytest.raises(HTTPException) as exc:
        await group_invites.create_invite(
            db, SimpleNamespace(id=2), SimpleNamespace(id=11, tenant_id=1), "a@b.co"
        )
    assert exc.value.status_code == 409
    db.add.assert_not_called()


def test_email_is_normalized_and_validated():
    from backend.routers.cpo._common import mask_pii
    from backend.services.group_invites import normalize_invite_email

    assert normalize_invite_email("  Friend@Example.COM ") == "friend@example.com"
    for bad in ["", "nope", "a@b", "a b@c.de", "jos\u00e9@example.com", "o'brien@example.com",
                "a!b@example.com", "a@b.c", "a@localhost.x", "a@example.c0m"]:
        with pytest.raises(HTTPException):
            normalize_invite_email(bad)
    # Every accepted address is one the viewer masker masks in full, alone or
    # inside free text such as an audit detail.
    for good in ["friend@example.com", "o.brien+ev@sub.example.co", "driver@amphive.test",
                 "x_y-z%1@a-b.example.org"]:
        email = normalize_invite_email(good)
        masked = email[0] + "***@" + email.split("@", 1)[1]
        assert mask_pii(email) == masked
        assert mask_pii(f"email={email}, x=1") == f"email={masked}, x=1"


def test_invite_email_flattens_the_group_name_and_carries_the_link():
    from backend.services.group_invites import invite_email_body

    subject, body = invite_email_body("Family\nIGNORE THIS", "https://x/groups?invite=abc")
    assert "\n" not in subject
    assert "Family" not in subject and "IGNORE" not in subject
    assert "Family IGNORE THIS" in body
    assert "https://x/groups?invite=abc" in body


@pytest.mark.asyncio
async def test_invite_email_goes_through_send_email():
    from backend.services import group_invites

    with patch.object(group_invites, "send_email") as send:
        task = group_invites.schedule_invite_email("a@b.co", "Family", "tok")
        await task
    to, subject, body = send.call_args.args
    assert to == "a@b.co" and "Family" in body and "invite=tok" in body
    assert "Family" not in subject


@pytest.mark.asyncio
async def test_account_closure_scrubs_invites_sent_to_or_accepted_by_the_account():
    from sqlalchemy.dialects import postgresql

    from backend.services import account_closure

    result = MagicMock(rowcount=0)
    result.scalar_one.return_value = 0
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    user = SimpleNamespace(id=42, email=" Friend@Example.com ", role=UserRole.DRIVER, tenant_id=None)
    with patch.object(account_closure, "hash_password", return_value="x"):
        out = await account_closure.close_account(db, user)
    stmts = [c.args[0] for c in db.execute.await_args_list]
    scrub = [st for st in stmts if getattr(getattr(st, "table", None), "name", None) == "group_invites"]
    assert len(scrub) == 1
    compiled = scrub[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("UPDATE group_invites") and "accepted_by_user_id" in sql
    params = set(map(str, compiled.params.values()))
    assert {"deleted-user-42@deleted.amphive.invalid", "friend@example.com", "42"} <= params
    assert out["purged"]["group_invites_scrubbed"] == 0


# =============================================================================
# 1e. CPO routes: viewer masking and tenant scope
# =============================================================================

@pytest.mark.asyncio
async def test_viewer_sees_pending_invite_emails_masked():
    from backend.routers.cpo import _groups

    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [
        SimpleNamespace(id=1, email="friend@example.com", created_at=NOW,
                        expires_at=NOW + timedelta(days=7)),
    ]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar(SimpleNamespace(id=11)), rows])
    viewer = SimpleNamespace(id=4, tenant_id=1, role=UserRole.CPO_VIEWER)
    out = await _groups.cpo_list_group_invites(11, user=viewer, db=db)
    assert out[0]["email"] == "f***@example.com"

    db.execute = AsyncMock(side_effect=[_scalar(SimpleNamespace(id=11)), rows])
    cpo = SimpleNamespace(id=2, tenant_id=1, role=UserRole.CPO)
    out = await _groups.cpo_list_group_invites(11, user=cpo, db=db)
    assert out[0]["email"] == "friend@example.com"


@pytest.mark.asyncio
async def test_override_on_another_tenants_group_is_404():
    from backend.routers.cpo import _groups

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar(None))
    cpo = SimpleNamespace(id=2, tenant_id=1, role=UserRole.CPO)
    with pytest.raises(HTTPException) as exc:
        await _groups.cpo_set_member_price(
            99, 9, _groups.MemberPriceRequest(price_per_kwh_override=3.0), user=cpo, db=db,
        )
    assert exc.value.status_code == 404
    db.commit.assert_not_awaited()


def test_override_price_bounds_match_a_tariff():
    from pydantic import ValidationError

    from backend.routers.cpo._groups import MemberPriceRequest

    assert MemberPriceRequest(price_per_kwh_override=None).price_per_kwh_override is None
    for bad in (0, -1, 1001):
        with pytest.raises(ValidationError):
            MemberPriceRequest(price_per_kwh_override=bad)


@pytest.mark.asyncio
async def test_viewer_cannot_write_through_the_cpo_gate():
    from backend.routers.cpo._common import require_cpo_tenant

    viewer = SimpleNamespace(id=4, tenant_id=1, role=UserRole.CPO_VIEWER)
    for method, path in [
        ("POST", "/api/cpo/groups/11/invites"),
        ("PUT", "/api/cpo/groups/11/members/9/price"),
    ]:
        request = SimpleNamespace(method=method, url=SimpleNamespace(path=path))
        with pytest.raises(HTTPException) as exc:
            await require_cpo_tenant(request, user=viewer)
        assert exc.value.status_code == 403


# =============================================================================
# 1f. Route happy paths: audit actions, and the raw token never leaks
# =============================================================================

PRIVATE_GROUP = SimpleNamespace(id=11, is_public=False, name="Family", tenant_id=1)
CPO = SimpleNamespace(id=2, tenant_id=1, role=UserRole.CPO)


@pytest.mark.asyncio
async def test_invite_route_mails_the_token_and_audits_without_it():
    from backend.routers.cpo import _groups

    invite = SimpleNamespace(id=5, expires_at=NOW + timedelta(days=7))
    audit = AsyncMock()
    with patch.object(_groups, "_own_group", AsyncMock(return_value=PRIVATE_GROUP)),          patch.object(_groups.group_invites, "create_invite",
                      AsyncMock(return_value=(invite, "raw-secret-token"))),          patch.object(_groups.group_invites, "schedule_invite_email") as mail,          patch.object(_groups, "try_record_audit", audit):
        out = await _groups.cpo_invite_to_group(
            11, _groups.GroupInviteRequest(email=" Friend@Example.com "), user=CPO, db=AsyncMock(),
        )
    mail.assert_called_once_with("friend@example.com", "Family", "raw-secret-token")
    assert "raw-secret-token" not in str(out)
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "group.invite"
    assert "raw-secret-token" not in kwargs["detail"]
    # The address lives in the invite row (scrubbed on account closure), not
    # in the retained audit trail.
    assert "friend@example.com" not in kwargs["detail"]


@pytest.mark.asyncio
async def test_invite_route_refuses_a_public_group():
    from backend.routers.cpo import _groups

    public = SimpleNamespace(id=12, is_public=True, name="Lot", tenant_id=1)
    with patch.object(_groups, "_own_group", AsyncMock(return_value=public)):
        with pytest.raises(HTTPException) as exc:
            await _groups.cpo_invite_to_group(
                12, _groups.GroupInviteRequest(email="a@b.co"), user=CPO, db=AsyncMock(),
            )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_revoke_route_audits():
    from backend.routers.cpo import _groups

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    audit = AsyncMock()
    with patch.object(_groups, "_own_group", AsyncMock(return_value=PRIVATE_GROUP)),          patch.object(_groups, "try_record_audit", audit):
        out = await _groups.cpo_revoke_group_invite(11, 5, user=CPO, db=db)
    assert out == {"status": "revoked", "invite_id": 5}
    assert audit.await_args.kwargs["action"] == "group.invite_revoke"


@pytest.mark.asyncio
async def test_price_route_sets_the_override_and_audits_old_and_new():
    from backend.routers.cpo import _groups

    membership = SimpleNamespace(price_per_kwh_override=None)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar(membership))
    audit = AsyncMock()
    with patch.object(_groups, "_own_group", AsyncMock(return_value=PRIVATE_GROUP)),          patch.object(_groups, "try_record_audit", audit):
        out = await _groups.cpo_set_member_price(
            11, 9, _groups.MemberPriceRequest(price_per_kwh_override=3.5), user=CPO, db=db,
        )
    assert membership.price_per_kwh_override == Decimal("3.50")
    assert out["price_per_kwh_override"] == 3.5
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "group.member_price"
    assert "old=None" in kwargs["detail"] and "new=3.50" in kwargs["detail"]


@pytest.mark.asyncio
async def test_accept_route_audits_in_the_groups_tenant():
    from backend.routers import groups as groups_router

    invite = SimpleNamespace(id=5, tenant_id=1)
    group = SimpleNamespace(id=11, name="Family")
    audit = AsyncMock()
    with patch.object(groups_router.group_invites, "accept_invite",
                      AsyncMock(return_value=(invite, group, True))),          patch.object(groups_router, "try_record_audit", audit):
        out = await groups_router.accept_group_invite(
            groups_router.AcceptGroupInviteRequest(token="raw-secret-token"),
            user=_driver(), db=AsyncMock(),
        )
    assert out == {"status": "joined", "group_id": 11, "group_name": "Family"}
    kwargs = audit.await_args.kwargs
    assert (kwargs["action"], kwargs["tenant_id"]) == ("group.invite_accept", 1)
    assert "raw-secret-token" not in kwargs["detail"]


# =============================================================================
# 2. DB-gated
# =============================================================================

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

db_gated = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = ["gateway_status", "plug_status", "session_status", "tx_type", "user_role"]


@pytest_asyncio.fixture()
async def factory():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.database.models import Base

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for enum_name in ENUM_TYPES:
            await conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(factory, verified=True, email="friend@example.com"):
    """Tenant, gateway, private group, a plug in it, a CPO and a driver."""
    from backend.database.models import ChargerGroup, Gateway, Plug, Tenant, User

    suffix = uuid.uuid4().hex[:8]
    async with factory() as db:
        tenant = Tenant(name=f"T-{suffix}")
        db.add(tenant)
        await db.flush()
        gw = Gateway(id=f"gw-{suffix}", tenant_id=tenant.id, name="gw", vpn_ip=f"gw-{suffix}")
        group = ChargerGroup(tenant_id=tenant.id, name="Family", is_public=False,
                             access_code=suffix.upper())
        db.add_all([gw, group])
        await db.flush()
        plug = Plug(gateway_id=gw.id, name="Bay", local_ip="10.0.0.5", group_id=group.id)
        cpo = User(email=f"cpo-{suffix}@example.com", hashed_password="x", full_name="Cpo",
                   role=UserRole.CPO, tenant_id=tenant.id, email_verified=True)
        driver = User(email=email, hashed_password="x", full_name="Friend",
                      email_verified=verified)
        db.add_all([plug, cpo, driver])
        await db.commit()
        return SimpleNamespace(tenant=tenant, group=group, plug=plug, cpo=cpo, driver=driver)


@db_gated
@pytest.mark.asyncio
async def test_db_tariff_edit_does_not_reprice_an_override_session(factory):
    from sqlalchemy import select

    from backend.database.models import ChargingSession, GroupMembership, SessionStatus
    from backend.services import pricing

    s = await _seed(factory)
    async with factory() as db:
        db.add(GroupMembership(user_id=s.driver.id, group_id=s.group.id,
                               price_per_kwh_override=Decimal("3.00")))
        override = ChargingSession(
            tenant_id=s.tenant.id, user_id=s.driver.id, plug_id=s.plug.id,
            status=SessionStatus.ACTIVE, rate_coins_per_kwh=Decimal("3.00"),
            member_price_override=True, energy_kwh=1.0,
        )
        normal = ChargingSession(
            tenant_id=s.tenant.id, user_id=s.cpo.id, plug_id=s.plug.id,
            status=SessionStatus.ACTIVE, rate_coins_per_kwh=Decimal("5.00"), energy_kwh=1.0,
        )
        db.add_all([override, normal])
        await db.commit()
        assert await pricing.member_price_override(db, s.driver.id, s.plug) == Decimal("3.00")
        assert await pricing.member_price_override(db, s.cpo.id, s.plug) is None

        with patch.object(pricing, "AUTO_REPRICE_ACTIVE_SESSIONS", True):
            await pricing.mark_tenant_sessions_for_reprice(db, s.tenant.id)
        await db.commit()
        rows = {
            r.id: r for r in (await db.execute(
                select(ChargingSession).execution_options(populate_existing=True)
            )).scalars()
        }
    assert rows[override.id].rate_valid_until is None
    assert rows[normal.id].rate_valid_until is not None
    async with factory() as db:
        again = await db.get(ChargingSession, override.id)
        assert await pricing.reprice_session_if_due(db, again, s.plug) is None
        assert again.rate_coins_per_kwh == Decimal("3.00")


@db_gated
@pytest.mark.asyncio
async def test_db_invite_accept_once_then_replay_refused(factory):
    from sqlalchemy import func, select

    from backend.database.models import GroupInvite, GroupMembership
    from backend.services import group_invites

    s = await _seed(factory, email="Friend@Example.com")
    async with factory() as db:
        group = await db.get(type(s.group), s.group.id)
        cpo = await db.get(type(s.cpo), s.cpo.id)
        _, token = await group_invites.create_invite(db, cpo, group, "friend@example.com")
        assert await group_invites.invites_sent_last_day(db, cpo.id, datetime.now(timezone.utc)) == 1
    async with factory() as db:
        driver = await db.get(type(s.driver), s.driver.id)
        _, _, created = await group_invites.accept_invite(db, driver, token)
        assert created is True
    async with factory() as db:
        driver = await db.get(type(s.driver), s.driver.id)
        with pytest.raises(HTTPException) as exc:
            await group_invites.accept_invite(db, driver, token)
        assert exc.value.status_code == 400
        members = (await db.execute(
            select(func.count(GroupMembership.id)).where(GroupMembership.group_id == s.group.id)
        )).scalar()
        assert members == 1
        stored = (await db.execute(select(GroupInvite.token_hash))).scalars().all()
        assert token not in stored


@db_gated
@pytest.mark.asyncio
async def test_db_daily_cap_counts_this_cpos_invites(factory, monkeypatch):
    from backend.services import group_invites

    monkeypatch.setattr(group_invites, "GROUP_INVITES_PER_DAY", 2)
    s = await _seed(factory)
    async with factory() as db:
        group = await db.get(type(s.group), s.group.id)
        cpo = await db.get(type(s.cpo), s.cpo.id)
        await group_invites.create_invite(db, cpo, group, "a@example.com")
        await group_invites.create_invite(db, cpo, group, "b@example.com")
        with pytest.raises(HTTPException) as exc:
            await group_invites.create_invite(db, cpo, group, "c@example.com")
        assert exc.value.status_code == 429


@db_gated
@pytest.mark.asyncio
async def test_db_ocpp_backed_gateway_does_not_unlock_invites(factory):
    """[C9 + C2] A Gateway row minted for a self-registered OCPP charge point
    is not a claimed charger, so it never anchors invite mail."""
    from backend.database.models import ChargerGroup, Gateway, OcppChargePoint, Tenant, User
    from backend.services import group_invites

    suffix = uuid.uuid4().hex[:8]
    async with factory() as db:
        tenant = Tenant(name=f"T-{suffix}")
        db.add(tenant)
        await db.flush()
        gw = Gateway(id=f"ocpp-cp{suffix}", tenant_id=tenant.id, name="cp", vpn_ip=f"ocpp-cp{suffix}")
        group = ChargerGroup(tenant_id=tenant.id, name="Family", is_public=False,
                             access_code=suffix.upper())
        cpo = User(email=f"cpo-{suffix}@example.com", hashed_password="x", full_name="Cpo",
                   role=UserRole.CPO, tenant_id=tenant.id, email_verified=True)
        db.add_all([gw, group, cpo])
        await db.flush()
        db.add(OcppChargePoint(charge_point_id=f"cp{suffix}", tenant_id=tenant.id,
                               gateway_id=gw.id, password_hash="x"))
        await db.commit()
        assert await group_invites.tenant_has_gateway(db, tenant.id) is False
        with pytest.raises(HTTPException) as exc:
            await group_invites.create_invite(db, cpo, group, "a@example.com")
        assert exc.value.status_code == 409

        db.add(Gateway(id=f"hw-{suffix}", tenant_id=tenant.id, name="hw", vpn_ip=f"hw-{suffix}"))
        await db.commit()
        assert await group_invites.tenant_has_gateway(db, tenant.id) is True


def test_invite_gateway_anchor_excludes_ocpp_backed_gateways():
    """DB-free pin of the same rule: the anchor query skips any gateway that
    backs an OCPP charge point."""
    import asyncio

    from backend.services import group_invites

    seen = []

    async def _execute(stmt):
        seen.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        return _scalar(None)

    db = SimpleNamespace(execute=_execute)
    assert asyncio.run(group_invites.tenant_has_gateway(db, 1)) is False
    assert "ocpp_charge_points" in seen[0]
    assert "NOT (EXISTS" in seen[0]


@db_gated
@pytest.mark.asyncio
async def test_db_limits_patch_resizes_the_hold_at_the_frozen_override(factory, monkeypatch):
    from sqlalchemy import select

    from backend import state
    from backend.database.models import ChargingSession, SessionStatus, Tariff, User
    from backend.routers.sessions import update_session_limits
    from backend.schemas import SessionLimitsUpdateRequest

    monkeypatch.setattr(state, "mqtt_manager", MagicMock())
    s = await _seed(factory)
    async with factory() as db:
        tariff = Tariff(tenant_id=s.tenant.id, name="Flat", price_per_kwh=Decimal("9.00"))
        db.add(tariff)
        await db.flush()
        plug = await db.get(type(s.plug), s.plug.id)
        plug.tariff_id = tariff.id
        driver = await db.get(User, s.driver.id)
        driver.coin_balance = Decimal("500.00")
        session = ChargingSession(
            tenant_id=s.tenant.id, user_id=s.driver.id, plug_id=s.plug.id,
            status=SessionStatus.ACTIVE, rate_coins_per_kwh=Decimal("3.00"),
            member_price_override=True, hold_coins=Decimal("3.00"),
            max_kwh=1.0, max_duration_seconds=1800, energy_kwh=0.0,
        )
        db.add(session)
        await db.commit()
        sid = session.id

    async with factory() as db:
        await update_session_limits(
            sid, SessionLimitsUpdateRequest(max_kwh=10.0), MagicMock(id=s.driver.id), db,
        )
    async with factory() as db:
        hold = (await db.execute(
            select(ChargingSession.hold_coins).where(ChargingSession.id == sid)
        )).scalar_one()
    # 10 kWh at the frozen 3.00, not at the plug tariff's 9.00.
    assert hold == Decimal("30.00")


@db_gated
@pytest.mark.asyncio
async def test_db_account_closure_scrubs_invite_addresses(factory):
    from sqlalchemy import select

    from backend.database.models import GroupInvite, User
    from backend.services import group_invites
    from backend.services.account_closure import close_account, tombstone_email

    s = await _seed(factory, email="Friend@Example.com")
    async with factory() as db:
        group = await db.get(type(s.group), s.group.id)
        cpo = await db.get(type(s.cpo), s.cpo.id)
        _, accepted_token = await group_invites.create_invite(db, cpo, group, "friend@example.com")
    async with factory() as db:
        driver = await db.get(User, s.driver.id)
        await group_invites.accept_invite(db, driver, accepted_token)
    async with factory() as db:
        group = await db.get(type(s.group), s.group.id)
        cpo = await db.get(type(s.cpo), s.cpo.id)
        # A second, still-pending invite to the same address.
        await group_invites.create_invite(db, cpo, group, "friend@example.com")
    async with factory() as db:
        driver = await db.get(User, s.driver.id)
        await close_account(db, driver)
    async with factory() as db:
        rows = (await db.execute(select(GroupInvite))).scalars().all()
    assert len(rows) == 2
    assert {r.email for r in rows} == {tombstone_email(s.driver.id)}
    assert all(r.accepted_at is not None or r.revoked_at is not None for r in rows)
