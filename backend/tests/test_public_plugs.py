"""
Tests for the unauthenticated public discovery endpoint (GET /api/plugs/public,
backend/routers/plugs.py get_public_plugs).

DB-free: db.execute is mocked to return (plug, group_tariff_id, gateway,
tenant) rows, and resolve_price_display_batch / gateway_is_live are patched.
This covers the response PROJECTION and the coords-skip /
gateway-coord-fallback logic.

The PRIVACY guarantee itself — that only public/ungrouped plugs are returned,
never private-group (society/office) plugs — is enforced by the SQL WHERE clause
(`group_id IS NULL OR group_id IN <public groups>`, a direct subset of the
proven get_available_plugs accessibility filter) and is additionally verified
live against prod after deploy. A mocked db.execute can't exercise SQL filtering.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.database.models import PlugStatus


def _plug(id, name, status, lat=None, lon=None, rated_power_w=None, connector_type=None):
    # `name` is a reserved Mock ctor kwarg, so set it after construction.
    p = MagicMock(
        id=id, status=status, latitude=lat, longitude=lon,
        rated_power_w=rated_power_w, connector_type=connector_type,
    )
    p.name = name
    return p


@pytest.mark.asyncio
async def test_get_public_plugs_projects_skips_no_coords_and_falls_back_to_gateway():
    from backend.routers.plugs import get_public_plugs

    # p1: own coords; p2: no plug coords -> inherits its gateway's; p3: no coords
    # anywhere -> skipped (not mappable).
    p1, g1 = (
        _plug(1, "Pub A", PlugStatus.AVAILABLE, 12.9, 77.6,
              rated_power_w=3300, connector_type="Type 2"),
        MagicMock(latitude=None, longitude=None),
    )
    p2, g2 = _plug(2, "Pub B", PlugStatus.OCCUPIED), MagicMock(latitude=13.0, longitude=77.5)
    p3, g3 = _plug(3, "No Loc", PlugStatus.AVAILABLE), MagicMock(latitude=None, longitude=None)

    result = MagicMock()
    result.all.return_value = [
        (p1, None, g1, MagicMock()), (p2, None, g2, MagicMock()), (p3, None, g3, MagicMock()),
    ]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    # The batch pricing map deliberately has NO entry for p3 — pricing an
    # unmappable plug would KeyError, proving only mappable plugs are priced.
    with patch("backend.routers.plugs.resolve_price_display_batch",
               AsyncMock(return_value={1: (Decimal("5.00"), None, None),
                                       2: (Decimal("5.00"), None, None)})), \
         patch("backend.routers.plugs.gateway_is_live", return_value=True):
        out = await get_public_plugs(db=db)

    # p3 (no location) is dropped; p1/p2 returned in order.
    assert [r.id for r in out] == [1, 2]
    # Effective coords: p1 its own, p2 its gateway's.
    assert (out[0].latitude, out[0].longitude) == (12.9, 77.6)
    assert (out[1].latitude, out[1].longitude) == (13.0, 77.5)
    # Minimal safe projection.
    assert out[0].status == "available" and out[1].status == "occupied"
    assert out[0].price_per_kwh == 5.0
    assert out[0].gateway_online is True
    # [Discovery] Advertised specs pass through when set; unset stays None.
    assert out[0].rated_power_w == 3300 and out[0].connector_type == "Type 2"
    assert out[1].rated_power_w is None and out[1].connector_type is None


@pytest.mark.asyncio
async def test_get_public_plugs_marks_dead_gateway_offline():
    from backend.routers.plugs import get_public_plugs

    p, g = _plug(1, "Pub A", PlugStatus.AVAILABLE, 12.9, 77.6), MagicMock()
    result = MagicMock()
    result.all.return_value = [(p, None, g, MagicMock())]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    with patch("backend.routers.plugs.resolve_price_display_batch",
               AsyncMock(return_value={1: (Decimal("5.00"), None, None)})), \
         patch("backend.routers.plugs.gateway_is_live", return_value=False):
        out = await get_public_plugs(db=db)

    assert out[0].gateway_online is False


def test_public_plug_response_excludes_per_user_and_network_fields():
    """The public projection must not carry per-user or network fields even if a
    caller tried to pass them — the schema simply has no such attributes."""
    from backend.schemas import PublicPlugResponse

    r = PublicPlugResponse(id=1, name="Pub", status="available", latitude=1.0, longitude=2.0)
    dumped = r.model_dump()
    for leaked in ("watching", "reserved_now_by_me", "queue_available", "local_ip",
                   "plug_powered", "current_power_w"):
        assert leaked not in dumped
