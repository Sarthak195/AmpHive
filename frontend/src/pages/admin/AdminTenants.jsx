/**
 * AdminTenants — cross-tenant roster of every organization on the platform:
 * search + fleet/usage aggregates (users, gateways online/total, chargers,
 * 30d sessions, 30d revenue, pending payouts). Rows link to the tenant
 * detail page (identity + GST, KPI row, recent sessions, payouts).
 *
 * Data: GET /api/admin/tenants?q=&limit=&offset= (contract §4) — one round
 * trip serves the whole page's per-tenant aggregates (no N+1).
 */

import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Building2 } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable from '../../components/ui/DataTable';
import Money from '../../components/ui/Money';
import api from '../../api/client';
import { useConfig } from '../../contexts/ConfigContext';

const LIMIT = 20;
const SEARCH_DEBOUNCE_MS = 300;

export default function AdminTenants() {
  const navigate = useNavigate();
  const { coin_inr_rate: rate = 1 } = useConfig();

  const [searchInput, setSearchInput] = useState('');
  const [q, setQ] = useState('');
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchTenants = useCallback(async (searchTerm, pageOffset) => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ limit: String(LIMIT), offset: String(pageOffset) });
      if (searchTerm) params.set('q', searchTerm);
      const res = await api.get(`/api/admin/tenants?${params.toString()}`);
      setItems(res?.items || []);
      setTotal(res?.total || 0);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTenants(q, offset);
  }, [fetchTenants, q, offset]);

  // Debounce the search box — one request per pause in typing, not per key.
  useEffect(() => {
    const id = setTimeout(() => {
      setOffset(0);
      setQ(searchInput.trim());
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [searchInput]);

  const retry = () => fetchTenants(q, offset);

  const columns = [
    { key: 'name', label: 'Organization' },
    { key: 'user_count', label: 'Users', num: true },
    {
      key: 'gateways_online',
      label: 'Gateways',
      num: true,
      render: (row) => `${row.gateways_online}/${row.gateway_count} online`,
    },
    { key: 'plug_count', label: 'Chargers', num: true },
    { key: 'sessions_30d', label: 'Sessions (30d)', num: true },
    {
      key: 'revenue_30d_coins',
      label: 'Revenue (30d)',
      num: true,
      render: (row) => <Money coins={row.revenue_30d_coins} rate={rate} />,
    },
    {
      key: 'pending_payouts',
      label: 'Payouts',
      render: (row) =>
        row.pending_payouts > 0 ? (
          <span className="badge badge-warn">{row.pending_payouts} pending</span>
        ) : (
          <span className="text-3">—</span>
        ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Tenants"
        sub="Every organization on the platform, with fleet and usage at a glance."
      />

      <div className="filter-bar">
        <label className="sr-only" htmlFor="tenant-search">
          Search organizations
        </label>
        <input
          id="tenant-search"
          className="input"
          type="search"
          placeholder="Search organizations…"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
        />
        <span className="filter-count" aria-live="polite">
          {total} organization{total === 1 ? '' : 's'}
        </span>
      </div>

      <DataTable
        columns={columns}
        rows={items}
        loading={loading}
        error={error}
        onRetry={retry}
        emptyIcon={Building2}
        emptyTitle={q ? 'No matching organizations' : 'No organizations yet'}
        emptyBody={q ? 'Try a different search.' : 'Tenants show up here once they sign up.'}
        onRowClick={(row) => navigate(`/admin/tenants/${row.id}`)}
        pagination={{ total, offset, limit: LIMIT, onPage: setOffset }}
        collapse
      />
    </>
  );
}
