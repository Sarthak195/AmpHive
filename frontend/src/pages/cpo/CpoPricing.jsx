/**
 * CpoPricing — pricing plans (tariffs), their time-of-day rate windows, and
 * where they're assigned (redesign v3 D6).
 *
 * Simple/Advanced split (owner ask: the full editor overwhelms a small CPO
 * who just wants to set one price):
 *  - **Simple** (`CpoPricingSimple.jsx`) — one "Price per kWh" field. Saving
 *    edits (or creates) the tenant's single flat tariff via the SAME tariff
 *    API below, strips any time-of-day slots off it, and makes it the
 *    tenant default — no new backend/schema.
 *  - **Advanced** — this file's original three sections, unchanged:
 *     1. Tariffs — name, base rate, slot count, assigned-to count; create/edit
 *        (PUT — finally wired from the CpoTariffs-era stub)/delete.
 *     2. Slot editor for the expanded tariff — existing slot create/delete +
 *        weekday chips + a 7x24 coverage grid (base rate = neutral tint).
 *        A window that crosses midnight (e.g. 22:00 -> 06:00) is submitted as
 *        two backend slots since the API requires start < end.
 *     3. Assignment — the tenant-wide default tariff, plus a compact table of
 *        every group/charger and a select to change its tariff (existing
 *        assignment endpoints: PUT .../groups/{id}/tariff, .../plugs/{id}/tariff,
 *        .../tenant/default-tariff).
 *  - **Smart detection** (`utils/pricingUniformity.js`) picks the default tab:
 *    Simple when the tenant's config is already one flat rate, Advanced when
 *    it isn't (so a custom schedule is never silently hidden). The user can
 *    still switch tabs manually; a non-uniform config shown in Simple gets a
 *    "custom schedule active" notice, and Save routes through a confirm
 *    dialog before overwriting it.
 *
 * Data: GET/POST/PUT/DELETE /api/cpo/tariffs[/{id}],
 *       GET/POST/DELETE /api/cpo/tariffs/{id}/slots[/{slotId}],
 *       GET /api/cpo/groups, GET /api/cpo/plugs, GET/PUT /api/cpo/profile.
 *
 * /api/cpo/groups and /api/cpo/plugs serialize `tariff_id` (None = inherit).
 * This page also updates its own local state optimistically after a
 * successful assign, so the page stays correct without waiting on a refetch.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Plus, Tags } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import PageHeader from '../../components/ui/PageHeader';
import DataTable from '../../components/ui/DataTable';
import Modal from '../../components/ui/Modal';
import ConfirmDialog from '../../components/ui/ConfirmDialog';
import Money from '../../components/ui/Money';
import ErrorState from '../../components/ui/ErrorState';
import Skeleton, { SkeletonTitle } from '../../components/ui/Skeleton';
import Tabs from '../../components/ui/Tabs';
import { useToast } from '../../components/ui';
import api from '../../api/client';
import { apiErrorCopy } from '../../utils/statusCopy';
import { useConfig } from '../../contexts/ConfigContext';
import { getPricingSummary } from '../../utils/pricingUniformity';
import CpoPricingSimple from './CpoPricingSimple';
import './CpoPricing.css';

// "HH:MM" -> minute-of-day. An empty/blank value yields null.
const toMinutes = (hhmm) => {
  if (!hhmm) return null;
  const [h, m] = hhmm.split(':').map(Number);
  if (Number.isNaN(h) || Number.isNaN(m)) return null;
  return h * 60 + m;
};

// minute-of-day -> "HH:MM" (1440 -> "24:00" for a window ending at midnight).
const fmtMinutes = (min) =>
  `${String(Math.floor(min / 60)).padStart(2, '0')}:${String(min % 60).padStart(2, '0')}`;

// Weekday bit convention matches the backend: Mon=bit0 ... Sun=bit6.
const WEEKDAYS = [['Mon', 0], ['Tue', 1], ['Wed', 2], ['Thu', 3], ['Fri', 4], ['Sat', 5], ['Sun', 6]];
const ALL_DAYS_MASK = 127;
const WEEKDAYS_MASK = 31;
const WEEKENDS_MASK = 96;

const daysLabel = (mask) => {
  const m = Number(mask);
  if (m === ALL_DAYS_MASK) return 'Every day';
  if (m === WEEKDAYS_MASK) return 'Weekdays';
  if (m === WEEKENDS_MASK) return 'Weekends';
  const on = WEEKDAYS.filter(([, i]) => (m >> i) & 1).map(([label]) => label);
  return on.length ? on.join(', ') : '—';
};

/** Splits a [start, end) window into one or two [start, end) windows so a
 * midnight-crossing entry (22:00 -> 06:00) becomes two backend slots — the
 * API rejects start >= end. An end time of 00:00 reads as "until midnight"
 * (1440), matching the prior tariff editor's convention. */
const splitWindow = (startMin, endMinRaw) => {
  const end = endMinRaw === 0 ? 1440 : endMinRaw;
  if (startMin < end) return [[startMin, end]];
  return [[startMin, 1440], [0, end]];
};

const CELL_TINTS = ['a', 'b', 'c', 'd', 'e'];

const CpoPricing = () => {
  const toast = useToast();
  const config = useConfig();

  const [tariffs, setTariffs] = useState([]);
  const [groups, setGroups] = useState([]);
  const [plugs, setPlugs] = useState([]);
  const [defaultTariffId, setDefaultTariffId] = useState(null);
  const [tenantTz, setTenantTz] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [slotCounts, setSlotCounts] = useState({});

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [tariffsRes, groupsRes, plugsRes, profileRes] = await Promise.all([
        api.get('/api/cpo/tariffs'),
        api.get('/api/cpo/groups'),
        api.get('/api/cpo/plugs'),
        api.get('/api/cpo/profile'),
      ]);
      setTariffs(tariffsRes || []);
      setGroups(groupsRes || []);
      setPlugs(plugsRes || []);
      setDefaultTariffId(profileRes?.tenant?.default_tariff_id ?? null);
      setTenantTz(profileRes?.tenant?.timezone || '');
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  // Background fetch of each tariff's slot count for the list column. Keyed
  // on the id set (not the array identity) so it doesn't refire on every
  // unrelated re-render.
  const tariffIdsKey = tariffs.map((t) => t.id).join(',');
  useEffect(() => {
    if (!tariffs.length) return undefined;
    let cancelled = false;
    Promise.allSettled(tariffs.map((t) => api.get(`/api/cpo/tariffs/${t.id}/slots`))).then(
      (results) => {
        if (cancelled) return;
        setSlotCounts((prev) => {
          const next = { ...prev };
          results.forEach((r, i) => {
            next[tariffs[i].id] =
              r.status === 'fulfilled' ? (r.value || []).length : (next[tariffs[i].id] ?? null);
          });
          return next;
        });
      }
    );
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on tariffIdsKey
  }, [tariffIdsKey]);

  const assignedCounts = useMemo(() => {
    const counts = {};
    tariffs.forEach((t) => {
      counts[t.id] = 0;
    });
    groups.forEach((g) => {
      if (g.tariff_id != null && counts[g.tariff_id] != null) counts[g.tariff_id] += 1;
    });
    plugs.forEach((p) => {
      if (p.tariff_id != null && counts[p.tariff_id] != null) counts[p.tariff_id] += 1;
    });
    if (defaultTariffId != null && counts[defaultTariffId] != null) counts[defaultTariffId] += 1;
    return counts;
  }, [tariffs, groups, plugs, defaultTariffId]);

  /* ---- Simple/Advanced mode + smart detection ----------------------------- */

  // null while the initial load (or the single-tariff slot-count check) is
  // still in flight — see utils/pricingUniformity.
  const pricingSummary = useMemo(() => {
    if (loading || error) return null;
    return getPricingSummary({
      tariffs,
      groups,
      plugs,
      defaultTariffId,
      slotCounts,
      fallbackRate: config.coins_per_kwh,
    });
  }, [loading, error, tariffs, groups, plugs, defaultTariffId, slotCounts, config.coins_per_kwh]);

  const [mode, setMode] = useState('simple');
  const modeInitialized = useRef(false);
  useEffect(() => {
    if (modeInitialized.current || pricingSummary == null) return;
    setMode(pricingSummary.uniform ? 'simple' : 'advanced');
    modeInitialized.current = true;
  }, [pricingSummary]);

  // What the Simple field starts filled with: the resolved flat price when
  // uniform, else the tenant default (or first) tariff's price as a sane
  // starting point for "replace the custom schedule with…".
  const simpleSeedPrice = useMemo(() => {
    if (!pricingSummary) return '';
    if (pricingSummary.uniform) return String(pricingSummary.price);
    const fallbackTariff = tariffs.find((t) => t.id === defaultTariffId) || tariffs[0];
    return fallbackTariff ? String(fallbackTariff.price_per_kwh) : '';
  }, [pricingSummary, tariffs, defaultTariffId]);

  const [simpleBusy, setSimpleBusy] = useState(false);
  const [simpleError, setSimpleError] = useState('');

  // Simple-mode Save: edits (or creates) the tenant's one flat tariff via the
  // existing tariff API, strips any time-of-day slots off it (a flat rate
  // "broadcasts" the same value across every weekday/phase by construction —
  // no slot means the base rate covers every hour), makes it the tenant
  // default, and clears any group/charger pinned to a *different* tariff so
  // the single price genuinely applies everywhere.
  const applySimplePrice = useCallback(
    async (price) => {
      setSimpleError('');
      setSimpleBusy(true);
      try {
        const target = tariffs.find((t) => t.id === defaultTariffId) || tariffs[0] || null;
        let targetId;
        if (target) {
          targetId = target.id;
          await api.put(`/api/cpo/tariffs/${targetId}`, { name: target.name, price_per_kwh: price });
        } else {
          const created = await api.post('/api/cpo/tariffs', {
            name: 'Standard rate',
            price_per_kwh: price,
          });
          targetId = created.tariff_id;
        }

        const existingSlots = await api.get(`/api/cpo/tariffs/${targetId}/slots`);
        for (const slot of existingSlots || []) {
          // Sequential on purpose, same reasoning as submitSlot below — keep
          // one in-flight request per tariff against the server's overlap/
          // existence checks.
          await api.delete(`/api/cpo/tariffs/${targetId}/slots/${slot.id}`);
        }

        await api.put('/api/cpo/tenant/default-tariff', { tariff_id: targetId });

        const overrideGroups = groups.filter((g) => g.tariff_id != null && g.tariff_id !== targetId);
        const overridePlugs = plugs.filter((p) => p.tariff_id != null && p.tariff_id !== targetId);
        for (const g of overrideGroups) {
          await api.put(`/api/cpo/groups/${g.id}/tariff`, { tariff_id: null });
        }
        for (const p of overridePlugs) {
          await api.put(`/api/cpo/plugs/${p.id}/tariff`, { tariff_id: null });
        }

        toast.ok('Price updated — applied to every charger.');
        await fetchAll();
        return true;
      } catch (err) {
        setSimpleError(apiErrorCopy(err));
        return false;
      } finally {
        setSimpleBusy(false);
      }
    },
    [tariffs, groups, plugs, defaultTariffId, fetchAll, toast]
  );

  /* ---- create / edit tariff ---------------------------------------------- */

  const [formOpen, setFormOpen] = useState(false);
  const [editingTariff, setEditingTariff] = useState(null); // null = create mode
  const [formName, setFormName] = useState('');
  const [formPrice, setFormPrice] = useState('');
  const [formBusy, setFormBusy] = useState(false);
  const [formError, setFormError] = useState('');

  const openCreate = () => {
    setEditingTariff(null);
    setFormName('');
    setFormPrice('');
    setFormError('');
    setFormOpen(true);
  };

  const openEdit = (t) => {
    setEditingTariff(t);
    setFormName(t.name);
    setFormPrice(String(t.price_per_kwh));
    setFormError('');
    setFormOpen(true);
  };

  const submitTariffForm = async (e) => {
    e.preventDefault();
    setFormError('');
    const name = formName.trim();
    const price = Number(formPrice);
    if (!name) {
      setFormError('Enter a tariff name.');
      return;
    }
    if (!(price > 0)) {
      setFormError('Enter a rate greater than 0.');
      return;
    }
    setFormBusy(true);
    try {
      if (editingTariff) {
        await api.put(`/api/cpo/tariffs/${editingTariff.id}`, { name, price_per_kwh: price });
        toast.ok(`Saved "${name}".`);
      } else {
        await api.post('/api/cpo/tariffs', { name, price_per_kwh: price });
        toast.ok(`Created "${name}".`);
      }
      setFormOpen(false);
      await fetchAll();
    } catch (err) {
      setFormError(apiErrorCopy(err));
    } finally {
      setFormBusy(false);
    }
  };

  /* ---- delete tariff ------------------------------------------------------ */

  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const confirmDeleteTariff = async () => {
    if (!deleteTarget) return;
    setDeleteBusy(true);
    try {
      await api.delete(`/api/cpo/tariffs/${deleteTarget.id}`);
      toast.ok(`Deleted "${deleteTarget.name}".`);
      if (selectedTariffId === deleteTarget.id) setSelectedTariffId(null);
      setDeleteTarget(null);
      await fetchAll();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setDeleteBusy(false);
    }
  };

  /* ---- slot editor for the expanded tariff -------------------------------- */

  const [selectedTariffId, setSelectedTariffId] = useState(null);
  const [slots, setSlots] = useState([]);
  const [slotsLoading, setSlotsLoading] = useState(false);
  const [slotsError, setSlotsError] = useState(null);

  const loadSlots = useCallback(async (tariffId) => {
    setSlotsLoading(true);
    setSlotsError(null);
    try {
      const data = await api.get(`/api/cpo/tariffs/${tariffId}/slots`);
      setSlots(data || []);
      setSlotCounts((prev) => ({ ...prev, [tariffId]: (data || []).length }));
    } catch (err) {
      setSlotsError(err);
    } finally {
      setSlotsLoading(false);
    }
  }, []);

  const toggleSlots = (t) => {
    if (selectedTariffId === t.id) {
      setSelectedTariffId(null);
      return;
    }
    setSelectedTariffId(t.id);
    loadSlots(t.id);
  };

  const selectedTariff = tariffs.find((t) => t.id === selectedTariffId) || null;

  const [slotStart, setSlotStart] = useState('18:00');
  const [slotEnd, setSlotEnd] = useState('22:00');
  const [slotPrice, setSlotPrice] = useState('');
  const [slotDays, setSlotDays] = useState(ALL_DAYS_MASK);
  const [slotBusy, setSlotBusy] = useState(false);
  const [slotFormError, setSlotFormError] = useState('');

  const submitSlot = async (e) => {
    e.preventDefault();
    setSlotFormError('');
    const start = toMinutes(slotStart);
    const endRaw = toMinutes(slotEnd);
    if (start == null || endRaw == null) {
      setSlotFormError('Enter a start and end time.');
      return;
    }
    if (start === endRaw) {
      setSlotFormError('Start and end time must be different.');
      return;
    }
    if (!slotDays) {
      setSlotFormError('Select at least one day.');
      return;
    }
    const price = Number(slotPrice);
    if (!(price > 0)) {
      setSlotFormError('Enter a rate greater than 0.');
      return;
    }

    const windows = splitWindow(start, endRaw);
    setSlotBusy(true);
    try {
      for (const [s, en] of windows) {
        // Sequential on purpose: a Promise.all would race the two halves of
        // one split window against the server's own overlap check.
        await api.post(`/api/cpo/tariffs/${selectedTariffId}/slots`, {
          start_min: s,
          end_min: en,
          price_per_kwh: price,
          days_mask: slotDays,
        });
      }
      toast.ok(windows.length > 1 ? 'Slot added (split across midnight).' : 'Slot added.');
      setSlotPrice('');
      await loadSlots(selectedTariffId);
    } catch (err) {
      setSlotFormError(apiErrorCopy(err));
    } finally {
      setSlotBusy(false);
    }
  };

  const [slotDeleteTarget, setSlotDeleteTarget] = useState(null);
  const [slotDeleteBusy, setSlotDeleteBusy] = useState(false);

  const confirmDeleteSlot = async () => {
    if (!slotDeleteTarget) return;
    setSlotDeleteBusy(true);
    try {
      await api.delete(`/api/cpo/tariffs/${selectedTariffId}/slots/${slotDeleteTarget.id}`);
      toast.ok('Slot removed.');
      setSlotDeleteTarget(null);
      await loadSlots(selectedTariffId);
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setSlotDeleteBusy(false);
    }
  };

  // 7x24 coverage: which slot (if any) governs each weekday/hour cell.
  const coverage = useMemo(() => {
    const sorted = [...slots].sort((a, b) => a.start_min - b.start_min);
    const byDay = WEEKDAYS.map(() => Array(24).fill(null));
    sorted.forEach((slot, idx) => {
      const tint = CELL_TINTS[idx % CELL_TINTS.length];
      WEEKDAYS.forEach(([, bit], dayIdx) => {
        if (!((slot.days_mask >> bit) & 1)) return;
        for (let h = 0; h < 24; h++) {
          const cellStart = h * 60;
          const cellEnd = cellStart + 60;
          if (slot.start_min < cellEnd && slot.end_min > cellStart) {
            byDay[dayIdx][h] = { slot, tint };
          }
        }
      });
    });
    return { byDay, sorted };
  }, [slots]);

  /* ---- assignment ---------------------------------------------------------- */

  const assignTenantDefault = async (tariffId) => {
    try {
      await api.put('/api/cpo/tenant/default-tariff', { tariff_id: tariffId });
      setDefaultTariffId(tariffId);
      toast.ok('Default tariff updated.');
    } catch (err) {
      toast.error(apiErrorCopy(err));
    }
  };

  const assignGroupTariff = async (groupId, tariffId) => {
    try {
      await api.put(`/api/cpo/groups/${groupId}/tariff`, { tariff_id: tariffId });
      setGroups((gs) => gs.map((g) => (g.id === groupId ? { ...g, tariff_id: tariffId } : g)));
      toast.ok('Pricing updated.');
    } catch (err) {
      toast.error(apiErrorCopy(err));
    }
  };

  const assignPlugTariff = async (plugId, tariffId) => {
    try {
      await api.put(`/api/cpo/plugs/${plugId}/tariff`, { tariff_id: tariffId });
      setPlugs((ps) => ps.map((p) => (p.id === plugId ? { ...p, tariff_id: tariffId } : p)));
      toast.ok('Pricing updated.');
    } catch (err) {
      toast.error(apiErrorCopy(err));
    }
  };

  const assignmentRows = useMemo(
    () => [
      ...groups.map((g) => ({
        key: `group-${g.id}`,
        kind: 'Group',
        name: g.name,
        tariffId: g.tariff_id ?? null,
        onAssign: (tariffId) => assignGroupTariff(g.id, tariffId),
      })),
      ...plugs.map((p) => ({
        key: `plug-${p.id}`,
        kind: 'Charger',
        name: p.name,
        tariffId: p.tariff_id ?? null,
        onAssign: (tariffId) => assignPlugTariff(p.id, tariffId),
      })),
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps -- assign* are stable per-row closures over ids already in scope
    [groups, plugs]
  );

  /* ---- render --------------------------------------------------------------- */

  const tariffColumns = [
    { key: 'name', label: 'Tariff' },
    {
      key: 'price_per_kwh',
      label: 'Base rate',
      num: true,
      render: (t) => (
        <>
          <Money coins={t.price_per_kwh} rate={config.coin_inr_rate} showCoins /> /kWh
        </>
      ),
    },
    { key: 'slots', label: 'Slots', num: true, render: (t) => slotCounts[t.id] ?? '—' },
    { key: 'assigned', label: 'Assigned to', num: true, render: (t) => assignedCounts[t.id] ?? 0 },
    {
      key: 'actions',
      label: '',
      render: (t) => (
        <div className="row">
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => toggleSlots(t)}>
            {selectedTariffId === t.id ? 'Hide slots' : 'Manage slots'}
          </button>
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => openEdit(t)}>
            Edit
          </button>
          <button
            type="button"
            className="btn btn-danger btn-sm"
            onClick={() => setDeleteTarget(t)}
          >
            Delete
          </button>
        </div>
      ),
    },
  ];

  const slotColumns = [
    {
      key: 'window',
      label: 'Window',
      num: true,
      render: (s) => `${fmtMinutes(s.start_min)}–${fmtMinutes(s.end_min)}`,
    },
    { key: 'days', label: 'Days', render: (s) => daysLabel(s.days_mask ?? ALL_DAYS_MASK) },
    {
      key: 'rate',
      label: 'Rate',
      num: true,
      render: (s) => (
        <>
          <Money coins={s.price_per_kwh} rate={config.coin_inr_rate} showCoins /> /kWh
        </>
      ),
    },
    {
      key: 'actions',
      label: '',
      render: (s) => (
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => setSlotDeleteTarget(s)}
        >
          Remove
        </button>
      ),
    },
  ];

  const assignmentColumns = [
    {
      key: 'kind',
      label: 'Type',
      render: (r) => <span className={`badge ${r.kind === 'Group' ? 'badge-info' : 'badge-brand'}`}>{r.kind}</span>,
    },
    { key: 'name', label: 'Name' },
    {
      key: 'tariff',
      label: 'Pricing',
      render: (r) => (
        <select
          className="select"
          aria-label={`Pricing for ${r.name}`}
          value={r.tariffId ?? ''}
          onChange={(e) => r.onAssign(e.target.value ? Number(e.target.value) : null)}
        >
          <option value="">Inherited</option>
          {tariffs.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      ),
    },
  ];

  return (
    <CpoLayout>
      <PageHeader
        title="Pricing"
        sub={
          mode === 'simple' ? (
            'Set the rate every charger bills at.'
          ) : tenantTz ? (
            <>
              Tariffs and time-of-day rates — slot times are in <strong>{tenantTz}</strong>.
            </>
          ) : (
            'Tariffs and time-of-day rates.'
          )
        }
        actions={
          mode === 'advanced' ? (
            <button type="button" className="btn btn-primary" onClick={openCreate}>
              <Plus size={16} aria-hidden="true" /> New tariff
            </button>
          ) : undefined
        }
      />

      {loading ? (
        <div className="stack">
          <SkeletonTitle />
          <Skeleton lines={5} />
        </div>
      ) : error ? (
        <ErrorState error={error} onRetry={fetchAll} />
      ) : pricingSummary == null ? (
        <div className="stack">
          <SkeletonTitle />
          <Skeleton lines={3} />
        </div>
      ) : (
        <>
          <div className="pricing-mode-tabs">
            <Tabs
              tabs={[
                { id: 'simple', label: 'Simple' },
                { id: 'advanced', label: 'Advanced' },
              ]}
              active={mode}
              onChange={setMode}
              ariaLabel="Pricing editor mode"
            />
          </div>

          {mode === 'simple' ? (
            <CpoPricingSimple
              pricingSummary={pricingSummary}
              seedPrice={simpleSeedPrice}
              coinInrRate={config.coin_inr_rate}
              busy={simpleBusy}
              error={simpleError}
              onSave={applySimplePrice}
              onSwitchToAdvanced={() => setMode('advanced')}
            />
          ) : (
            <>
              <section className="stack pricing-section">
                <h2>Tariffs</h2>
                <DataTable
                  columns={tariffColumns}
                  rows={tariffs}
                  emptyIcon={Tags}
                  emptyTitle="No tariffs yet"
                  emptyBody="Create a pricing plan to set your rate per kWh, then assign it below."
                  emptyAction={
                    <button type="button" className="btn btn-primary" onClick={openCreate}>
                      New tariff
                    </button>
                  }
                />
              </section>

              {selectedTariff && (
                <section className="card pricing-slot-editor">
                  <div className="row-between">
                    <h2>{selectedTariff.name} — time-of-day slots</h2>
                    <button
                      type="button"
                      className="btn btn-quiet btn-sm"
                      onClick={() => setSelectedTariffId(null)}
                    >
                      Close
                    </button>
                  </div>
                  <p className="text-2 text-sm">
                    A window overrides the base rate during those hours; outside every slot, the base
                    rate (<Money coins={selectedTariff.price_per_kwh} rate={config.coin_inr_rate} />
                    /kWh) applies.
                  </p>

                  <form className="pricing-slot-form" onSubmit={submitSlot}>
                    <div className="field">
                      <label className="field-label" htmlFor="slot-start">
                        From
                      </label>
                      <input
                        id="slot-start"
                        type="time"
                        className="input"
                        value={slotStart}
                        onChange={(e) => setSlotStart(e.target.value)}
                        required
                      />
                    </div>
                    <div className="field">
                      <label className="field-label" htmlFor="slot-end">
                        To
                      </label>
                      <input
                        id="slot-end"
                        type="time"
                        className="input"
                        value={slotEnd}
                        onChange={(e) => setSlotEnd(e.target.value)}
                        required
                      />
                    </div>
                    <div className="field">
                      <label className="field-label" htmlFor="slot-price">
                        Rate (coins/kWh)
                      </label>
                      <input
                        id="slot-price"
                        type="number"
                        step="0.01"
                        min="0.01"
                        max="1000"
                        className="input"
                        value={slotPrice}
                        onChange={(e) => setSlotPrice(e.target.value)}
                        required
                      />
                    </div>
                    <div className="field">
                      <span className="field-label">Days</span>
                      <div className="row" role="group" aria-label="Days of week">
                        {WEEKDAYS.map(([label, bit]) => {
                          const on = Boolean((slotDays >> bit) & 1);
                          return (
                            <button
                              key={label}
                              type="button"
                              className="chip pricing-day-chip"
                              aria-pressed={on}
                              onClick={() => setSlotDays((d) => d ^ (1 << bit))}
                            >
                              {label}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                    {slotFormError && (
                      <p className="field-error" role="alert">
                        {slotFormError}
                      </p>
                    )}
                    <button type="submit" className="btn btn-primary btn-sm" disabled={slotBusy}>
                      {slotBusy ? 'Adding…' : 'Add slot'}
                    </button>
                  </form>

                  <DataTable
                    columns={slotColumns}
                    rows={slots}
                    loading={slotsLoading}
                    error={slotsError}
                    onRetry={() => loadSlots(selectedTariffId)}
                    emptyTitle="No time-of-day slots"
                    emptyBody="This tariff charges its base rate around the clock."
                  />

                  {!slotsLoading && !slotsError && (
                    <>
                      <div className="pricing-grid-wrap">
                        <div
                          className="pricing-grid"
                          role="table"
                          aria-label={`Weekly price coverage for ${selectedTariff.name}`}
                        >
                          <div className="pricing-grid-row pricing-grid-header" role="row">
                            <div className="pricing-grid-corner" role="columnheader" aria-hidden="true" />
                            {Array.from({ length: 24 }, (_, h) => (
                              <div key={h} className="pricing-grid-hour" role="columnheader">
                                {h % 3 === 0 ? h : ''}
                              </div>
                            ))}
                          </div>
                          {WEEKDAYS.map(([label], dayIdx) => (
                            <div key={label} className="pricing-grid-row" role="row">
                              <div className="pricing-grid-daylabel" role="rowheader">
                                {label}
                              </div>
                              {coverage.byDay[dayIdx].map((cell, h) => (
                                <div
                                  key={h}
                                  className={`pricing-grid-cell${
                                    cell ? ` pricing-grid-cell--${cell.tint}` : ' pricing-grid-cell--base'
                                  }`}
                                  role="cell"
                                  title={
                                    cell
                                      ? `${label} ${fmtMinutes(h * 60)}–${fmtMinutes((h + 1) * 60)} · ${cell.slot.price_per_kwh}/kWh`
                                      : `${label} ${fmtMinutes(h * 60)}–${fmtMinutes((h + 1) * 60)} · base ${selectedTariff.price_per_kwh}/kWh`
                                  }
                                />
                              ))}
                            </div>
                          ))}
                        </div>
                      </div>

                      <div className="pricing-legend">
                        <span className="pricing-legend-item">
                          <span className="pricing-legend-swatch pricing-grid-cell--base" />
                          Base rate — {selectedTariff.price_per_kwh}/kWh
                        </span>
                        {coverage.sorted.map((slot, idx) => (
                          <span key={slot.id} className="pricing-legend-item">
                            <span
                              className={`pricing-legend-swatch pricing-grid-cell--${CELL_TINTS[idx % CELL_TINTS.length]}`}
                            />
                            {daysLabel(slot.days_mask)} {fmtMinutes(slot.start_min)}
                            –{fmtMinutes(slot.end_min)} — {slot.price_per_kwh}/kWh
                          </span>
                        ))}
                      </div>
                    </>
                  )}
                </section>
              )}

              <section className="stack pricing-section">
                <h2>Assignment</h2>

                <div className="field pricing-default-field">
                  <label className="field-label" htmlFor="tenant-default-tariff">
                    Tenant default
                  </label>
                  <select
                    id="tenant-default-tariff"
                    className="select"
                    value={defaultTariffId ?? ''}
                    onChange={(e) =>
                      assignTenantDefault(e.target.value ? Number(e.target.value) : null)
                    }
                  >
                    <option value="">No default (falls back to the base rate)</option>
                    {tariffs.map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.name}
                      </option>
                    ))}
                  </select>
                  <p className="field-help">
                    Used when a charger and its group have no tariff of their own.
                  </p>
                </div>

                <DataTable
                  columns={assignmentColumns}
                  rows={assignmentRows}
                  keyField="key"
                  emptyTitle="No groups or chargers yet"
                  emptyBody="Create a group or register a charger first."
                  collapse
                />
              </section>
            </>
          )}
        </>
      )}

      <Modal
        open={formOpen}
        onClose={() => {
          if (!formBusy) setFormOpen(false);
        }}
        title={editingTariff ? 'Edit tariff' : 'New tariff'}
      >
        <form onSubmit={submitTariffForm} className="stack">
          <div className="field">
            <label className="field-label" htmlFor="tariff-name">
              Name
            </label>
            <input
              id="tariff-name"
              className="input"
              value={formName}
              onChange={(e) => setFormName(e.target.value)}
              maxLength={100}
              required
              autoFocus
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="tariff-price">
              Base rate (coins/kWh)
            </label>
            <input
              id="tariff-price"
              className="input"
              type="number"
              step="0.01"
              min="0.01"
              max="1000"
              value={formPrice}
              onChange={(e) => setFormPrice(e.target.value)}
              required
            />
            <p className="field-help">Applies whenever no time-of-day slot below covers the hour.</p>
          </div>
          {formError && (
            <p className="field-error" role="alert">
              {formError}
            </p>
          )}
          <div className="modal-actions">
            <button
              type="button"
              className="btn btn-quiet"
              onClick={() => setFormOpen(false)}
              disabled={formBusy}
            >
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={formBusy}>
              {formBusy ? 'Saving…' : editingTariff ? 'Save changes' : 'Create tariff'}
            </button>
          </div>
        </form>
      </Modal>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onClose={() => {
          if (!deleteBusy) setDeleteTarget(null);
        }}
        onConfirm={confirmDeleteTariff}
        title={`Delete "${deleteTarget?.name}"?`}
        body={
          deleteTarget && assignedCounts[deleteTarget.id] > 0
            ? `${assignedCounts[deleteTarget.id]} group, charger or the tenant default currently use this tariff. They'll fall back to the next pricing rule — group, then tenant default, then the base rate.`
            : 'Any plug or group using it falls back to the next pricing rule (group → tenant default → base rate).'
        }
        confirmLabel="Delete tariff"
        tone="danger"
        busy={deleteBusy}
      />

      <ConfirmDialog
        open={Boolean(slotDeleteTarget)}
        onClose={() => {
          if (!slotDeleteBusy) setSlotDeleteTarget(null);
        }}
        onConfirm={confirmDeleteSlot}
        title="Remove this slot?"
        body={
          slotDeleteTarget
            ? `${fmtMinutes(slotDeleteTarget.start_min)}–${fmtMinutes(slotDeleteTarget.end_min)} will bill at the base rate again.`
            : ''
        }
        confirmLabel="Remove slot"
        tone="danger"
        busy={slotDeleteBusy}
      />
    </CpoLayout>
  );
};

export default CpoPricing;
