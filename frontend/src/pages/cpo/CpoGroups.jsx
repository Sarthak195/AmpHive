/**
 * CpoGroups — charger groups (public lots & private, code-gated communities):
 * master-detail. The left column lists every group (badge, plug/member
 * counts, a live circuit meter when a shared cap is set); selecting one opens
 * its detail panel (full-screen on narrow viewports, back arrow to return):
 * Access (code + copy + regenerate), Members (join via new members endpoint,
 * remove w/ confirm), Chargers in this group (link to Chargers filtered by
 * group), Circuit (shared amp cap editor + the safety advisory), and a
 * Danger zone (delete, with the concrete "plugs become public" consequence).
 *
 * Data: GET/POST/PUT/DELETE /api/cpo/groups, GET /api/cpo/plugs (chargers are
 * filtered to the selected group client-side — no per-group endpoint exists),
 * GET /api/cpo/groups/{id}/members + DELETE .../members/{user_id}
 * (redesign/ui-v3 contract §4 "CPO gaps" — degrades to an inline ErrorState
 * if the backend hasn't landed these yet).
 */

import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft, Copy, PlugZap, Plus, RefreshCw, Trash2, Users } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import Modal from '../../components/ui/Modal';
import ConfirmDialog from '../../components/ui/ConfirmDialog';
import PageHeader from '../../components/ui/PageHeader';
import EmptyState from '../../components/ui/EmptyState';
import ErrorState from '../../components/ui/ErrorState';
import Skeleton from '../../components/ui/Skeleton';
import { useToast } from '../../components/ui';
import api from '../../api/client';
import { apiErrorCopy } from '../../utils/statusCopy';
import './CpoGroups.css';

const LOAD_WARN = 0.8;
const LOAD_DANGER = 0.95;

function meterFillClass(load, limit) {
  if (!limit) return '';
  const ratio = load / limit;
  if (ratio >= LOAD_DANGER) return 'is-danger';
  if (ratio >= LOAD_WARN) return 'is-warn';
  return '';
}

function GroupCard({ group, active, onSelect }) {
  const load = group.current_load_a || 0;
  return (
    <button
      type="button"
      className={`card card-link groups-card${active ? ' is-active' : ''}`}
      aria-current={active ? 'true' : undefined}
      onClick={() => onSelect(group.id)}
    >
      <div className="groups-card-header">
        <h3>{group.name}</h3>
        <span className={`badge ${group.is_public ? 'badge-ok' : 'badge-info'}`}>
          {group.is_public ? 'Public' : 'Private'}
        </span>
      </div>

      <div className="groups-card-counts">
        <span>
          {group.plug_count} {group.plug_count === 1 ? 'charger' : 'chargers'}
        </span>
        {!group.is_public && (
          <span>
            {group.member_count} {group.member_count === 1 ? 'member' : 'members'}
          </span>
        )}
      </div>

      {group.max_current_a != null && (
        <div className="groups-card-meter">
          <div className="groups-card-meter-label">
            <span>Circuit</span>
            <span className="num">
              {load} / {group.max_current_a} A
            </span>
          </div>
          <div className="meter">
            <div
              className={`meter-fill ${meterFillClass(load, group.max_current_a)}`}
              style={{ width: `${Math.min(100, (load / group.max_current_a) * 100)}%` }}
            />
          </div>
        </div>
      )}

      {group.pending_capacity_requests > 0 && (
        <span className="badge badge-warn">
          {group.pending_capacity_requests} waiting for capacity
        </span>
      )}
    </button>
  );
}

export default function CpoGroups() {
  const toast = useToast();

  const [groups, setGroups] = useState([]);
  const [groupsLoading, setGroupsLoading] = useState(true);
  const [groupsError, setGroupsError] = useState(null);

  const [plugs, setPlugs] = useState([]);
  const [plugsError, setPlugsError] = useState(null);

  const [selectedId, setSelectedId] = useState(null);

  const [members, setMembers] = useState([]);
  const [membersLoading, setMembersLoading] = useState(false);
  const [membersError, setMembersError] = useState(null);

  const [limitInput, setLimitInput] = useState('');
  const [limitBusy, setLimitBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState({ name: '', is_public: false });
  const [createBusy, setCreateBusy] = useState(false);

  const [editOpen, setEditOpen] = useState(false);
  const [editForm, setEditForm] = useState({ name: '', is_public: false });
  const [editBusy, setEditBusy] = useState(false);

  const [regenOpen, setRegenOpen] = useState(false);
  const [regenBusy, setRegenBusy] = useState(false);

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const [removeTarget, setRemoveTarget] = useState(null);
  const [removeBusy, setRemoveBusy] = useState(false);

  const selected = groups.find((g) => g.id === selectedId) || null;

  const fetchGroups = useCallback(async () => {
    setGroupsLoading(true);
    setGroupsError(null);
    try {
      const data = await api.get('/api/cpo/groups');
      setGroups(data || []);
    } catch (err) {
      setGroupsError(err);
    } finally {
      setGroupsLoading(false);
    }
  }, []);

  const fetchPlugs = useCallback(async () => {
    setPlugsError(null);
    try {
      const data = await api.get('/api/cpo/plugs');
      setPlugs(data || []);
    } catch (err) {
      setPlugsError(err);
    }
  }, []);

  useEffect(() => {
    fetchGroups();
  }, [fetchGroups]);

  useEffect(() => {
    fetchPlugs();
  }, [fetchPlugs]);

  const fetchMembers = useCallback(async (groupId) => {
    setMembersLoading(true);
    setMembersError(null);
    try {
      const data = await api.get(`/api/cpo/groups/${groupId}/members`);
      setMembers(data || []);
    } catch (err) {
      setMembersError(err);
    } finally {
      setMembersLoading(false);
    }
  }, []);

  // Members only exist for private groups; re-fetch whenever the selection
  // (or its visibility) changes.
  useEffect(() => {
    if (!selected || selected.is_public) {
      setMembers([]);
      setMembersError(null);
      return;
    }
    fetchMembers(selected.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id, selected?.is_public]);

  // Reset the circuit editor + copy feedback to the freshly-selected group.
  useEffect(() => {
    setLimitInput(selected?.max_current_a != null ? String(selected.max_current_a) : '');
    setCopied(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id]);

  const openCreate = () => {
    setCreateForm({ name: '', is_public: false });
    setCreateOpen(true);
  };

  const handleCreate = async (e) => {
    e.preventDefault();
    const name = createForm.name.trim();
    if (!name || createBusy) return;
    setCreateBusy(true);
    try {
      const created = await api.post('/api/cpo/groups', { name, is_public: createForm.is_public });
      toast.ok(`Created "${name}".`);
      setCreateOpen(false);
      await fetchGroups();
      setSelectedId(created.group_id);
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setCreateBusy(false);
    }
  };

  const openEdit = () => {
    if (!selected) return;
    setEditForm({ name: selected.name, is_public: selected.is_public });
    setEditOpen(true);
  };

  const handleEdit = async (e) => {
    e.preventDefault();
    if (!selected) return;
    const name = editForm.name.trim();
    if (!name || editBusy) return;
    setEditBusy(true);
    try {
      const body = {};
      if (name !== selected.name) body.name = name;
      if (editForm.is_public !== selected.is_public) body.is_public = editForm.is_public;
      await api.put(`/api/cpo/groups/${selected.id}`, body);
      toast.ok('Group updated.');
      setEditOpen(false);
      await fetchGroups();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setEditBusy(false);
    }
  };

  const copyCode = async () => {
    if (!selected?.access_code) return;
    try {
      await navigator.clipboard.writeText(selected.access_code);
      setCopied(true);
      toast.ok('Access code copied.');
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error('Could not copy — copy the code manually.');
    }
  };

  const confirmRegen = async () => {
    if (!selected) return;
    setRegenBusy(true);
    try {
      await api.put(`/api/cpo/groups/${selected.id}`, { regenerate_access_code: true });
      toast.ok('New access code generated.');
      setRegenOpen(false);
      await fetchGroups();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setRegenBusy(false);
    }
  };

  const saveLimit = async () => {
    if (!selected || limitBusy) return;
    setLimitBusy(true);
    try {
      const value = limitInput === '' ? 0 : Number(limitInput);
      await api.put(`/api/cpo/groups/${selected.id}`, { max_current_a: value });
      toast.ok('Circuit limit updated.');
      await fetchGroups();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setLimitBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!selected) return;
    setDeleteBusy(true);
    try {
      await api.delete(`/api/cpo/groups/${selected.id}`);
      toast.ok(`Deleted "${selected.name}".`);
      setDeleteOpen(false);
      setSelectedId(null);
      await fetchGroups();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setDeleteBusy(false);
    }
  };

  const confirmRemoveMember = async () => {
    if (!removeTarget || !selected) return;
    setRemoveBusy(true);
    try {
      await api.delete(`/api/cpo/groups/${selected.id}/members/${removeTarget.user_id}`);
      toast.ok(`Removed ${removeTarget.email}.`);
      setRemoveTarget(null);
      await fetchMembers(selected.id);
      await fetchGroups();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setRemoveBusy(false);
    }
  };

  const groupPlugs = selected ? plugs.filter((p) => p.group_id === selected.id) : [];

  const deleteBody = selected
    ? `${selected.plug_count} ${selected.plug_count === 1 ? 'charger' : 'chargers'} will become public and visible to all users.${
        !selected.is_public && selected.member_count > 0
          ? ` ${selected.member_count} ${selected.member_count === 1 ? 'member' : 'members'} will lose access.`
          : ''
      }`
    : '';

  return (
    <CpoLayout>
      <PageHeader
        title="Groups"
        sub="Organize chargers into public lots or private, code-gated communities."
        actions={
          <button type="button" className="btn btn-primary" onClick={openCreate}>
            <Plus size={18} aria-hidden="true" />
            Create group
          </button>
        }
      />

      {groupsLoading ? (
        <Skeleton lines={5} />
      ) : groupsError ? (
        <ErrorState error={groupsError} onRetry={fetchGroups} />
      ) : groups.length === 0 ? (
        <EmptyState
          icon={Users}
          title="No groups yet"
          body="Create a group to share an access code with a private community, or organize a public lot's chargers."
          action={
            <button type="button" className="btn btn-primary" onClick={openCreate}>
              Create group
            </button>
          }
        />
      ) : (
        <div className={`groups-layout${selected ? ' has-selection' : ''}`}>
          <div>
            <h2 className="sr-only">Your groups</h2>
            <div className="groups-list">
              {groups.map((group) => (
                <GroupCard
                  key={group.id}
                  group={group}
                  active={group.id === selectedId}
                  onSelect={setSelectedId}
                />
              ))}
            </div>
          </div>

          <div className="groups-detail">
            {!selected ? (
              <EmptyState
                icon={Users}
                title="Select a group"
                body="Choose a group from the list to see its access code, members, chargers and circuit settings."
              />
            ) : (
              <div className="card groups-detail-card">
                <div className="groups-detail-header">
                  <button
                    type="button"
                    className="btn btn-quiet btn-icon groups-detail-back"
                    aria-label="Back to groups"
                    onClick={() => setSelectedId(null)}
                  >
                    <ArrowLeft size={18} aria-hidden="true" />
                  </button>
                  <h2>{selected.name}</h2>
                  <span className={`badge ${selected.is_public ? 'badge-ok' : 'badge-info'}`}>
                    {selected.is_public ? 'Public' : 'Private'}
                  </span>
                  <button
                    type="button"
                    className="btn btn-quiet btn-sm groups-detail-edit"
                    onClick={openEdit}
                  >
                    Edit
                  </button>
                </div>

                {!selected.is_public && (
                  <section className="groups-section">
                    <h3>Access</h3>
                    {selected.access_code ? (
                      <div className="groups-access-row">
                        <code className="groups-access-code">{selected.access_code}</code>
                        <button type="button" className="btn btn-quiet btn-sm" onClick={copyCode}>
                          <Copy size={15} aria-hidden="true" />
                          {copied ? 'Copied' : 'Copy'}
                        </button>
                        <button
                          type="button"
                          className="btn btn-quiet btn-sm"
                          onClick={() => setRegenOpen(true)}
                        >
                          <RefreshCw size={15} aria-hidden="true" />
                          New code
                        </button>
                      </div>
                    ) : (
                      <p className="text-2 text-sm">No access code yet.</p>
                    )}
                  </section>
                )}

                {!selected.is_public && (
                  <section className="groups-section">
                    <h3>Members</h3>
                    {membersLoading ? (
                      <Skeleton lines={2} />
                    ) : membersError ? (
                      <ErrorState error={membersError} onRetry={() => fetchMembers(selected.id)} />
                    ) : members.length === 0 ? (
                      <p className="text-2 text-sm">No one has joined with the access code yet.</p>
                    ) : (
                      <ul className="groups-members-list">
                        {members.map((m) => (
                          <li key={m.user_id} className="groups-member-row">
                            <span>
                              {m.full_name || m.email}
                              {m.full_name && <span className="text-3 text-sm"> · {m.email}</span>}
                            </span>
                            <button
                              type="button"
                              className="btn btn-quiet btn-sm"
                              aria-label={`Remove ${m.full_name || m.email} from this group`}
                              onClick={() => setRemoveTarget(m)}
                            >
                              Remove
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </section>
                )}

                <section className="groups-section">
                  <h3>Chargers in this group</h3>
                  {plugsError ? (
                    <ErrorState error={plugsError} onRetry={fetchPlugs} />
                  ) : groupPlugs.length === 0 ? (
                    <p className="text-2 text-sm">No chargers assigned yet.</p>
                  ) : (
                    <div className="groups-charger-chips">
                      {groupPlugs.map((p) => (
                        <span key={p.id} className="chip">
                          <PlugZap size={13} aria-hidden="true" />
                          {p.name}
                        </span>
                      ))}
                    </div>
                  )}
                  <Link to={`/cpo/chargers?group=${selected.id}`} className="btn btn-quiet btn-sm">
                    View in Chargers
                  </Link>
                </section>

                <section className="groups-section">
                  <h3>Circuit</h3>
                  <div className="groups-circuit-form">
                    <div className="field">
                      <label className="field-label" htmlFor="groups-circuit-limit">
                        Shared capacity (A)
                      </label>
                      <input
                        id="groups-circuit-limit"
                        type="number"
                        min="0"
                        step="1"
                        className="input"
                        placeholder="No limit"
                        value={limitInput}
                        onChange={(e) => setLimitInput(e.target.value)}
                      />
                    </div>
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      disabled={limitBusy}
                      onClick={saveLimit}
                    >
                      {limitBusy ? 'Saving…' : 'Save'}
                    </button>
                  </div>
                  <p className="field-help">
                    Total amps this group&apos;s chargers share. A charge is blocked from starting
                    when the sum of active caps would exceed it. Blank saves as no limit.
                  </p>
                  <div className="banner banner-warn">
                    Below-16A limits are enforced when starting sessions, not by the plug hardware.
                  </div>
                </section>

                <section className="groups-section groups-danger">
                  <h3>Danger zone</h3>
                  <p className="text-sm">{deleteBody}</p>
                  <button type="button" className="btn btn-danger" onClick={() => setDeleteOpen(true)}>
                    <Trash2 size={16} aria-hidden="true" />
                    Delete group
                  </button>
                </section>
              </div>
            )}
          </div>
        </div>
      )}

      <Modal
        open={createOpen}
        onClose={() => !createBusy && setCreateOpen(false)}
        title="Create group"
      >
        <form className="stack" onSubmit={handleCreate}>
          <div className="field">
            <label className="field-label" htmlFor="groups-create-name">
              Group name
            </label>
            <input
              id="groups-create-name"
              className="input"
              value={createForm.name}
              onChange={(e) => setCreateForm((f) => ({ ...f, name: e.target.value }))}
              placeholder="e.g. Sunrise Apartments"
              autoFocus
              required
            />
          </div>
          <div className="field">
            <span className="field-label">Visibility</span>
            <label className="checkbox-row">
              <input
                type="radio"
                name="create-visibility"
                checked={!createForm.is_public}
                onChange={() => setCreateForm((f) => ({ ...f, is_public: false }))}
              />
              Private — needs an access code to join
            </label>
            <label className="checkbox-row">
              <input
                type="radio"
                name="create-visibility"
                checked={createForm.is_public}
                onChange={() => setCreateForm((f) => ({ ...f, is_public: true }))}
              />
              Public — open to every driver
            </label>
          </div>

          <div className="modal-actions">
            <button
              type="button"
              className="btn btn-quiet"
              disabled={createBusy}
              onClick={() => setCreateOpen(false)}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={createBusy || !createForm.name.trim()}
            >
              {createBusy ? 'Creating…' : 'Create group'}
            </button>
          </div>
        </form>
      </Modal>

      <Modal open={editOpen} onClose={() => !editBusy && setEditOpen(false)} title="Edit group">
        <form className="stack" onSubmit={handleEdit}>
          <div className="field">
            <label className="field-label" htmlFor="groups-edit-name">
              Group name
            </label>
            <input
              id="groups-edit-name"
              className="input"
              value={editForm.name}
              onChange={(e) => setEditForm((f) => ({ ...f, name: e.target.value }))}
              required
            />
          </div>
          <div className="field">
            <span className="field-label">Visibility</span>
            <label className="checkbox-row">
              <input
                type="radio"
                name="edit-visibility"
                checked={!editForm.is_public}
                onChange={() => setEditForm((f) => ({ ...f, is_public: false }))}
              />
              Private — needs an access code to join
            </label>
            <label className="checkbox-row">
              <input
                type="radio"
                name="edit-visibility"
                checked={editForm.is_public}
                onChange={() => setEditForm((f) => ({ ...f, is_public: true }))}
              />
              Public — open to every driver
            </label>
          </div>

          <div className="modal-actions">
            <button
              type="button"
              className="btn btn-quiet"
              disabled={editBusy}
              onClick={() => setEditOpen(false)}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={editBusy || !editForm.name.trim()}
            >
              {editBusy ? 'Saving…' : 'Save changes'}
            </button>
          </div>
        </form>
      </Modal>

      <ConfirmDialog
        open={regenOpen}
        onClose={() => !regenBusy && setRegenOpen(false)}
        onConfirm={confirmRegen}
        title="Generate a new access code?"
        body="Every member's saved code stops working immediately — they'll need the new code to keep joining this group's chargers."
        confirmLabel="Generate new code"
        tone="danger"
        busy={regenBusy}
      />

      <ConfirmDialog
        open={deleteOpen}
        onClose={() => !deleteBusy && setDeleteOpen(false)}
        onConfirm={confirmDelete}
        title={`Delete ${selected?.name || 'this group'}?`}
        body={deleteBody}
        confirmLabel="Delete group"
        tone="danger"
        busy={deleteBusy}
      />

      <ConfirmDialog
        open={Boolean(removeTarget)}
        onClose={() => !removeBusy && setRemoveTarget(null)}
        onConfirm={confirmRemoveMember}
        title={`Remove ${removeTarget?.email || 'this member'}?`}
        body="They'll lose access to this group's private chargers immediately."
        confirmLabel="Remove member"
        tone="danger"
        busy={removeBusy}
      />
    </CpoLayout>
  );
}
