/**
 * AdminPayouts — cross-tenant payout ledger. A CPO snapshots its unsettled
 * earnings into a REQUESTED payout (POST /api/cpo/payouts, owned by the CPO
 * pages); the platform operator settles it out-of-band (bank/UPI) and marks
 * it PAID here. No money moves in-app — see contract §0 honesty traps.
 *
 * Data: GET /api/admin/payouts?status=&limit=&offset= → { total, items }
 * (contract §4) — each item is the existing payout shape + tenant_name.
 *
 * Mutation: POST /api/cpo/payouts/{id}/mark_paid — there is no dedicated
 * admin mark-paid route, so this reuses the existing CPO one. Verified
 * against backend/routers/cpo.py (cpo_mark_payout_paid): it is declared
 * `require_role("admin")` and selects the payout by id with no tenant
 * filter, so a tenant-less admin can settle any tenant's payout cleanly.
 */

import { useCallback, useEffect, useState } from 'react';
import { Wallet } from 'lucide-react';
import { DataTable, PageHeader, Money, ConfirmDialog, useToast } from '../../components/ui';
import api from '../../api/client';
import { apiErrorCopy } from '../../utils/statusCopy';
import { useConfig } from '../../contexts/ConfigContext';

const LIMIT = 20;

const STATUS_TABS = [
  { id: 'requested', label: 'Requested' },
  { id: 'paid', label: 'Paid' },
  { id: 'cancelled', label: 'Cancelled' },
  { id: 'all', label: 'All' },
];

const STATUS_BADGE = {
  requested: 'badge-warn',
  paid: 'badge-ok',
  cancelled: 'badge-danger',
};

function formatDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-IN', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  });
}

export default function AdminPayouts() {
  const toast = useToast();
  const { coin_inr_rate: rate = 1 } = useConfig();

  const [status, setStatus] = useState('requested');
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [target, setTarget] = useState(null);
  const [marking, setMarking] = useState(false);

  const fetchPayouts = useCallback(async (statusArg, offsetArg) => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ limit: String(LIMIT), offset: String(offsetArg) });
      if (statusArg !== 'all') params.set('status', statusArg);
      const res = await api.get(`/api/admin/payouts?${params.toString()}`);
      setItems(res?.items || []);
      setTotal(res?.total || 0);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchPayouts(status, offset);
  }, [fetchPayouts, status, offset]);

  const retry = () => fetchPayouts(status, offset);

  const changeStatus = (id) => {
    setStatus(id);
    setOffset(0);
  };

  const markPaid = async () => {
    if (!target) return;
    setMarking(true);
    try {
      await api.post(`/api/cpo/payouts/${target.id}/mark_paid`, {});
      toast.ok(`Payout #${target.id} marked paid`);
      setTarget(null);
      await fetchPayouts(status, offset);
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setMarking(false);
    }
  };

  const columns = [
    { key: 'tenant_name', label: 'Organization' },
    {
      key: 'gross_coins',
      label: 'Gross',
      num: true,
      render: (row) => <Money coins={row.gross_coins} rate={rate} />,
    },
    {
      key: 'platform_fee_coins',
      label: 'Fee',
      num: true,
      render: (row) => <Money coins={row.platform_fee_coins} rate={rate} />,
    },
    {
      key: 'net_coins',
      label: 'Net',
      num: true,
      render: (row) => <Money coins={row.net_coins} rate={rate} />,
    },
    {
      key: 'status',
      label: 'Status',
      render: (row) => (
        <span className={`badge ${STATUS_BADGE[row.status] || ''}`.trim()}>{row.status}</span>
      ),
    },
    {
      key: 'requested_at',
      label: 'Requested',
      render: (row) => formatDate(row.requested_at),
    },
    {
      key: 'actions',
      label: 'Action',
      render: (row) =>
        row.status === 'requested' ? (
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => setTarget(row)}>
            Mark paid
          </button>
        ) : (
          <span className="text-3">—</span>
        ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Payouts"
        sub="Settlement requests across every organization. Transfers happen outside AmpHive — mark paid once you've sent the money."
      />

      <div className="seg" role="group" aria-label="Filter by payout status">
        {STATUS_TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`seg-item${status === t.id ? ' active' : ''}`}
            aria-selected={status === t.id}
            onClick={() => changeStatus(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <DataTable
        columns={columns}
        rows={items}
        loading={loading}
        error={error}
        onRetry={retry}
        emptyIcon={Wallet}
        emptyTitle="No payouts here"
        emptyBody="Payout requests from organizations will show up here."
        pagination={{ total, offset, limit: LIMIT, onPage: setOffset }}
        collapse
      />

      <ConfirmDialog
        open={Boolean(target)}
        onClose={() => setTarget(null)}
        onConfirm={markPaid}
        title={`Mark payout #${target?.id ?? ''} paid?`}
        body="Confirm the transfer happened outside AmpHive first — this only records that it's settled, it doesn't move any money in-app."
        confirmLabel="Mark paid"
        tone="primary"
        busy={marking}
      />
    </>
  );
}
