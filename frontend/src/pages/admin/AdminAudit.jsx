/**
 * AdminAudit — cross-tenant audit log table.
 * Columns: time, organization, actor (email), action, detail.
 * Filter by tenant_id.
 *
 * Data: GET /api/admin/audit?tenant_id=&limit=&offset= (contract §4).
 */

import { useCallback, useEffect, useState } from 'react';
import { FileText } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable from '../../components/ui/DataTable';
import api from '../../api/client';

const LIMIT = 20;

export default function AdminAudit() {
  const [tenantFilter, setTenantFilter] = useState('');
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tenants, setTenants] = useState([]);

  const fetchTenants = useCallback(async () => {
    try {
      const res = await api.get('/api/admin/tenants?limit=1000');
      setTenants(res?.items || []);
    } catch (err) {
      // Silently fail for tenant list; still show audit log
      console.error('Failed to fetch tenants:', err);
    }
  }, []);

  const fetchAudit = useCallback(
    async (pageOffset) => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({ limit: String(LIMIT), offset: String(pageOffset) });
        if (tenantFilter) params.set('tenant_id', tenantFilter);
        const res = await api.get(`/api/admin/audit?${params.toString()}`);
        setItems(res?.items || []);
        setTotal(res?.total || 0);
      } catch (err) {
        setError(err);
      } finally {
        setLoading(false);
      }
    },
    [tenantFilter],
  );

  useEffect(() => {
    fetchTenants();
  }, [fetchTenants]);

  useEffect(() => {
    setOffset(0);
    fetchAudit(0);
  }, [tenantFilter, fetchAudit]);

  useEffect(() => {
    fetchAudit(offset);
  }, [offset, fetchAudit]);

  const retry = () => fetchAudit(offset);

  const columns = [
    {
      key: 'created_at',
      label: 'Time',
      render: (row) => {
        const date = new Date(row.created_at);
        return date.toLocaleString('en-IN', {
          year: 'numeric',
          month: 'short',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
        });
      },
    },
    { key: 'tenant_name', label: 'Organization' },
    { key: 'actor_email', label: 'Actor' },
    { key: 'action', label: 'Action' },
    { key: 'detail', label: 'Detail' },
  ];

  return (
    <>
      <PageHeader
        title="Audit log"
        sub="Every platform action across all organizations — who did what, when."
      />

      <div className="filter-bar">
        <label htmlFor="audit-tenant-filter" className="sr-only">
          Filter by organization
        </label>
        <select
          id="audit-tenant-filter"
          className="input"
          value={tenantFilter}
          onChange={(e) => setTenantFilter(e.target.value)}
        >
          <option value="">All organizations</option>
          {tenants.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
        <span className="filter-count" aria-live="polite">
          {total} audit log{total === 1 ? '' : 's'}
        </span>
      </div>

      <DataTable
        columns={columns}
        rows={items}
        loading={loading}
        error={error}
        onRetry={retry}
        emptyIcon={FileText}
        emptyTitle="No audit logs yet"
        emptyBody="Administrative actions show up here."
        pagination={{ total, offset, limit: LIMIT, onPage: setOffset }}
        collapse
      />
    </>
  );
}
