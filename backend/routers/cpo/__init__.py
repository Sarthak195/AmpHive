"""
Cpo routes — moved verbatim from main.py (2026-07-07, TD#7 split), then
split again from a single 2000+ line module into this package (2026-07-21,
TD#7 follow-up) for maintainability. `router` is the single APIRouter that
backend/main.py mounts (`from backend.routers import cpo; app.include_router
(cpo.router)`, unchanged); each CPO domain (setup/profile, gateways, plugs,
groups, analytics, events/audit, payouts, offline top-ups, tariffs, disputes,
invoices, reservations) lives in its own `_<domain>.py` submodule with its own
APIRouter, included below in the same order the routes appeared in the
original file. Shared helpers (tenant-scope guard, cross-tenant tariff
loader, response builders) live in `_common.py`.

Route functions and a few domain-internal helpers are re-exported here so
existing `from backend.routers.cpo import <name>` imports — mostly tests
that invoke a handler coroutine directly with a mocked session — keep
working unchanged. Note that `unittest.mock.patch("backend.routers.cpo.X")`
call-sites that target a *module global* the handler reads (e.g. `state`)
must instead patch it on the submodule that actually defines it (e.g.
`backend.routers.cpo._gateways.state`) — patching the re-export here does
not affect the submodule's own copy of the name.
"""
from fastapi import APIRouter

from . import (
    _analytics,
    _disputes,
    _events,
    _gateways,
    _groups,
    _invoices,
    _payouts,
    _plug_photos,
    _plug_reports,
    _plugs,
    _profile,
    _reservations,
    _tariffs,
    _topups,
)
from ._analytics import (
    cpo_analytics_energy,
    cpo_analytics_overview,
    cpo_analytics_revenue,
    cpo_analytics_sessions,
    cpo_analytics_telemetry,
    cpo_export_sessions_csv,
)
from ._common import (
    _dispute_response,
    _fmt_min,
    _load_tenant_tariff,
    _payout_response,
    _plug_report_response,
    _require_tenant_id,
    _slot_dict,
    logger,
)
from ._disputes import cpo_list_disputes, cpo_resolve_dispute
from ._events import (
    cpo_acknowledge_event,
    cpo_gateway_logs,
    cpo_list_audit_log,
    cpo_list_events,
)
from ._gateways import (
    cpo_claim_gateway,
    cpo_create_gateway,
    cpo_gateway_ota,
    cpo_list_firmware_releases,
    cpo_list_gateways,
)
from ._groups import (
    cpo_create_group,
    cpo_delete_group,
    cpo_list_group_members,
    cpo_list_groups,
    cpo_remove_group_member,
    cpo_update_group,
    generate_unique_access_code,
)
from ._invoices import cpo_export_invoices_csv, cpo_list_invoices
from ._payouts import (
    cpo_cancel_payout,
    cpo_earnings,
    cpo_list_payouts,
    cpo_mark_payout_paid,
    cpo_request_payout,
)
from ._plug_photos import (
    cpo_approve_plug_photo,
    cpo_list_plug_photos,
    cpo_reject_plug_photo,
)
from ._plug_reports import cpo_list_plug_reports, cpo_resolve_plug_report
from ._plugs import (
    _publish_gateway_roster,
    cpo_create_plug,
    cpo_list_plugs,
    cpo_plug_maintenance,
    cpo_update_plug,
)
from ._profile import cpo_profile, cpo_setup, cpo_update_profile
from ._reservations import cpo_list_reservations
from ._tariffs import (
    cpo_assign_group_tariff,
    cpo_assign_plug_tariff,
    cpo_assign_tenant_default_tariff,
    cpo_create_tariff,
    cpo_create_tariff_slot,
    cpo_delete_tariff,
    cpo_delete_tariff_slot,
    cpo_list_tariff_slots,
    cpo_list_tariffs,
    cpo_update_tariff,
    cpo_update_tariff_slot,
)
from ._topups import cpo_create_topup, cpo_list_topups

# Re-exported for `from backend.routers.cpo import <name>` call-sites (see
# module docstring above) — listed explicitly so ruff's F401 (unused-import)
# recognizes this as the deliberate public surface, not dead code.
__all__ = [
    "router",
    "cpo_analytics_energy", "cpo_analytics_overview", "cpo_analytics_revenue",
    "cpo_analytics_sessions", "cpo_analytics_telemetry", "cpo_export_sessions_csv",
    "_dispute_response", "_fmt_min", "_load_tenant_tariff", "_payout_response",
    "_plug_report_response",
    "_require_tenant_id", "_slot_dict", "logger",
    "cpo_list_disputes", "cpo_resolve_dispute",
    "cpo_list_plug_reports", "cpo_resolve_plug_report",
    "cpo_list_plug_photos", "cpo_approve_plug_photo", "cpo_reject_plug_photo",
    "cpo_acknowledge_event", "cpo_gateway_logs", "cpo_list_audit_log", "cpo_list_events",
    "cpo_claim_gateway", "cpo_create_gateway", "cpo_gateway_ota",
    "cpo_list_firmware_releases", "cpo_list_gateways",
    "cpo_create_group", "cpo_delete_group", "cpo_list_group_members", "cpo_list_groups",
    "cpo_remove_group_member", "cpo_update_group", "generate_unique_access_code",
    "cpo_export_invoices_csv", "cpo_list_invoices",
    "cpo_cancel_payout", "cpo_earnings", "cpo_list_payouts", "cpo_mark_payout_paid",
    "cpo_request_payout",
    "_publish_gateway_roster", "cpo_create_plug", "cpo_list_plugs",
    "cpo_plug_maintenance", "cpo_update_plug",
    "cpo_profile", "cpo_setup", "cpo_update_profile",
    "cpo_list_reservations",
    "cpo_create_topup", "cpo_list_topups",
    "cpo_assign_group_tariff", "cpo_assign_plug_tariff",
    "cpo_assign_tenant_default_tariff", "cpo_create_tariff", "cpo_create_tariff_slot",
    "cpo_delete_tariff", "cpo_delete_tariff_slot", "cpo_list_tariff_slots",
    "cpo_list_tariffs", "cpo_update_tariff", "cpo_update_tariff_slot",
]

router = APIRouter()
# Routes are spliced in directly (not via include_router()) so `router.routes`
# ends up with the exact same flat list of plain APIRoute objects the single-
# file router used to hold — include_router() on this FastAPI version wraps
# the sub-router in a lazy `_IncludedRouter` proxy instead of flattening it
# eagerly, which is transparent to real request dispatch but breaks the few
# tests that introspect `router.routes` directly (e.g. asserting a path is
# registered). Same order the routes appeared in the original monolithic file.
for _sub in (
    _profile.router, _gateways.router, _plugs.router, _groups.router,
    _analytics.router, _events.router, _payouts.router, _topups.router,
    _tariffs.router, _disputes.router, _invoices.router, _reservations.router,
    _plug_reports.router, _plug_photos.router,
):
    router.routes.extend(_sub.routes)
router._mark_routes_changed()
del _sub
