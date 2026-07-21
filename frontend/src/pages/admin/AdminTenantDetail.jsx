/**
 * AdminTenantDetail — one organization: identity + GST card, KPI row, its 10
 * most recent sessions, and its payout history.
 *
 * Data: GET /api/admin/tenants/{id} (contract §4) — the tenant list row's
 * aggregates plus gst_number/legal_name/default_tariff_id, recent_sessions
 * (last 10) and payouts (last 50).
 */

import { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ArrowLeft, Banknote, Receipt } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable from '../../components/ui/DataTable';
import ErrorState from '../../components/ui/ErrorState';
import { SkeletonTitle } from '../../components/ui/Skeleton';
import Money from '../../components/ui/Money';
import api from '../../api/client';
import { useConfig } from '../../contexts/ConfigContext';
import { sessionStatusLabel } from '../../utils/statusCopy';
import { formatKwh } from '../../utils/money';
import './AdminTenantDetail.css';

const formatDate = (iso) => (iso ? new Date(iso).toLocaleDateString() : '—');
const formatDateTime = (iso) => (iso ? new Date(iso).toLocaleString() : '—');

const SESSION_BADGE = {
  active: 'badge-info',
  completed: 'badge-ok',
  paid: 'badge-ok',
  billed: 'badge-ok',
  cancelled: 'badge',
  orphaned: 'badge-danger',
};

const PAYOUT_BADGE = {
  requested: 'badge-warn',
  paid: 'badge-ok',
  cancelled: 'badge',
};

export default function AdminTenantDetail() {
  const { id } = useParams();
  const { coin_inr_rate: rate = 1 } = useConfig();

  const [tenant, setTenant] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchTenant = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get(`/api/admin/tenants/${id}`);
      setTenant(res);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    fetchTenant();
  }, [fetchTenant]);

  const backLink = (
    <div className="admin-tenant-back">
      <Link to="/admin/tenants" className="btn btn-quiet btn-sm">
        <ArrowLeft size={16} aria-hidden="true" />
        All tenants
      </Link>
    </div>
  );

  if (loading) {
    return (
      <>
        {backLink}
        <div className="stack">
          <SkeletonTitle />
          <div className="card">
            <SkeletonTitle />
          </div>
        </div>
      </>
    );
  }

  if (error || !tenant) {
    return (
      <>
        {backLink}
        <ErrorState error={error} onRetry={fetchTenant} title="Couldn't load this tenant" />
      </>
    );
  }

  const sessionColumns = [
    { key: 'plug_name', label: 'Charger' },
    { key: 'user_email', label: 'Driver' },
    { key: 'started_at', label: 'Started', render: (row) => formatDateTime(row.started_at) },
    {
      key: 'energy_kwh',
      label: 'Energy',
      num: true,
      render: (row) => formatKwh(row.energy_kwh),
    },
    {
      key: 'coins_spent',
      label: 'Cost',
      num: true,
      render: (row) => <Money coins={row.coins_spent} rate={rate} />,
    },
    {
      key: 'status',
      label: 'Status',
      render: (row) => (
        <span className={`badge ${SESSION_BADGE[row.status] || 'badge'}`}>
          {sessionStatusLabel(row.status)}
        </span>
      ),
    },
  ];

  const payoutColumns = [
    {
      key: 'period_start',
      label: 'Period',
      render: (row) => `${formatDate(row.period_start)} – ${formatDate(row.period_end)}`,
    },
    { key: 'gross_coins', label: 'Gross', num: true, render: (row) => <Money coins={row.gross_coins} rate={rate} /> },
    {
      key: 'platform_fee_coins',
      label: 'Fee',
      num: true,
      render: (row) => <Money coins={row.platform_fee_coins} rate={rate} />,
    },
    { key: 'net_coins', label: 'Net', num: true, render: (row) => <Money coins={row.net_coins} rate={rate} /> },
    {
      key: 'status',
      label: 'Status',
      render: (row) => <span className={`badge ${PAYOUT_BADGE[row.status] || 'badge'}`}>{row.status}</span>,
    },
    { key: 'requested_at', label: 'Requested', render: (row) => formatDate(row.requested_at) },
  ];

  return (
    <>
      {backLink}
      <PageHeader
        title={tenant.name}
        sub={`Tenant since ${formatDate(tenant.created_at)}`}
      />

      <div className="stack">
        <section className="card">
          <h2>Identity &amp; GST</h2>
          <div className="kpi-grid">
            <div>
              <p className="kpi-label">Legal name</p>
              <p>{tenant.legal_name || '—'}</p>
            </div>
            <div>
              <p className="kpi-label">GSTIN</p>
              <p className="mono">{tenant.gst_number || 'Not provided'}</p>
            </div>
            <div>
              <p className="kpi-label">Default tariff</p>
              <p>{tenant.default_tariff_id != null ? `#${tenant.default_tariff_id}` : 'None'}</p>
            </div>
          </div>
        </section>

        <div className="kpi-grid">
          <div className="kpi">
            <p className="kpi-label">Users</p>
            <p className="kpi-value">{tenant.user_count}</p>
          </div>
          <div className="kpi">
            <p className="kpi-label">Gateways</p>
            <p className="kpi-value">{tenant.gateways_online}/{tenant.gateway_count}</p>
            <p className="kpi-sub">online</p>
          </div>
          <div className="kpi">
            <p className="kpi-label">Chargers</p>
            <p className="kpi-value">{tenant.plug_count}</p>
          </div>
          <div className="kpi">
            <p className="kpi-label">Sessions (30d)</p>
            <p className="kpi-value">{tenant.sessions_30d}</p>
          </div>
          <div className="kpi">
            <p className="kpi-label">Revenue (30d)</p>
            <p className="kpi-value"><Money coins={tenant.revenue_30d_coins} rate={rate} /></p>
          </div>
          <div className="kpi">
            <p className="kpi-label">Pending payouts</p>
            <p className="kpi-value">{tenant.pending_payouts}</p>
          </div>
        </div>

        <section>
          <h2>Recent sessions</h2>
          <DataTable
            columns={sessionColumns}
            rows={tenant.recent_sessions || []}
            loading={false}
            emptyIcon={Receipt}
            emptyTitle="No sessions yet"
            emptyBody="This tenant's charging sessions will show up here."
            collapse
          />
        </section>

        <section>
          <h2>Payouts</h2>
          <DataTable
            columns={payoutColumns}
            rows={tenant.payouts || []}
            loading={false}
            emptyIcon={Banknote}
            emptyTitle="No payouts yet"
            emptyBody="Payout requests from this tenant will show up here."
            collapse
          />
        </section>
      </div>
    </>
  );
}
