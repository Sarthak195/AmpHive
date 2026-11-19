/**
 * [Promo codes] CPO promos card, rendered on the Earnings page under the
 * offline top-ups. Issue a code (POST /api/cpo/promos), list codes with their
 * redemption counts (GET /api/cpo/promos, paginated {total, items}) and
 * disable one (POST /api/cpo/promos/{id}/disable).
 *
 * The plain code comes back only in the create response (the backend keeps a
 * hash), so it is shown once here with a copy button. Each redemption is an
 * offline top-up drawn from the same pool, checked when the driver redeems,
 * not when the code is issued.
 */
import { useCallback, useEffect, useState } from 'react';
import { Ticket } from 'lucide-react';
import Modal from './ui/Modal';
import { DataTable, Money, useToast } from './ui';
import api from '../api/client';
import { apiErrorCopy } from '../utils/statusCopy';

const PAGE_SIZE = 10;
const EMPTY_FORM = { amount_coins: '', max_redemptions: '1', per_user_limit: '1', expires_in_days: '30', note: '' };

const STATUS_LABEL = {
  active: 'Active',
  used_up: 'Used up',
  expired: 'Expired',
  disabled: 'Disabled',
};

const formatDate = (iso) => (iso ? new Date(iso).toLocaleDateString() : 'Never');

export default function CpoPromosCard({ rate = 1 }) {
  const toast = useToast();
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [formError, setFormError] = useState('');
  const [busy, setBusy] = useState(false);
  const [issued, setIssued] = useState(null); // create response, carries the one-time code
  const [disablingId, setDisablingId] = useState(null);

  const fetchPromos = useCallback(async (nextOffset = 0) => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.get(`/api/cpo/promos?limit=${PAGE_SIZE}&offset=${nextOffset}`);
      setItems(data?.items || []);
      setTotal(data?.total || 0);
      setOffset(nextOffset);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchPromos(0);
  }, [fetchPromos]);

  const setField = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const openForm = () => {
    setForm(EMPTY_FORM);
    setFormError('');
    setOpen(true);
  };

  const submit = async (e) => {
    e.preventDefault();
    const amount = Number(form.amount_coins);
    const maxRedemptions = Number(form.max_redemptions);
    const perUser = Number(form.per_user_limit);
    if (!amount || amount <= 0) {
      setFormError('Enter an amount greater than 0.');
      return;
    }
    if (!Number.isInteger(maxRedemptions) || maxRedemptions < 1 || !Number.isInteger(perUser) || perUser < 1) {
      setFormError('Redemption limits must be whole numbers of at least 1.');
      return;
    }
    setFormError('');
    setBusy(true);
    try {
      const res = await api.post('/api/cpo/promos', {
        amount_coins: amount,
        max_redemptions: maxRedemptions,
        per_user_limit: perUser,
        expires_in_days: form.expires_in_days === '' ? null : Number(form.expires_in_days),
        note: form.note.trim() || undefined,
      });
      setOpen(false);
      setIssued(res);
      await fetchPromos(0);
    } catch (err) {
      setFormError(apiErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  const copyCode = async () => {
    try {
      await navigator.clipboard.writeText(issued.code);
      toast.ok('Code copied.');
    } catch {
      toast.error('Copy failed. Select the code and copy it by hand.');
    }
  };

  const disable = async (promo) => {
    setDisablingId(promo.id);
    try {
      await api.post(`/api/cpo/promos/${promo.id}/disable`, {});
      toast.ok('Code disabled.');
      await fetchPromos(offset);
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setDisablingId(null);
    }
  };

  const columns = [
    {
      key: 'code_hint',
      label: 'Code',
      // The read-only demo viewer gets code_hint: null from the API.
      render: (p) =>
        p.code_hint ? <span className="mono">…{p.code_hint}</span> : <span className="text-3">Hidden</span>,
    },
    { key: 'amount_coins', label: 'Amount', num: true, render: (p) => <Money coins={p.amount_coins} rate={rate} showCoins /> },
    { key: 'used', label: 'Redeemed', num: true, render: (p) => `${p.redemption_count} / ${p.max_redemptions}` },
    { key: 'expires_at', label: 'Expires', render: (p) => formatDate(p.expires_at) },
    {
      key: 'status',
      label: 'Status',
      render: (p) => (
        <span className={p.status === 'active' ? 'badge badge-ok' : 'badge'}>{STATUS_LABEL[p.status] || p.status}</span>
      ),
    },
    { key: 'note', label: 'Note', render: (p) => p.note || <span className="text-3">-</span> },
    {
      key: 'actions',
      label: '',
      render: (p) =>
        p.status === 'disabled' ? null : (
          <button
            data-write
            type="button"
            className="btn btn-quiet btn-sm"
            onClick={() => disable(p)}
            disabled={disablingId === p.id}
            aria-label={p.code_hint ? `Disable code ending ${p.code_hint}` : 'Disable code'}
          >
            Disable
          </button>
        ),
    },
  ];

  return (
    <>
      <div className="card earnings-topup-card">
        <div className="earnings-topup-header">
          <div>
            <h2 className="earnings-card-title">Promo codes</h2>
            <p className="text-3 text-sm">
              Codes drivers redeem for credit. Each redemption comes out of the same pool as a
              cash top-up, and fails if the pool can't cover it at that moment.
            </p>
          </div>
          <button data-write type="button" className="btn btn-primary" onClick={openForm}>
            <Ticket size={16} aria-hidden="true" />
            New code
          </button>
        </div>

        {issued?.code && (
          <div className="banner banner-ok" role="status">
            <div className="stack-sm">
              <p>
                New code for <Money coins={issued.amount_coins} rate={rate} showCoins />:{' '}
                <strong className="mono">{issued.code}</strong>
              </p>
              <p className="text-sm">Copy it now. It is stored hashed and can't be shown again.</p>
              <div>
                <button type="button" className="btn btn-quiet btn-sm" onClick={copyCode}>
                  Copy code
                </button>{' '}
                <button type="button" className="btn btn-quiet btn-sm" onClick={() => setIssued(null)}>
                  Done
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      <DataTable
        columns={columns}
        rows={items}
        loading={loading}
        error={error}
        onRetry={() => fetchPromos(offset)}
        emptyIcon={Ticket}
        emptyTitle="No promo codes yet"
        emptyBody="Issue a code to give drivers charging credit from your own unsettled earnings."
        pagination={{ total, offset, limit: PAGE_SIZE, onPage: fetchPromos }}
        collapse
      />

      <Modal open={open} onClose={() => !busy && setOpen(false)} title="New promo code">
        <form className="stack" onSubmit={submit} noValidate>
          <div className="field">
            <label className="field-label" htmlFor="promo-amount">Amount per redemption (coins)</label>
            <input id="promo-amount" type="number" min="0" step="0.01" className="input"
              value={form.amount_coins} onChange={setField('amount_coins')} placeholder="0.00" />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="promo-max">Total redemptions</label>
            <input id="promo-max" type="number" min="1" step="1" className="input"
              value={form.max_redemptions} onChange={setField('max_redemptions')} />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="promo-per-user">Redemptions per driver</label>
            <input id="promo-per-user" type="number" min="1" step="1" className="input"
              value={form.per_user_limit} onChange={setField('per_user_limit')} />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="promo-expiry">Expires after (days)</label>
            <input id="promo-expiry" type="number" min="1" max="365" step="1" className="input"
              value={form.expires_in_days} onChange={setField('expires_in_days')} placeholder="Never" />
            <p className="field-help">Leave blank for a code that never expires.</p>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="promo-note">Note (optional)</label>
            <input id="promo-note" className="input" maxLength={200}
              value={form.note} onChange={setField('note')} placeholder="e.g. launch flyer" />
          </div>
          {formError && <p className="field-error" role="alert">{formError}</p>}
          <div className="modal-actions">
            <button type="button" className="btn btn-quiet" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? 'Creating…' : 'Create code'}
            </button>
          </div>
        </form>
      </Modal>
    </>
  );
}
