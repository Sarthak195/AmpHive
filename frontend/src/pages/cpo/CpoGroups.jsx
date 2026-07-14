/**
 * AmpHive CPO Groups Management Page
 * =====================================
 * CRUD interface for managing charger groups (public/private).
 * Features: group cards with plug/member counts, create/edit/delete modals,
 * access code display with copy-to-clipboard, and access code regeneration.
 *
 * Data source: /api/cpo/groups
 */

import { useState, useEffect, useCallback } from 'react';
import CpoLayout from '../../components/CpoLayout';
import api from '../../api/client';

const CpoGroups = () => {
  const [groups, setGroups] = useState([]);
  const [tenantName, setTenantName] = useState('');
  const [loading, setLoading] = useState(true);

  // Modal state
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [editingGroup, setEditingGroup] = useState(null);
  const [deletingGroup, setDeletingGroup] = useState(null);
  const [formError, setFormError] = useState('');
  const [formLoading, setFormLoading] = useState(false);

  // Create form
  const [createForm, setCreateForm] = useState({ name: '', is_public: false });

  // Edit form
  const [editForm, setEditForm] = useState({ name: '', is_public: false, max_current_a: '' });

  // Clipboard feedback
  const [copiedId, setCopiedId] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const [groupsRes, profileRes] = await Promise.all([
        api.get('/api/cpo/groups'),
        api.get('/api/cpo/profile'),
      ]);
      setGroups(groupsRes);
      setTenantName(profileRes.tenant?.name || '');
    } catch (err) {
      console.error('Failed to load groups:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  /**
   * Copy access code to clipboard with visual feedback.
   */
  const copyAccessCode = async (code, groupId) => {
    try {
      await navigator.clipboard.writeText(code);
      setCopiedId(groupId);
      setTimeout(() => setCopiedId(null), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  /**
   * Create a new group.
   */
  const handleCreateGroup = async (e) => {
    e.preventDefault();
    if (!createForm.name.trim()) {
      setFormError('Group name is required.');
      return;
    }

    setFormLoading(true);
    setFormError('');

    try {
      await api.post('/api/cpo/groups', {
        name: createForm.name.trim(),
        is_public: createForm.is_public,
      });
      setShowCreateModal(false);
      setCreateForm({ name: '', is_public: false });
      await fetchData();
    } catch (err) {
      setFormError(err.message || 'Failed to create group.');
    } finally {
      setFormLoading(false);
    }
  };

  /**
   * Open edit modal with pre-filled data.
   */
  const openEditModal = (group) => {
    setEditingGroup(group);
    setEditForm({
      name: group.name,
      is_public: group.is_public,
      max_current_a: group.max_current_a ?? '',
    });
    setFormError('');
    setShowEditModal(true);
  };

  /**
   * Update an existing group.
   */
  const handleEditGroup = async (e) => {
    e.preventDefault();
    if (!editingGroup) return;

    setFormLoading(true);
    setFormError('');

    try {
      const body = {};
      if (editForm.name !== editingGroup.name) body.name = editForm.name;
      if (editForm.is_public !== editingGroup.is_public) body.is_public = editForm.is_public;
      // [Caps] Circuit capacity (amps). Blank -> 0, which the backend reads as
      // "clear the limit". Only sent when changed.
      if (String(editForm.max_current_a) !== String(editingGroup.max_current_a ?? '')) {
        body.max_current_a = editForm.max_current_a === '' ? 0 : Number(editForm.max_current_a);
      }

      await api.put(`/api/cpo/groups/${editingGroup.id}`, body);
      setShowEditModal(false);
      setEditingGroup(null);
      await fetchData();
    } catch (err) {
      setFormError(err.message || 'Failed to update group.');
    } finally {
      setFormLoading(false);
    }
  };

  /**
   * Regenerate access code for a private group.
   */
  const handleRegenerateCode = async (group) => {
    try {
      await api.put(`/api/cpo/groups/${group.id}`, { regenerate_access_code: true });
      await fetchData();
    } catch (err) {
      console.error('Failed to regenerate code:', err);
    }
  };

  /**
   * Delete a group after confirmation.
   */
  const handleDeleteGroup = async () => {
    if (!deletingGroup) return;

    setFormLoading(true);
    try {
      await api.delete(`/api/cpo/groups/${deletingGroup.id}`);
      setShowDeleteConfirm(false);
      setDeletingGroup(null);
      await fetchData();
    } catch (err) {
      console.error('Failed to delete group:', err);
    } finally {
      setFormLoading(false);
    }
  };

  return (
    <CpoLayout tenantName={tenantName}>
      {/* Page Header */}
      <div className="cpo-page-header flex justify-between items-center" style={{ flexWrap: 'wrap', gap: '1rem' }}>
        <div>
          <h1>Charger Groups</h1>
          <p>Organize your plugs into public or private groups</p>
        </div>
        <button className="btn btn-cpo" onClick={() => { setFormError(''); setShowCreateModal(true); }}>
          + Create Group
        </button>
      </div>

      {/* Groups Grid */}
      {loading ? (
        <div className="card-grid">
          {[1, 2, 3].map((i) => (
            <div key={i} className="glass glass-card" style={{ cursor: 'default' }}>
              <div className="skeleton" style={{ width: '60%', height: '18px', marginBottom: '1rem' }} />
              <div className="skeleton" style={{ width: '40%', height: '14px', marginBottom: '0.5rem' }} />
              <div className="skeleton" style={{ width: '50%', height: '14px' }} />
            </div>
          ))}
        </div>
      ) : groups.length > 0 ? (
        <div className="card-grid animate-fade-in">
          {groups.map((group, idx) => (
            <div
              key={group.id}
              className="glass glass-card animate-fade-in"
              style={{ cursor: 'default', animationDelay: `${idx * 0.1}s` }}
            >
              {/* Group Header */}
              <div className="flex justify-between items-center" style={{ marginBottom: '0.75rem' }}>
                <h3 style={{ margin: 0, fontSize: '1.1rem' }}>{group.name}</h3>
                <span className={`badge ${group.is_public ? 'badge-success' : 'badge-primary'}`}>
                  {group.is_public ? 'Public' : 'Private'}
                </span>
              </div>

              {/* Stats Row */}
              <div className="flex gap-4" style={{ marginBottom: '1rem' }}>
                <div>
                  <span style={{ color: 'var(--color-text-muted)', fontSize: '0.78rem' }}>Plugs</span>
                  <p style={{ color: 'var(--color-text-primary)', fontWeight: 600, margin: '0.1rem 0 0', fontSize: '1.1rem' }}>
                    {group.plug_count}
                  </p>
                </div>
                {!group.is_public && (
                  <div>
                    <span style={{ color: 'var(--color-text-muted)', fontSize: '0.78rem' }}>Members</span>
                    <p style={{ color: 'var(--color-text-primary)', fontWeight: 600, margin: '0.1rem 0 0', fontSize: '1.1rem' }}>
                      {group.member_count}
                    </p>
                  </div>
                )}
                {/* [Caps] Live circuit load vs limit, when a limit is set. */}
                {group.max_current_a != null && (
                  <div>
                    <span style={{ color: 'var(--color-text-muted)', fontSize: '0.78rem' }}>Circuit</span>
                    <p className="num" style={{ color: 'var(--color-text-primary)', fontWeight: 600, margin: '0.1rem 0 0', fontSize: '1.1rem' }}>
                      {group.current_load_a ?? 0} / {group.max_current_a} A
                    </p>
                  </div>
                )}
              </div>

              {/* [Caps] Drivers waiting on "Request capacity" — a nudge to raise
                  the cap (or free a plug). Raising it notifies them automatically. */}
              {group.pending_capacity_requests > 0 && (
                <div className="badge badge-warning" style={{ marginBottom: '0.75rem' }}>
                  ⚡ {group.pending_capacity_requests} waiting for capacity
                </div>
              )}

              {/* Access Code (private groups only) */}
              {!group.is_public && group.access_code && (
                <div style={{ marginBottom: '1rem' }}>
                  <span style={{ color: 'var(--color-text-muted)', fontSize: '0.75rem', display: 'block', marginBottom: '0.35rem' }}>
                    ACCESS CODE
                  </span>
                  <div className="access-code-display">
                    {group.access_code}
                    <button
                      className={`copy-btn ${copiedId === group.id ? 'copied' : ''}`}
                      onClick={() => copyAccessCode(group.access_code, group.id)}
                      title="Copy to clipboard"
                    >
                      {copiedId === group.id ? '✓' : '📋'}
                    </button>
                  </div>
                </div>
              )}

              {/* Action Buttons */}
              <div className="flex gap-2" style={{ marginTop: 'auto' }}>
                <button className="btn btn-ghost btn-sm" onClick={() => openEditModal(group)}>
                  Edit
                </button>
                {!group.is_public && (
                  <button
                    className="btn btn-ghost btn-sm"
                    onClick={() => handleRegenerateCode(group)}
                    style={{ color: 'var(--color-cpo-accent)' }}
                    title="Generate new access code"
                  >
                    🔄 New Code
                  </button>
                )}
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={() => { setDeletingGroup(group); setShowDeleteConfirm(true); }}
                  style={{ color: 'var(--color-danger)', marginLeft: 'auto' }}
                >
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-state glass">
          <div className="empty-state-icon">👥</div>
          <h3>No groups yet</h3>
          <p>Create charger groups to organize your plugs. Private groups require an access code for drivers to join.</p>
          <button className="btn btn-cpo" onClick={() => setShowCreateModal(true)}>
            + Create Your First Group
          </button>
        </div>
      )}

      {/* Create Group Modal */}
      {showCreateModal && (
        <div className="modal-overlay" onClick={() => setShowCreateModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Create Charger Group</h2>
              <button className="modal-close" onClick={() => setShowCreateModal(false)}>✕</button>
            </div>

            <form onSubmit={handleCreateGroup}>
              <div className="flex flex-col gap-4">
                <div className="input-group">
                  <label>Group Name *</label>
                  <input
                    type="text"
                    className="input"
                    placeholder="e.g. Sunrise Apartments"
                    value={createForm.name}
                    onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })}
                    autoFocus
                    required
                  />
                </div>

                <div className="input-group">
                  <label>Visibility</label>
                  <div className="flex gap-4 mt-2">
                    <label className="flex items-center gap-2" style={{ cursor: 'pointer', fontSize: '0.9rem', color: 'var(--color-text-secondary)' }}>
                      <input
                        type="radio"
                        name="visibility"
                        checked={!createForm.is_public}
                        onChange={() => setCreateForm({ ...createForm, is_public: false })}
                        style={{ accentColor: 'var(--color-cpo-accent)' }}
                      />
                      🔒 Private (access code required)
                    </label>
                    <label className="flex items-center gap-2" style={{ cursor: 'pointer', fontSize: '0.9rem', color: 'var(--color-text-secondary)' }}>
                      <input
                        type="radio"
                        name="visibility"
                        checked={createForm.is_public}
                        onChange={() => setCreateForm({ ...createForm, is_public: true })}
                        style={{ accentColor: 'var(--color-cpo-accent)' }}
                      />
                      🌐 Public (open to all)
                    </label>
                  </div>
                </div>

                {!createForm.is_public && (
                  <p style={{ fontSize: '0.82rem', color: 'var(--color-text-muted)', margin: 0 }}>
                    An access code will be auto-generated. Share it with drivers to give them access.
                  </p>
                )}
              </div>

              {formError && <p className="error-text mt-4">{formError}</p>}

              <div className="modal-actions">
                <button type="button" className="btn btn-ghost" onClick={() => setShowCreateModal(false)}>Cancel</button>
                <button type="submit" className="btn btn-cpo" disabled={formLoading}>
                  {formLoading ? 'Creating...' : 'Create Group'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Edit Group Modal */}
      {showEditModal && editingGroup && (
        <div className="modal-overlay" onClick={() => setShowEditModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Edit Group</h2>
              <button className="modal-close" onClick={() => setShowEditModal(false)}>✕</button>
            </div>

            <form onSubmit={handleEditGroup}>
              <div className="flex flex-col gap-4">
                <div className="input-group">
                  <label>Group Name</label>
                  <input
                    type="text"
                    className="input"
                    value={editForm.name}
                    onChange={(e) => setEditForm({ ...editForm, name: e.target.value })}
                  />
                </div>

                <div className="input-group">
                  <label>Visibility</label>
                  <div className="flex gap-4 mt-2">
                    <label className="flex items-center gap-2" style={{ cursor: 'pointer', fontSize: '0.9rem', color: 'var(--color-text-secondary)' }}>
                      <input
                        type="radio"
                        name="edit-visibility"
                        checked={!editForm.is_public}
                        onChange={() => setEditForm({ ...editForm, is_public: false })}
                        style={{ accentColor: 'var(--color-cpo-accent)' }}
                      />
                      🔒 Private
                    </label>
                    <label className="flex items-center gap-2" style={{ cursor: 'pointer', fontSize: '0.9rem', color: 'var(--color-text-secondary)' }}>
                      <input
                        type="radio"
                        name="edit-visibility"
                        checked={editForm.is_public}
                        onChange={() => setEditForm({ ...editForm, is_public: true })}
                        style={{ accentColor: 'var(--color-cpo-accent)' }}
                      />
                      🌐 Public
                    </label>
                  </div>
                </div>

                <div className="input-group">
                  <label>Circuit capacity (A)</label>
                  <input
                    type="number"
                    className="input"
                    min="0"
                    step="1"
                    placeholder="No limit"
                    value={editForm.max_current_a}
                    onChange={(e) => setEditForm({ ...editForm, max_current_a: e.target.value })}
                  />
                  <small style={{ color: 'var(--color-text-muted)', fontSize: '0.78rem' }}>
                    Total amps this group's chargers share. A start is blocked when
                    the sum of active charger caps would exceed it. Blank = no limit.
                  </small>
                  <small style={{ color: 'var(--color-warning, #b8860b)', fontSize: '0.78rem', display: 'block', marginTop: '0.35rem' }}>
                    Note: admission math assumes each charger draws no more than its
                    per-plug cap, but a sub-default cap (below 16 A) is not yet enforced
                    on the device (pending a firmware OTA). The circuit-protection
                    guarantee is hard only when every charger sits at the 16 A default.
                  </small>
                </div>
              </div>

              {formError && <p className="error-text mt-4">{formError}</p>}

              <div className="modal-actions">
                <button type="button" className="btn btn-ghost" onClick={() => setShowEditModal(false)}>Cancel</button>
                <button type="submit" className="btn btn-cpo" disabled={formLoading}>
                  {formLoading ? 'Saving...' : 'Save Changes'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {showDeleteConfirm && deletingGroup && (
        <div className="modal-overlay" onClick={() => setShowDeleteConfirm(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Delete Group</h2>
              <button className="modal-close" onClick={() => setShowDeleteConfirm(false)}>✕</button>
            </div>

            <p style={{ marginBottom: '0.5rem' }}>
              Are you sure you want to delete <strong style={{ color: 'var(--color-text-primary)' }}>{deletingGroup.name}</strong>?
            </p>
            <p style={{ fontSize: '0.85rem', color: 'var(--color-warning)' }}>
              ⚠️ {deletingGroup.plug_count} plug{deletingGroup.plug_count !== 1 ? 's' : ''} will be unlinked
              and become visible to all users.
              {deletingGroup.member_count > 0 && ` ${deletingGroup.member_count} member${deletingGroup.member_count !== 1 ? 's' : ''} will lose access.`}
            </p>

            <div className="modal-actions">
              <button className="btn btn-ghost" onClick={() => setShowDeleteConfirm(false)}>Cancel</button>
              <button
                className="btn btn-danger"
                onClick={handleDeleteGroup}
                disabled={formLoading}
              >
                {formLoading ? 'Deleting...' : 'Delete Group'}
              </button>
            </div>
          </div>
        </div>
      )}
    </CpoLayout>
  );
};

export default CpoGroups;
