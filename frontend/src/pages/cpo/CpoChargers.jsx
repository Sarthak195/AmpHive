/**
 * CpoChargers (redesign v3, D2) — the operator's charger fleet: search +
 * filter (effective status / gateway / group), bulk actions (assign to
 * group, put in maintenance), per-charger Edit (identity / pricing / safety
 * / behavior), a printable QR bay-label, and the per-row maintenance
 * toggle. Replaces CpoPlugs.jsx.
 *
 * Data: GET /api/cpo/plugs, /api/cpo/gateways, /api/cpo/groups,
 * /api/cpo/tariffs. "Price now" previews GET /api/plugs/{id}/tariff-preview
 * per charger (best-effort — a fleet-wide bulk-preview endpoint doesn't
 * exist yet, so a slow/failed preview just leaves that row's price blank
 * rather than blocking the table).
 *
 * Mutations: PUT /api/cpo/plugs/{id} (identity/safety/behavior fields +
 * bulk group moves), PUT /api/cpo/plugs/{id}/tariff (pricing assignment),
 * POST /api/cpo/plugs/{id}/maintenance (per-row enter/clear — the dedicated,
 * audited maintenance workflow; refuses `clear` with an active session).
 * POST /api/cpo/plugs registers a new charger.
 *
 * Live updates (faster-gateway-offline-detection, lever 1+3): a 30s usePoll
 * refetch is the catch-all backstop (mirrors CpoGateways), and a
 * `plug_connectivity` socket listener (mirrors Dashboard.jsx's
 * handlePlugConnectivity) patches the effective-status computation in place
 * so a gateway going offline/online reflects immediately without waiting on
 * the poll. Patched as a plug_id-keyed override on top of the polled
 * gatewaysById status (cheaper than resolving plug_id -> gateway_id -> row
 * patch) and reset on every fetchAll so the poll always wins as the source
 * of truth between socket pushes.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { QRCodeSVG } from 'qrcode.react';
import {
  CheckCircle2,
  Pencil,
  Plus,
  Printer,
  QrCode,
  Search,
  Users,
  PlugZap,
  Wrench,
  Zap,
} from 'lucide-react';

import CpoLayout from '../../components/CpoLayout';
import PageHeader from '../../components/ui/PageHeader';
import DataTable from '../../components/ui/DataTable';
import Modal from '../../components/ui/Modal';
import ConfirmDialog from '../../components/ui/ConfirmDialog';
import StatusDot from '../../components/ui/StatusDot';
import Money from '../../components/ui/Money';
import { useToast } from '../../components/ui';
import api from '../../api/client';
import { useConfig } from '../../contexts/ConfigContext';
import { useSession } from '../../contexts/SessionContext';
import usePoll from '../../hooks/usePoll';
import { getPlugAvailability, AVAILABILITY_STATES, AVAILABILITY_LABELS } from '../../utils/plugAvailability';
import { apiErrorCopy } from '../../utils/statusCopy';
import './CpoChargers.css';

const QUEUED_OPTIONS = [
  { value: '', label: 'Inherit tenant default' },
  { value: 'true', label: 'Enabled' },
  { value: 'false', label: 'Disabled' },
];

function Section({ title, children }) {
  return (
    <div className="cpo-chargers-section">
      <h3 className="cpo-chargers-section-title">{title}</h3>
      {children}
    </div>
  );
}

export default function CpoChargers() {
  const { coin_inr_rate: rate = 1 } = useConfig();
  const { socket } = useSession() || {};
  const toast = useToast();
  const [searchParams] = useSearchParams();

  const [plugs, setPlugs] = useState([]);
  const [gateways, setGateways] = useState([]);
  const [groups, setGroups] = useState([]);
  const [tariffs, setTariffs] = useState([]);
  const [priceByPlug, setPriceByPlug] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  // Live plug_connectivity pushes, keyed by plug_id — overrides the polled
  // gatewaysById status until the next fetchAll (see the module doc comment
  // above). Reset on every successful poll so the poll stays authoritative.
  const [connectivityOverrides, setConnectivityOverrides] = useState({});

  const fetchAll = useCallback(async () => {
    setError(null);
    try {
      const [plugsRes, gatewaysRes, groupsRes, tariffsRes] = await Promise.all([
        api.get('/api/cpo/plugs'),
        api.get('/api/cpo/gateways'),
        api.get('/api/cpo/groups'),
        api.get('/api/cpo/tariffs'),
      ]);
      setPlugs(Array.isArray(plugsRes) ? plugsRes : plugsRes?.items || []);
      setGateways(Array.isArray(gatewaysRes) ? gatewaysRes : gatewaysRes?.items || []);
      setGroups(Array.isArray(groupsRes) ? groupsRes : groupsRes?.items || []);
      setTariffs(Array.isArray(tariffsRes) ? tariffsRes : tariffsRes?.items || []);
      setConnectivityOverrides({});
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  usePoll(fetchAll, 30_000);

  // Best-effort "price now" per charger. Non-blocking: a failed/slow preview
  // just leaves that row's price cell blank.
  useEffect(() => {
    if (plugs.length === 0) return undefined;
    let cancelled = false;
    Promise.allSettled(plugs.map((p) => api.get(`/api/plugs/${p.id}/tariff-preview`))).then((results) => {
      if (cancelled) return;
      setPriceByPlug((prev) => {
        const next = { ...prev };
        results.forEach((r, i) => {
          if (r.status === 'fulfilled' && r.value) next[plugs[i].id] = r.value.price_now;
        });
        return next;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [plugs]);

  // Gateway connectivity push (faster-gateway-offline-detection, lever 1) —
  // patches the effective-status computation without waiting on the 30s poll.
  useEffect(() => {
    if (!socket) return undefined;
    const handlePlugConnectivity = ({ plug_id, gateway_online }) => {
      setConnectivityOverrides((prev) => ({ ...prev, [plug_id]: gateway_online }));
    };
    socket.on('plug_connectivity', handlePlugConnectivity);
    return () => {
      socket.off('plug_connectivity', handlePlugConnectivity);
    };
  }, [socket]);

  const gatewaysById = useMemo(
    () => Object.fromEntries(gateways.map((g) => [g.id, g])),
    [gateways]
  );
  const tariffsById = useMemo(() => Object.fromEntries(tariffs.map((t) => [t.id, t])), [tariffs]);

  const effectiveState = useCallback(
    (plug) => {
      const override = connectivityOverrides[plug.id];
      const gateway_online =
        override !== undefined ? override : gatewaysById[plug.gateway_id]?.status === 'online';
      return getPlugAvailability({ ...plug, gateway_online });
    },
    [gatewaysById, connectivityOverrides]
  );

  /* ---- filters ------------------------------------------------------------ */
  const [search, setSearch] = useState('');
  const [stateFilter, setStateFilter] = useState('');
  const [gatewayFilter, setGatewayFilter] = useState('');
  const [groupFilter, setGroupFilter] = useState(() => searchParams.get('group') || '');

  const hasActiveFilters = Boolean(search.trim() || stateFilter || gatewayFilter || groupFilter);

  const filteredPlugs = plugs.filter((p) => {
    if (search.trim() && !p.name?.toLowerCase().includes(search.trim().toLowerCase())) return false;
    if (stateFilter && effectiveState(p) !== stateFilter) return false;
    if (gatewayFilter && p.gateway_id !== gatewayFilter) return false;
    if (groupFilter === 'none' && p.group_id != null) return false;
    if (groupFilter && groupFilter !== 'none' && String(p.group_id) !== groupFilter) return false;
    return true;
  });

  /* ---- selection + bulk actions -------------------------------------------- */
  const [selectedIds, setSelectedIds] = useState(new Set());
  const toggleSelect = (id) =>
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const selectAllShown = () => setSelectedIds(new Set(filteredPlugs.map((p) => p.id)));
  const clearSelection = () => setSelectedIds(new Set());

  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkGroupOpen, setBulkGroupOpen] = useState(false);
  const [bulkGroupValue, setBulkGroupValue] = useState('');
  const [bulkMaintenanceOpen, setBulkMaintenanceOpen] = useState(false);

  const confirmBulkGroup = async () => {
    const ids = Array.from(selectedIds);
    setBulkBusy(true);
    let ok = 0;
    let fail = 0;
    for (const id of ids) {
      try {
        await api.put(`/api/cpo/plugs/${id}`, {
          group_id: bulkGroupValue === '' ? 0 : Number(bulkGroupValue),
        });
        ok++;
      } catch {
        fail++;
      }
    }
    setBulkBusy(false);
    setBulkGroupOpen(false);
    setBulkGroupValue('');
    clearSelection();
    toast[fail === 0 ? 'ok' : 'warn'](
      fail === 0
        ? `Moved ${ok} charger${ok !== 1 ? 's' : ''} to the selected group.`
        : `Moved ${ok} charger${ok !== 1 ? 's' : ''}; ${fail} failed.`
    );
    await fetchAll();
  };

  const confirmBulkMaintenance = async () => {
    const ids = Array.from(selectedIds);
    setBulkBusy(true);
    let ok = 0;
    let fail = 0;
    for (const id of ids) {
      try {
        await api.put(`/api/cpo/plugs/${id}`, { status: 'maintenance' });
        ok++;
      } catch {
        fail++;
      }
    }
    setBulkBusy(false);
    setBulkMaintenanceOpen(false);
    clearSelection();
    toast[fail === 0 ? 'ok' : 'warn'](
      fail === 0
        ? `Put ${ok} charger${ok !== 1 ? 's' : ''} into maintenance.`
        : `Put ${ok} charger${ok !== 1 ? 's' : ''} into maintenance; ${fail} failed.`
    );
    await fetchAll();
  };

  /* ---- per-row maintenance toggle ------------------------------------------ */
  const [maintenanceTarget, setMaintenanceTarget] = useState(null);
  const [maintenanceBusy, setMaintenanceBusy] = useState(false);

  const confirmMaintenanceToggle = async () => {
    if (!maintenanceTarget) return;
    const action = maintenanceTarget.status === 'maintenance' ? 'clear' : 'enter';
    setMaintenanceBusy(true);
    try {
      await api.post(`/api/cpo/plugs/${maintenanceTarget.id}/maintenance`, { action });
      toast.ok(
        action === 'enter'
          ? `${maintenanceTarget.name} is now in maintenance.`
          : `${maintenanceTarget.name} is back in service.`
      );
      setMaintenanceTarget(null);
      await fetchAll();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setMaintenanceBusy(false);
    }
  };

  /* ---- add charger ---------------------------------------------------------- */
  const [addOpen, setAddOpen] = useState(false);
  const [addForm, setAddForm] = useState({ gateway_id: '', name: '', local_ip: '', group_id: '' });
  const [addError, setAddError] = useState('');
  const [addBusy, setAddBusy] = useState(false);

  const openAdd = () => {
    setAddForm({ gateway_id: '', name: '', local_ip: '', group_id: '' });
    setAddError('');
    setAddOpen(true);
  };

  const handleAddSubmit = async (e) => {
    e.preventDefault();
    if (!addForm.gateway_id || !addForm.name.trim() || !addForm.local_ip.trim()) {
      setAddError('Gateway, name, and IP address are required.');
      return;
    }
    setAddBusy(true);
    setAddError('');
    try {
      const body = {
        gateway_id: addForm.gateway_id,
        name: addForm.name.trim(),
        local_ip: addForm.local_ip.trim(),
      };
      if (addForm.group_id) body.group_id = Number(addForm.group_id);
      await api.post('/api/cpo/plugs', body);
      toast.ok(`${addForm.name.trim()} added.`);
      setAddOpen(false);
      await fetchAll();
    } catch (err) {
      setAddError(apiErrorCopy(err));
    } finally {
      setAddBusy(false);
    }
  };

  /* ---- edit charger ---------------------------------------------------------- */
  const [editingPlug, setEditingPlug] = useState(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editForm, setEditForm] = useState({
    name: '',
    group_id: '',
    max_current_a: '',
    queued_charging_enabled: '',
    auto_start_delay_min: '',
    tariff_id: '',
  });
  const [editError, setEditError] = useState('');
  const [editBusy, setEditBusy] = useState(false);

  const openEdit = (plug) => {
    setEditingPlug(plug);
    setEditForm({
      name: plug.name,
      group_id: plug.group_id != null ? String(plug.group_id) : '',
      max_current_a: plug.max_current_a ?? '',
      queued_charging_enabled:
        plug.queued_charging_enabled == null ? '' : String(plug.queued_charging_enabled),
      auto_start_delay_min: plug.auto_start_delay_min ?? '',
      tariff_id: plug.tariff_id != null ? String(plug.tariff_id) : '',
    });
    setEditError('');
    setEditOpen(true);
  };

  const closeEdit = () => {
    if (!editBusy) setEditOpen(false);
  };

  const handleEditSave = async (e) => {
    e.preventDefault();
    if (!editingPlug) return;
    setEditBusy(true);
    setEditError('');
    try {
      const body = {};
      if (editForm.name !== editingPlug.name) body.name = editForm.name;

      const newGroupId = editForm.group_id === '' ? 0 : Number(editForm.group_id);
      const oldGroupId = editingPlug.group_id || 0;
      if (newGroupId !== oldGroupId) body.group_id = newGroupId;

      if (String(editForm.max_current_a) !== String(editingPlug.max_current_a ?? '')) {
        body.max_current_a = editForm.max_current_a === '' ? 0 : Number(editForm.max_current_a);
      }

      const origQueued =
        editingPlug.queued_charging_enabled == null ? '' : String(editingPlug.queued_charging_enabled);
      if (editForm.queued_charging_enabled !== origQueued) {
        body.queued_charging_enabled =
          editForm.queued_charging_enabled === '' ? null : editForm.queued_charging_enabled === 'true';
      }

      if (String(editForm.auto_start_delay_min) !== String(editingPlug.auto_start_delay_min ?? '')) {
        body.auto_start_delay_min =
          editForm.auto_start_delay_min === '' ? null : Number(editForm.auto_start_delay_min);
      }

      if (Object.keys(body).length > 0) {
        await api.put(`/api/cpo/plugs/${editingPlug.id}`, body);
      }

      const origTariff = editingPlug.tariff_id != null ? String(editingPlug.tariff_id) : '';
      if (editForm.tariff_id !== origTariff) {
        await api.put(`/api/cpo/plugs/${editingPlug.id}/tariff`, {
          tariff_id: editForm.tariff_id === '' ? null : Number(editForm.tariff_id),
        });
      }

      toast.ok(`${editForm.name || editingPlug.name} updated.`);
      setEditOpen(false);
      setEditingPlug(null);
      await fetchAll();
    } catch (err) {
      setEditError(apiErrorCopy(err));
    } finally {
      setEditBusy(false);
    }
  };

  /* ---- QR modal ---------------------------------------------------------- */
  const [qrPlug, setQrPlug] = useState(null);

  /* ---- table columns ------------------------------------------------------ */
  const columns = [
    {
      key: 'select',
      label: '',
      render: (row) => (
        <input
          type="checkbox"
          aria-label={`Select ${row.name}`}
          checked={selectedIds.has(row.id)}
          onChange={() => toggleSelect(row.id)}
        />
      ),
    },
    { key: 'name', label: 'Name' },
    {
      key: 'status',
      label: 'Status',
      render: (row) => {
        const state = effectiveState(row);
        return <StatusDot state={state} label={AVAILABILITY_LABELS[state]} live={state === 'in_use'} />;
      },
    },
    {
      key: 'gateway',
      label: 'Gateway',
      render: (row) => {
        const gw = gatewaysById[row.gateway_id];
        const override = connectivityOverrides[row.id];
        const online = override !== undefined ? override : gw?.status === 'online';
        return (
          <span className="cpo-chargers-gw-cell">
            <StatusDot tone={online ? 'ok' : 'danger'} />
            {gw?.name || row.gateway_id}
          </span>
        );
      },
    },
    {
      key: 'group_name',
      label: 'Group',
      render: (row) => row.group_name || <span className="text-3 text-sm">Ungrouped</span>,
    },
    {
      key: 'tariff',
      label: 'Tariff',
      render: (row) =>
        row.tariff_id != null ? (
          tariffsById[row.tariff_id]?.name || `Tariff #${row.tariff_id}`
        ) : (
          <span className="text-3 text-sm">Inherited</span>
        ),
    },
    {
      key: 'price_now',
      label: 'Price now',
      num: true,
      render: (row) =>
        priceByPlug[row.id] != null ? (
          <span className="num">
            <Money coins={priceByPlug[row.id]} rate={rate} />
            /kWh
          </span>
        ) : (
          '—'
        ),
    },
    {
      key: 'actions',
      label: 'Actions',
      render: (row) => (
        <div className="cpo-chargers-row-actions">
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => openEdit(row)}>
            <Pencil size={14} aria-hidden="true" />
            Edit
          </button>
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => setQrPlug(row)}>
            <QrCode size={14} aria-hidden="true" />
            QR
          </button>
          <button
            type="button"
            className={`btn btn-sm ${row.status === 'maintenance' ? 'btn-quiet' : 'btn-danger'}`}
            onClick={() => setMaintenanceTarget(row)}
          >
            {row.status === 'maintenance' ? (
              <>
                <CheckCircle2 size={14} aria-hidden="true" />
                Clear maintenance
              </>
            ) : (
              <>
                <Wrench size={14} aria-hidden="true" />
                Put in maintenance
              </>
            )}
          </button>
        </div>
      ),
    },
  ];

  return (
    <CpoLayout>
      <PageHeader
        title="Chargers"
        sub="Manage your smart chargers, their groups, and pricing."
        actions={
          <button type="button" className="btn btn-primary" onClick={openAdd}>
            <Plus size={16} aria-hidden="true" />
            Add charger
          </button>
        }
      />

      <div className="filter-bar">
        <div className="field cpo-chargers-search">
          <label className="sr-only" htmlFor="cpo-chargers-search">
            Search chargers
          </label>
          <div className="cpo-chargers-search-row">
            <Search size={16} aria-hidden="true" />
            <input
              id="cpo-chargers-search"
              className="input"
              type="search"
              placeholder="Search by name"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
        </div>
        <select
          className="select"
          aria-label="Filter by status"
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value)}
        >
          <option value="">All statuses</option>
          {AVAILABILITY_STATES.map((s) => (
            <option key={s} value={s}>
              {AVAILABILITY_LABELS[s]}
            </option>
          ))}
        </select>
        <select
          className="select"
          aria-label="Filter by gateway"
          value={gatewayFilter}
          onChange={(e) => setGatewayFilter(e.target.value)}
        >
          <option value="">All gateways</option>
          {gateways.map((g) => (
            <option key={g.id} value={g.id}>
              {g.name}
            </option>
          ))}
        </select>
        <select
          className="select"
          aria-label="Filter by group"
          value={groupFilter}
          onChange={(e) => setGroupFilter(e.target.value)}
        >
          <option value="">All groups</option>
          <option value="none">Ungrouped</option>
          {groups.map((g) => (
            <option key={g.id} value={String(g.id)}>
              {g.name}
            </option>
          ))}
        </select>
        <span className="filter-count">
          {filteredPlugs.length} of {plugs.length} chargers
        </span>
        {filteredPlugs.length > 0 && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={selectAllShown}>
            Select all shown
          </button>
        )}
      </div>

      {selectedIds.size > 0 && (
        <div className="banner banner-info cpo-chargers-bulkbar">
          <span>{selectedIds.size} selected</span>
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => setBulkGroupOpen(true)}>
            <Users size={14} aria-hidden="true" />
            Assign to group
          </button>
          <button
            type="button"
            className="btn btn-quiet btn-sm"
            onClick={() => setBulkMaintenanceOpen(true)}
          >
            <Wrench size={14} aria-hidden="true" />
            Put in maintenance
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={clearSelection}>
            Clear
          </button>
        </div>
      )}

      <DataTable
        columns={columns}
        rows={filteredPlugs}
        loading={loading}
        error={error}
        onRetry={fetchAll}
        emptyIcon={PlugZap}
        emptyTitle={hasActiveFilters ? 'No chargers match your filters' : 'No chargers yet'}
        emptyBody={
          hasActiveFilters
            ? 'Try a different search or filter.'
            : 'Register your first smart charger to get started.'
        }
        emptyAction={
          !hasActiveFilters && (
            <button type="button" className="btn btn-primary" onClick={openAdd}>
              Add charger
            </button>
          )
        }
        collapse
      />

      {/* Add charger */}
      <Modal
        open={addOpen}
        onClose={() => !addBusy && setAddOpen(false)}
        title="Add charger"
        footer={
          <>
            <button type="button" className="btn btn-quiet" onClick={() => setAddOpen(false)} disabled={addBusy}>
              Cancel
            </button>
            <button type="button" className="btn btn-primary" onClick={handleAddSubmit} disabled={addBusy}>
              {addBusy ? 'Adding…' : 'Add charger'}
            </button>
          </>
        }
      >
        <form className="cpo-chargers-form" onSubmit={handleAddSubmit}>
          <div className="field">
            <label className="field-label" htmlFor="cpo-chargers-add-gateway">
              Gateway
            </label>
            <select
              id="cpo-chargers-add-gateway"
              className="select"
              value={addForm.gateway_id}
              onChange={(e) => setAddForm({ ...addForm, gateway_id: e.target.value })}
              required
            >
              <option value="">Select a gateway…</option>
              {gateways.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="cpo-chargers-add-name">
              Charger name
            </label>
            <input
              id="cpo-chargers-add-name"
              className="input"
              type="text"
              placeholder="e.g. Parking bay 1"
              value={addForm.name}
              onChange={(e) => setAddForm({ ...addForm, name: e.target.value })}
              required
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="cpo-chargers-add-ip">
              Local IP address
            </label>
            <input
              id="cpo-chargers-add-ip"
              className="input"
              type="text"
              placeholder="e.g. 192.168.20.10"
              value={addForm.local_ip}
              onChange={(e) => setAddForm({ ...addForm, local_ip: e.target.value })}
              required
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="cpo-chargers-add-group">
              Group (optional)
            </label>
            <select
              id="cpo-chargers-add-group"
              className="select"
              value={addForm.group_id}
              onChange={(e) => setAddForm({ ...addForm, group_id: e.target.value })}
            >
              <option value="">No group (public)</option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </div>
          {addError && <p className="field-error">{addError}</p>}
        </form>
      </Modal>

      {/* Edit charger */}
      <Modal
        open={editOpen}
        onClose={closeEdit}
        title={`Edit ${editingPlug?.name || 'charger'}`}
        size="lg"
        footer={
          <>
            <button type="button" className="btn btn-quiet" onClick={closeEdit} disabled={editBusy}>
              Cancel
            </button>
            <button type="button" className="btn btn-primary" onClick={handleEditSave} disabled={editBusy}>
              {editBusy ? 'Saving…' : 'Save changes'}
            </button>
          </>
        }
      >
        {editingPlug && (
          <form className="cpo-chargers-form" onSubmit={handleEditSave}>
            <Section title="Identity">
              <div className="field">
                <label className="field-label" htmlFor="cpo-chargers-edit-name">
                  Charger name
                </label>
                <input
                  id="cpo-chargers-edit-name"
                  className="input"
                  type="text"
                  value={editForm.name}
                  onChange={(e) => setEditForm({ ...editForm, name: e.target.value })}
                />
              </div>
              <div className="field">
                <label className="field-label" htmlFor="cpo-chargers-edit-group">
                  Group
                </label>
                <select
                  id="cpo-chargers-edit-group"
                  className="select"
                  value={editForm.group_id}
                  onChange={(e) => setEditForm({ ...editForm, group_id: e.target.value })}
                >
                  <option value="">No group (public)</option>
                  {groups.map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.name}
                    </option>
                  ))}
                </select>
              </div>
            </Section>

            <Section title="Pricing">
              <div className="field">
                <label className="field-label" htmlFor="cpo-chargers-edit-tariff">
                  Tariff
                </label>
                <select
                  id="cpo-chargers-edit-tariff"
                  className="select"
                  value={editForm.tariff_id}
                  onChange={(e) => setEditForm({ ...editForm, tariff_id: e.target.value })}
                >
                  <option value="">No override</option>
                  {tariffs.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </select>
                <p className="field-help">
                  Inherited from group/organization when no tariff is assigned directly.
                </p>
              </div>
            </Section>

            <Section title="Safety">
              <div className="field">
                <label className="field-label" htmlFor="cpo-chargers-edit-cap">
                  Max current (A)
                </label>
                <input
                  id="cpo-chargers-edit-cap"
                  className="input"
                  type="number"
                  min="0"
                  step="1"
                  placeholder="Default (16 A)"
                  value={editForm.max_current_a}
                  onChange={(e) => setEditForm({ ...editForm, max_current_a: e.target.value })}
                />
                <p className="field-help">
                  This charger's current draw for circuit-capacity math. Blank = the default
                  hardware cutoff.
                </p>
              </div>
              <div className="banner banner-warn">
                Below-16A limits are enforced when starting sessions, not by the plug hardware.
              </div>
            </Section>

            <Section title="Behavior">
              <div className="field">
                <label className="field-label" htmlFor="cpo-chargers-edit-queued">
                  Queued charging
                </label>
                <select
                  id="cpo-chargers-edit-queued"
                  className="select"
                  value={editForm.queued_charging_enabled}
                  onChange={(e) => setEditForm({ ...editForm, queued_charging_enabled: e.target.value })}
                >
                  {QUEUED_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
                <p className="field-help">
                  Let drivers queue a charge when this plug loses line power; it auto-starts when
                  power returns.
                </p>
              </div>
              <div className="field">
                <label className="field-label" htmlFor="cpo-chargers-edit-delay">
                  Auto-start delay (minutes)
                </label>
                <input
                  id="cpo-chargers-edit-delay"
                  className="input"
                  type="number"
                  min="0"
                  step="1"
                  placeholder="Inherit tenant default"
                  value={editForm.auto_start_delay_min}
                  onChange={(e) => setEditForm({ ...editForm, auto_start_delay_min: e.target.value })}
                />
                <p className="field-help">
                  How long power must stay on before a queued charge auto-starts.
                </p>
              </div>
            </Section>

            {editError && <p className="field-error">{editError}</p>}
          </form>
        )}
      </Modal>

      {/* QR bay label */}
      <Modal
        open={Boolean(qrPlug)}
        onClose={() => setQrPlug(null)}
        title="Charger QR code"
        size="sm"
        footer={
          <>
            <button type="button" className="btn btn-quiet" onClick={() => setQrPlug(null)}>
              Close
            </button>
            <button type="button" className="btn btn-primary" onClick={() => window.print?.()}>
              <Printer size={16} aria-hidden="true" />
              Print
            </button>
          </>
        }
      >
        {qrPlug && (
          <>
            <div className="print-area cpo-chargers-qr-sheet">
              <div className="cpo-chargers-qr-brand">
                <span className="brand-bolt">
                  <Zap size={16} aria-hidden="true" />
                </span>
                <span>AmpHive</span>
              </div>
              <div className="cpo-chargers-qr-box">
                <QRCodeSVG
                  value={`${window.location.origin}/?plug=${qrPlug.id}`}
                  size={200}
                  bgColor="#ffffff"
                  fgColor="#0C0E07"
                  level="M"
                  marginSize={2}
                />
              </div>
              <p className="cpo-chargers-qr-name">{qrPlug.name}</p>
              <p className="cpo-chargers-qr-hint">Scan to charge</p>
            </div>
            <p className="text-3 text-sm cpo-chargers-qr-footnote">
              Opens AmpHive with this charger prefilled — the driver still signs in and taps
              Start.
            </p>
          </>
        )}
      </Modal>

      {/* Per-row maintenance toggle */}
      <ConfirmDialog
        open={Boolean(maintenanceTarget)}
        onClose={() => !maintenanceBusy && setMaintenanceTarget(null)}
        onConfirm={confirmMaintenanceToggle}
        title={maintenanceTarget?.status === 'maintenance' ? 'Clear maintenance' : 'Put in maintenance'}
        confirmLabel={maintenanceTarget?.status === 'maintenance' ? 'Clear maintenance' : 'Put in maintenance'}
        tone={maintenanceTarget?.status === 'maintenance' ? 'primary' : 'danger'}
        busy={maintenanceBusy}
        body={
          maintenanceTarget?.status === 'maintenance'
            ? `${maintenanceTarget?.name} will be available to drivers again.`
            : `Take ${maintenanceTarget?.name} out of service. Drivers won't be able to start new sessions on it until you clear maintenance.`
        }
      />

      {/* Bulk: assign to group */}
      <Modal
        open={bulkGroupOpen}
        onClose={() => !bulkBusy && setBulkGroupOpen(false)}
        title={`Assign ${selectedIds.size} charger${selectedIds.size !== 1 ? 's' : ''} to a group`}
        size="sm"
        footer={
          <>
            <button
              type="button"
              className="btn btn-quiet"
              onClick={() => setBulkGroupOpen(false)}
              disabled={bulkBusy}
            >
              Cancel
            </button>
            <button type="button" className="btn btn-primary" onClick={confirmBulkGroup} disabled={bulkBusy}>
              {bulkBusy ? 'Assigning…' : 'Assign'}
            </button>
          </>
        }
      >
        <div className="field">
          <label className="field-label" htmlFor="cpo-chargers-bulk-group">
            Group
          </label>
          <select
            id="cpo-chargers-bulk-group"
            className="select"
            value={bulkGroupValue}
            onChange={(e) => setBulkGroupValue(e.target.value)}
          >
            <option value="">No group (public)</option>
            {groups.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </div>
      </Modal>

      {/* Bulk: put in maintenance */}
      <ConfirmDialog
        open={bulkMaintenanceOpen}
        onClose={() => !bulkBusy && setBulkMaintenanceOpen(false)}
        onConfirm={confirmBulkMaintenance}
        title="Put chargers into maintenance"
        confirmLabel="Put in maintenance"
        tone="danger"
        busy={bulkBusy}
        body={`Take ${selectedIds.size} charger${selectedIds.size !== 1 ? 's' : ''} out of service. Drivers won't be able to start new sessions on them until you clear each one.`}
      />
    </CpoLayout>
  );
}
