/**
 * AdminPlugs — every charger on the platform, across every tenant. The
 * cross-tenant twin of the operator's CpoChargers view: searchable by name,
 * filterable by public/private visibility and effective status, with the
 * owning tenant + gateway, live power draw, advertised model/connector, and
 * last-telemetry recency surfaced for support triage.
 *
 * Read-only by design — mutations (maintenance, pricing, group moves) stay in
 * the operator console; this is the admin's fleet-wide lookup surface.
 *
 * Data: GET /api/admin/plugs?q=&visibility=&status=&limit=&offset= →
 * { total, items }. Search is debounced (400ms) like AdminUsers; the
 * visibility and status filters apply immediately, both resetting pagination
 * to page one. Polled every 30s so power/telemetry stay fresh.
 */

import { useCallback, useEffect, useState } from 'react';
import { PlugZap, Search } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable from '../../components/ui/DataTable';
import StatusDot from '../../components/ui/StatusDot';
import api from '../../api/client';
import usePoll from '../../hooks/usePoll';
import { formatKw } from '../../utils/money';
import { getPlugAvailability } from '../../utils/plugAvailability';
import { plugStateLabel } from '../../utils/statusCopy';
import './AdminPlugs.css';

const LIMIT = 25;
const POLL_MS = 30_000;
const SEARCH_DEBOUNCE_MS = 400;

// Raw PlugStatus values the backend filters on (distinct from the derived
// availability buckets used for the coloured StatusDot).
const STATUS_OPTIONS = [
  { value: 'available', label: 'Available' },
  { value: 'occupied', label: 'In use' },
  { value: 'offline', label: 'Offline' },
  { value: 'maintenance', label: 'Maintenance' },
];

const relativeTime = (iso) => {
  if (!iso) return 'never';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 0) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
};

export default function AdminPlugs() {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [searchInput, setSearchInput] = useState('');
  const [q, setQ] = useState('');
  const [visibility, setVisibility] = useState(''); // '' | 'public' | 'private'
  const [status, setStatus] = useState(''); // '' | available | occupied | offline | maintenance

  const hasFilters = Boolean(q || visibility || status);

  // Debounce the free-text search box; the selects apply immediately.
  useEffect(() => {
    const t = setTimeout(() => {
      setQ(searchInput.trim());
      setOffset(0);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [searchInput]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
      if (q) params.set('q', q);
      if (visibility) params.set('visibility', visibility);
      if (status) params.set('status', status);
      const data = await api.get(`/api/admin/plugs?${params.toString()}`);
      setItems(data?.items || []);
      setTotal(data?.total || 0);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [q, visibility, status, offset]);

  usePoll(load, POLL_MS, [q, visibility, status, offset]);

  const columns = [
    { key: 'name', label: 'Name', render: (row) => row.name || '—' },
    { key: 'tenant_name', label: 'Tenant', render: (row) => row.tenant_name || '—' },
    { key: 'gateway_name', label: 'Gateway', render: (row) => row.gateway_name || row.gateway_id || '—' },
    {
      key: 'visibility',
      label: 'Visibility',
      render: (row) =>
        row.visibility === 'public' ? (
          <span className="badge badge-ok">Public</span>
        ) : (
          <span className="badge badge-info">Private</span>
        ),
    },
    {
      key: 'status',
      label: 'Status',
      render: (row) => {
        const state = getPlugAvailability(row);
        return <StatusDot state={state} label={plugStateLabel(state)} live={state === 'in_use'} />;
      },
    },
    {
      key: 'current_power_w',
      label: 'Power',
      num: true,
      render: (row) => <span className="num">{formatKw(row.current_power_w)}</span>,
    },
    {
      key: 'model',
      label: 'Model / Connector',
      render: (row) => (
        <div>
          <div>{row.plug_model || '—'}</div>
          {row.connector_type && <div className="text-3 text-sm">{row.connector_type}</div>}
        </div>
      ),
    },
    {
      key: 'last_telemetry_at',
      label: 'Last telemetry',
      render: (row) => (
        <span className="text-2 text-sm" title={row.last_telemetry_at || 'never'}>
          {relativeTime(row.last_telemetry_at)}
        </span>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        eyebrow="Platform"
        title="Chargers"
        sub="Every charger on AmpHive, across every organization."
      />

      <div className="filter-bar">
        <div className="field admin-plugs-search">
          <label className="sr-only" htmlFor="admin-plugs-search">
            Search chargers
          </label>
          <div className="admin-plugs-search-row">
            <Search size={16} aria-hidden="true" />
            <input
              id="admin-plugs-search"
              className="input"
              type="search"
              placeholder="Search by name"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
            />
          </div>
        </div>
        <select
          className="select"
          aria-label="Filter by visibility"
          value={visibility}
          onChange={(e) => {
            setVisibility(e.target.value);
            setOffset(0);
          }}
        >
          <option value="">All visibility</option>
          <option value="public">Public</option>
          <option value="private">Private</option>
        </select>
        <select
          className="select"
          aria-label="Filter by status"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value);
            setOffset(0);
          }}
        >
          <option value="">All statuses</option>
          {STATUS_OPTIONS.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
        <span className="filter-count">
          {total} charger{total !== 1 ? 's' : ''}
        </span>
      </div>

      <DataTable
        columns={columns}
        rows={items}
        loading={loading}
        error={error}
        onRetry={load}
        emptyIcon={PlugZap}
        emptyTitle={hasFilters ? 'No chargers match your filters' : 'No chargers yet'}
        emptyBody={
          hasFilters
            ? 'Try a different search or filter.'
            : 'Chargers will appear here once operators register them.'
        }
        pagination={{ total, offset, limit: LIMIT, onPage: setOffset }}
        collapse
      />
    </>
  );
}
