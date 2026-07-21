/**
 * AdminUsers — every account on the platform, searchable/filterable, with
 * three mutations: change role, disable/enable, and a signed manual wallet
 * adjustment. All three hit PATCH/POST /api/admin/users/{id}[...] and are
 * audited server-side.
 *
 * Self-protection mirrors the backend (which 403s a self-demote/self-disable
 * so a lone admin can't lock themselves out): the signed-in admin's own row
 * disables the role/disable buttons up front rather than round-tripping to
 * find out.
 *
 * Data: GET /api/admin/users?q=&role=&limit=&offset= → { total, items }.
 * Search is debounced (400ms) the same way Dashboard's charger lookup is;
 * both the search box and the role filter reset pagination to page one.
 */

import { useCallback, useEffect, useState } from 'react';
import { ShieldCheck, Ban, CheckCircle2, Wallet as WalletIcon, Search, Users as UsersIcon } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable from '../../components/ui/DataTable';
import ConfirmDialog from '../../components/ui/ConfirmDialog';
import Modal from '../../components/ui/Modal';
import Money from '../../components/ui/Money';
import { useToast } from '../../components/ui';
import api from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useConfig } from '../../contexts/ConfigContext';
import { formatINR, coinsToINR } from '../../utils/money';
import { roleLabel, apiErrorCopy } from '../../utils/statusCopy';
import './AdminUsers.css';

const LIMIT = 20;
const SEARCH_DEBOUNCE_MS = 400;
const ROLE_OPTIONS = ['driver', 'cpo', 'admin'];
const ROLE_BADGE = { driver: 'badge-info', cpo: 'badge-brand', admin: 'badge-warn' };

const formatDate = (iso) => (iso ? new Date(iso).toLocaleDateString() : '—');

export default function AdminUsers() {
  const { user: me } = useAuth();
  const { coin_inr_rate: rate = 1 } = useConfig();
  const toast = useToast();

  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [searchInput, setSearchInput] = useState('');
  const [q, setQ] = useState('');
  const [role, setRole] = useState('');

  const isSelf = (row) => Boolean(me) && row.id === me.id;

  // Debounce the free-text search box; role filter applies immediately.
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
      if (role) params.set('role', role);
      const data = await api.get(`/api/admin/users?${params.toString()}`);
      setItems(data.items || []);
      setTotal(data.total || 0);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [q, role, offset]);

  useEffect(() => {
    load();
  }, [load]);

  const handleRoleFilter = (value) => {
    setRole(value);
    setOffset(0);
  };

  /* ---- change role -------------------------------------------------------- */
  const [roleTarget, setRoleTarget] = useState(null); // { user, newRole }
  const [roleBusy, setRoleBusy] = useState(false);

  const openRoleModal = (row) => {
    const options = ROLE_OPTIONS.filter((r) => r !== row.role);
    setRoleTarget({ user: row, newRole: options[0] });
  };

  const confirmRoleChange = async () => {
    if (!roleTarget) return;
    setRoleBusy(true);
    try {
      const res = await api.patch(`/api/admin/users/${roleTarget.user.id}`, { role: roleTarget.newRole });
      setItems((rows) => rows.map((r) => (r.id === roleTarget.user.id ? { ...r, role: res.role } : r)));
      toast.ok(`${roleTarget.user.email} is now ${roleLabel(res.role)}.`);
      setRoleTarget(null);
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setRoleBusy(false);
    }
  };

  /* ---- disable / enable ----------------------------------------------------- */
  const [disableTarget, setDisableTarget] = useState(null); // user row
  const [disableBusy, setDisableBusy] = useState(false);

  const confirmDisableToggle = async () => {
    if (!disableTarget) return;
    const nextDisabled = !disableTarget.is_disabled;
    setDisableBusy(true);
    try {
      const res = await api.patch(`/api/admin/users/${disableTarget.id}`, { is_disabled: nextDisabled });
      setItems((rows) =>
        rows.map((r) => (r.id === disableTarget.id ? { ...r, is_disabled: res.is_disabled } : r))
      );
      toast.ok(
        res.is_disabled
          ? `${disableTarget.email}'s account is disabled.`
          : `${disableTarget.email}'s account is enabled.`
      );
      setDisableTarget(null);
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setDisableBusy(false);
    }
  };

  /* ---- adjust balance ------------------------------------------------------- */
  const [balanceTarget, setBalanceTarget] = useState(null); // user row
  const [balanceAmount, setBalanceAmount] = useState('');
  const [balanceReason, setBalanceReason] = useState('');
  const [balanceError, setBalanceError] = useState('');
  const [balanceBusy, setBalanceBusy] = useState(false);

  const openBalanceModal = (row) => {
    setBalanceTarget(row);
    setBalanceAmount('');
    setBalanceReason('');
    setBalanceError('');
  };

  const closeBalanceModal = () => {
    if (!balanceBusy) setBalanceTarget(null);
  };

  const submitBalance = async (e) => {
    e.preventDefault();
    const amountInr = Number(balanceAmount);
    if (balanceAmount === '' || Number.isNaN(amountInr) || amountInr === 0) {
      setBalanceError('Enter a non-zero ₹ amount.');
      return;
    }
    if (balanceReason.trim().length < 3) {
      setBalanceError('Add a short reason (at least 3 characters).');
      return;
    }
    setBalanceError('');
    setBalanceBusy(true);
    try {
      const amountCoins = amountInr / (rate || 1);
      const res = await api.post(`/api/admin/users/${balanceTarget.id}/adjust-balance`, {
        amount_coins: amountCoins,
        reason: balanceReason.trim(),
      });
      setItems((rows) =>
        rows.map((r) => (r.id === balanceTarget.id ? { ...r, coin_balance: res.new_balance } : r))
      );
      toast.ok(`Balance updated — new balance ${formatINR(coinsToINR(res.new_balance, rate))}.`);
      setBalanceTarget(null);
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setBalanceBusy(false);
    }
  };

  const columns = [
    {
      key: 'user',
      label: 'User',
      render: (row) => (
        <div>
          <div>{row.full_name || '—'}</div>
          <div className="text-3 text-sm">{row.email}</div>
        </div>
      ),
    },
    {
      key: 'role',
      label: 'Role',
      render: (row) => <span className={`badge ${ROLE_BADGE[row.role] || ''}`}>{roleLabel(row.role)}</span>,
    },
    { key: 'tenant_name', label: 'Organization', render: (row) => row.tenant_name || '—' },
    {
      key: 'coin_balance',
      label: 'Balance',
      num: true,
      render: (row) => <Money coins={row.coin_balance} rate={rate} />,
    },
    {
      key: 'is_disabled',
      label: 'Status',
      render: (row) =>
        row.is_disabled ? (
          <span className="badge badge-danger">Disabled</span>
        ) : (
          <span className="badge badge-ok">Active</span>
        ),
    },
    { key: 'created_at', label: 'Joined', render: (row) => formatDate(row.created_at) },
    {
      key: 'actions',
      label: 'Actions',
      render: (row) => (
        <div className="admin-users-actions">
          <button
            type="button"
            className="btn btn-quiet btn-sm"
            onClick={() => openRoleModal(row)}
            disabled={isSelf(row)}
            title={isSelf(row) ? "You can't change your own role" : undefined}
          >
            <ShieldCheck size={14} aria-hidden="true" />
            Change role
          </button>
          <button
            type="button"
            className={`btn btn-sm ${row.is_disabled ? 'btn-quiet' : 'btn-danger'}`}
            onClick={() => setDisableTarget(row)}
            disabled={isSelf(row)}
            title={isSelf(row) ? "You can't disable your own account" : undefined}
          >
            {row.is_disabled ? (
              <>
                <CheckCircle2 size={14} aria-hidden="true" />
                Enable
              </>
            ) : (
              <>
                <Ban size={14} aria-hidden="true" />
                Disable
              </>
            )}
          </button>
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => openBalanceModal(row)}>
            <WalletIcon size={14} aria-hidden="true" />
            Adjust balance
          </button>
        </div>
      ),
    },
  ];

  const roleOptions = roleTarget ? ROLE_OPTIONS.filter((r) => r !== roleTarget.user.role) : [];

  return (
    <>
      <PageHeader eyebrow="Platform" title="Users" sub="Every account on AmpHive, across every organization." />

      <div className="filter-bar">
        <div className="field admin-users-search">
          <label className="sr-only" htmlFor="admin-users-search">
            Search users
          </label>
          <div className="admin-users-search-row">
            <Search size={16} aria-hidden="true" />
            <input
              id="admin-users-search"
              className="input"
              type="search"
              placeholder="Search by name or email"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
            />
          </div>
        </div>
        <select
          className="select"
          aria-label="Filter by role"
          value={role}
          onChange={(e) => handleRoleFilter(e.target.value)}
        >
          <option value="">All roles</option>
          {ROLE_OPTIONS.map((r) => (
            <option key={r} value={r}>
              {roleLabel(r)}
            </option>
          ))}
        </select>
        <span className="filter-count">{total} users</span>
      </div>

      <DataTable
        columns={columns}
        rows={items}
        loading={loading}
        error={error}
        onRetry={load}
        emptyIcon={UsersIcon}
        emptyTitle="No users found"
        emptyBody="Try a different search or role filter."
        pagination={{ total, offset, limit: LIMIT, onPage: setOffset }}
        collapse
      />

      <ConfirmDialog
        open={Boolean(roleTarget)}
        onClose={() => !roleBusy && setRoleTarget(null)}
        onConfirm={confirmRoleChange}
        title="Change role"
        confirmLabel="Change role"
        tone="primary"
        busy={roleBusy}
        body={
          roleTarget && (
            <div className="admin-users-role-body">
              <p className="text-2">
                Change <strong>{roleTarget.user.email}</strong>&rsquo;s role from{' '}
                {roleLabel(roleTarget.user.role)} to:
              </p>
              <select
                className="select"
                aria-label="New role"
                value={roleTarget.newRole}
                onChange={(e) => setRoleTarget((t) => ({ ...t, newRole: e.target.value }))}
              >
                {roleOptions.map((r) => (
                  <option key={r} value={r}>
                    {roleLabel(r)}
                  </option>
                ))}
              </select>
              <p className="text-3 text-sm">They&rsquo;ll be signed out everywhere and need to sign in again.</p>
              {roleTarget.newRole === 'cpo' && roleTarget.user.role === 'driver' && (
                <div className="banner banner-warn">
                  <p>Driver → CPO requires an organization — this account won&rsquo;t see a console until one is assigned.</p>
                </div>
              )}
            </div>
          )
        }
      />

      <ConfirmDialog
        open={Boolean(disableTarget)}
        onClose={() => !disableBusy && setDisableTarget(null)}
        onConfirm={confirmDisableToggle}
        title={disableTarget?.is_disabled ? 'Enable account' : 'Disable account'}
        confirmLabel={disableTarget?.is_disabled ? 'Enable account' : 'Disable account'}
        tone={disableTarget?.is_disabled ? 'primary' : 'danger'}
        busy={disableBusy}
        body={
          disableTarget?.is_disabled
            ? `${disableTarget.email} will be able to sign in again.`
            : `Disable ${disableTarget?.email}? They're signed out everywhere immediately. A charge already in progress keeps running until it finishes or is stopped separately.`
        }
      />

      <Modal
        open={Boolean(balanceTarget)}
        onClose={closeBalanceModal}
        title={`Adjust balance — ${balanceTarget?.email || ''}`}
        size="sm"
        footer={
          <>
            <button type="button" className="btn btn-quiet" onClick={closeBalanceModal} disabled={balanceBusy}>
              Cancel
            </button>
            <button type="button" className="btn btn-primary" onClick={submitBalance} disabled={balanceBusy}>
              {balanceBusy ? 'Saving…' : 'Adjust balance'}
            </button>
          </>
        }
      >
        {balanceTarget && (
          <form className="admin-users-balance-form" onSubmit={submitBalance}>
            <p className="text-2 text-sm">
              Current balance: <Money coins={balanceTarget.coin_balance} rate={rate} />
            </p>
            <div className="field">
              <label className="field-label" htmlFor="admin-users-balance-amount">
                Amount (₹)
              </label>
              <input
                id="admin-users-balance-amount"
                className="input"
                type="number"
                step="0.01"
                inputMode="decimal"
                placeholder="e.g. 50 or -50"
                value={balanceAmount}
                onChange={(e) => setBalanceAmount(e.target.value)}
              />
              <p className="field-help">Positive credits the wallet, negative debits it.</p>
            </div>
            <div className="field">
              <label className="field-label" htmlFor="admin-users-balance-reason">
                Reason
              </label>
              <input
                id="admin-users-balance-reason"
                className="input"
                type="text"
                placeholder="Why is this adjustment being made?"
                value={balanceReason}
                onChange={(e) => setBalanceReason(e.target.value)}
              />
            </div>
            {balanceError && <p className="field-error">{balanceError}</p>}
          </form>
        )}
      </Modal>
    </>
  );
}
