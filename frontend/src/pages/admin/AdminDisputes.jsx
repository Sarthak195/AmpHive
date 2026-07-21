/**
 * AdminDisputes — cross-tenant dispute read-only table.
 * Columns: organization, driver email, session cost (₹), status badge, age.
 * No resolve actions (the tenant's CPO resolves disputes on their side).
 *
 * Data: GET /api/admin/disputes?status=&limit=&offset= (contract §4) →
 * items: dispute + {tenant_name,user_email,session_cost_coins}.
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertCircle } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable from '../../components/ui/DataTable';
import Money from '../../components/ui/Money';
import { disputeStatusLabel } from '../../utils/statusCopy';
import api from '../../api/client';
import { useConfig } from '../../contexts/ConfigContext';

const LIMIT = 20;

export default function AdminDisputes() {
  const { coin_inr_rate: rate = 1 } = useConfig();

  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchDisputes = useCallback(async (pageOffset) => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ limit: String(LIMIT), offset: String(pageOffset) });
      const res = await api.get(`/api/admin/disputes?${params.toString()}`);
      setItems(res?.items || []);
      setTotal(res?.total || 0);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDisputes(offset);
  }, [fetchDisputes, offset]);

  const retry = () => fetchDisputes(offset);

  const getStatus = (status) => {
    const label = disputeStatusLabel(status);
    const classes = {
      open: 'badge-info',
      approved: 'badge-ok',
      rejected: 'badge-danger',
    };
    const cls = classes[status] || '';
    return <span className={`badge ${cls}`}>{label}</span>;
  };

  const columns = [
    { key: 'tenant_name', label: 'Organization' },
    { key: 'user_email', label: 'Driver' },
    {
      key: 'session_cost_coins',
      label: 'Session cost',
      num: true,
      render: (row) => <Money coins={row.session_cost_coins} rate={rate} />,
    },
    {
      key: 'status',
      label: 'Status',
      render: (row) => getStatus(row.status),
    },
    {
      key: 'created_at',
      label: 'Age',
      render: (row) => {
        const date = new Date(row.created_at);
        const now = new Date();
        const days = Math.floor((now - date) / (1000 * 60 * 60 * 24));
        if (days === 0) return 'Today';
        if (days === 1) return 'Yesterday';
        return `${days} days ago`;
      },
    },
  ];

  return (
    <>
      <PageHeader
        title="Disputes"
        sub="All reported issues across the platform. The CPO resolves their own disputes."
      />

      <div className="banner banner-info">
        <AlertCircle size={18} aria-hidden="true" />
        <div>
          <strong>Disputes are resolved by their CPO.</strong> Drivers report issues here; the
          operator approves refunds or rejects with an explanation.
        </div>
      </div>

      <DataTable
        columns={columns}
        rows={items}
        loading={loading}
        error={error}
        onRetry={retry}
        emptyIcon={AlertCircle}
        emptyTitle="No disputes yet"
        emptyBody="Reported issues show up here once drivers submit them."
        pagination={{ total, offset, limit: LIMIT, onPage: setOffset }}
        collapse
      />
    </>
  );
}
