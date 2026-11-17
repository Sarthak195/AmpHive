/**
 * CpoAudit — the CPO's own admin-action audit trail (TD#26): gateway/plug/
 * group create-delete, status changes, access-code regeneration and payout
 * requests, newest first.
 *
 * Data: GET /api/cpo/audit?limit=&offset= returns a bare array (no total), so
 * paging is "Load more" and a short page marks the end. The admin console has
 * its own cross-tenant view (AdminAudit); this one is tenant-scoped by the
 * backend.
 */

import { useCallback, useEffect, useState } from 'react';
import { ScrollText } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import { PageHeader, DataTable } from '../../components/ui';
import api from '../../api/client';

const PAGE_SIZE = 50;

const fmtTime = (iso) =>
  iso
    ? new Date(iso).toLocaleString('en-IN', {
        year: 'numeric',
        month: 'short',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      })
    : '';

export default function CpoAudit() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState(null);
  const [hasMore, setHasMore] = useState(false);

  const load = useCallback(async (offset) => {
    if (offset === 0) setLoading(true);
    else setLoadingMore(true);
    try {
      const res = await api.get(`/api/cpo/audit?limit=${PAGE_SIZE}&offset=${offset}`);
      const page = Array.isArray(res) ? res : res?.items || [];
      setRows((prev) => (offset === 0 ? page : [...prev, ...page]));
      setHasMore(page.length === PAGE_SIZE);
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, []);

  useEffect(() => {
    load(0);
  }, [load]);

  const columns = [
    { key: 'created_at', label: 'Time', render: (r) => fmtTime(r.created_at) },
    { key: 'actor_email', label: 'Actor', render: (r) => r.actor_email || 'system' },
    { key: 'action', label: 'Action', render: (r) => <span className="chip">{r.action}</span> },
    {
      key: 'target',
      label: 'Target',
      render: (r) => [r.target_type, r.target_id].filter(Boolean).join(' ') || '—',
    },
    { key: 'detail', label: 'Detail', render: (r) => r.detail || '—' },
  ];

  return (
    <CpoLayout>
      <PageHeader
        title="Audit log"
        sub="Who changed what on your account: gateways, chargers, groups and payouts."
      />

      <DataTable
        columns={columns}
        rows={rows}
        loading={loading}
        error={error}
        onRetry={() => load(0)}
        emptyIcon={ScrollText}
        emptyTitle="No audit entries yet"
        emptyBody="Changes to your gateways, chargers, groups and payouts show up here."
        collapse
      />

      {hasMore && !loading && !error && (
        <button
          type="button"
          className="btn btn-quiet btn-full"
          onClick={() => load(rows.length)}
          disabled={loadingMore}
        >
          {loadingMore ? 'Loading…' : 'Load more'}
        </button>
      )}
    </CpoLayout>
  );
}
