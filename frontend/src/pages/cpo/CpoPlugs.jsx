/**
 * AmpHive CPO Plugs Management Page
 * ====================================
 * CRUD interface for managing smart plugs across the CPO's gateways.
 * Features: plug table with status filters, add/edit modals, inline
 * status toggling between Available and Maintenance, and a per-plug QR
 * code (2026-07-12) linking to the driver deep-link start (`/?plug=<id>`)
 * for printing and posting at the charger.
 *
 * Data source: /api/cpo/plugs, /api/cpo/gateways, /api/cpo/groups
 */

import { useState, useEffect, useCallback, useMemo } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import CpoLayout from '../../components/CpoLayout';
import api from '../../api/client';

/**
 * Status filter options matching the PlugStatus enum.
 */
const STATUS_FILTERS = [
  { value: '', label: 'All Statuses' },
  { value: 'available', label: 'Available' },
  { value: 'occupied', label: 'Occupied' },
  { value: 'offline', label: 'Offline' },
  { value: 'maintenance', label: 'Maintenance' },
];

/**
 * Map plug status to badge CSS class.
 */
const STATUS_BADGE = {
  available: 'badge-success',
  occupied: 'badge-warning',
  offline: 'badge-danger',
  maintenance: 'badge-primary',
};

const CpoPlugs = () => {
  const [plugs, setPlugs] = useState([]);
  const [gateways, setGateways] = useState([]);
  const [groups, setGroups] = useState([]);
  const [tenantName, setTenantName] = useState('');
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState('');

  // Gateway lookup so the plug table can show each gateway's live state + fw.
  const gatewaysById = useMemo(() => {
    const map = {};
    for (const gw of gateways) map[gw.id] = gw;
    return map;
  }, [gateways]);

  // Modal state
  const [showAddModal, setShowAddModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [editingPlug, setEditingPlug] = useState(null);
  const [formError, setFormError] = useState('');
  const [formLoading, setFormLoading] = useState(false);

  // QR code modal — the plug currently being shown, or null when closed.
  const [qrPlug, setQrPlug] = useState(null);

  // Add form fields
  const [addForm, setAddForm] = useState({
    gateway_id: '', name: '', local_ip: '', plug_model: 'tapo_p110', group_id: '',
  });

  // Edit form fields. queued_charging_enabled is a tri-state override select
  // ('' = inherit tenant default, 'true' = on, 'false' = off);
  // auto_start_delay_min blank = inherit.
  const [editForm, setEditForm] = useState({
    name: '', group_id: '', status: '', max_current_a: '',
    queued_charging_enabled: '', auto_start_delay_min: '',
  });

  const fetchData = useCallback(async () => {
    try {
      const [plugsRes, gatewaysRes, groupsRes, profileRes] = await Promise.all([
        api.get('/api/cpo/plugs'),
        api.get('/api/cpo/gateways'),
        api.get('/api/cpo/groups'),
        api.get('/api/cpo/profile'),
      ]);
      setPlugs(plugsRes);
      setGateways(gatewaysRes);
      setGroups(groupsRes);
      setTenantName(profileRes.tenant?.name || '');
    } catch (err) {
      console.error('Failed to load plugs:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  // Filter plugs by status
  const filteredPlugs = statusFilter
    ? plugs.filter((p) => p.status === statusFilter)
    : plugs;

  /**
   * Handle adding a new plug.
   */
  const handleAddPlug = async (e) => {
    e.preventDefault();
    if (!addForm.gateway_id || !addForm.name || !addForm.local_ip) {
      setFormError('Gateway, name, and IP address are required.');
      return;
    }

    setFormLoading(true);
    setFormError('');

    try {
      const body = {
        gateway_id: addForm.gateway_id,
        name: addForm.name,
        local_ip: addForm.local_ip,
        plug_model: addForm.plug_model || 'tapo_p110',
      };
      if (addForm.group_id) body.group_id = parseInt(addForm.group_id);

      await api.post('/api/cpo/plugs', body);
      setShowAddModal(false);
      setAddForm({ gateway_id: '', name: '', local_ip: '', plug_model: 'tapo_p110', group_id: '' });
      await fetchData(); // Refresh the plug list
    } catch (err) {
      setFormError(err.message || 'Failed to add plug.');
    } finally {
      setFormLoading(false);
    }
  };

  /**
   * Open the edit modal with pre-filled data.
   */
  const openEditModal = (plug) => {
    setEditingPlug(plug);
    setEditForm({
      name: plug.name,
      group_id: plug.group_id || '',
      status: plug.status,
      max_current_a: plug.max_current_a ?? '',
      // null override → '' (inherit tenant); explicit bool → its string.
      queued_charging_enabled:
        plug.queued_charging_enabled == null ? '' : String(plug.queued_charging_enabled),
      auto_start_delay_min: plug.auto_start_delay_min ?? '',
    });
    setFormError('');
    setShowEditModal(true);
  };

  /**
   * Handle updating a plug.
   */
  const handleEditPlug = async (e) => {
    e.preventDefault();
    if (!editingPlug) return;

    setFormLoading(true);
    setFormError('');

    try {
      const body = {};
      if (editForm.name !== editingPlug.name) body.name = editForm.name;
      if (editForm.status !== editingPlug.status) body.status = editForm.status;

      // Handle group assignment: empty string = remove from group (send 0)
      const newGroupId = editForm.group_id === '' ? 0 : parseInt(editForm.group_id);
      const oldGroupId = editingPlug.group_id || 0;
      if (newGroupId !== oldGroupId) body.group_id = newGroupId;

      // [Caps] Per-plug current cap (amps). Blank -> 0 = clear to the default
      // hardware cutoff. Only sent when changed.
      if (String(editForm.max_current_a) !== String(editingPlug.max_current_a ?? '')) {
        body.max_current_a = editForm.max_current_a === '' ? 0 : Number(editForm.max_current_a);
      }

      // [Queued charging] Per-plug override of the tenant default. The select
      // is tri-state: '' = inherit (send null), else the boolean. Only sent
      // when changed.
      const origQueued =
        editingPlug.queued_charging_enabled == null ? '' : String(editingPlug.queued_charging_enabled);
      if (editForm.queued_charging_enabled !== origQueued) {
        body.queued_charging_enabled =
          editForm.queued_charging_enabled === '' ? null : editForm.queued_charging_enabled === 'true';
      }
      // Auto-start debounce (minutes). Blank -> null = inherit the tenant
      // default. Only sent when changed.
      if (String(editForm.auto_start_delay_min) !== String(editingPlug.auto_start_delay_min ?? '')) {
        body.auto_start_delay_min =
          editForm.auto_start_delay_min === '' ? null : Number(editForm.auto_start_delay_min);
      }

      await api.put(`/api/cpo/plugs/${editingPlug.id}`, body);
      setShowEditModal(false);
      setEditingPlug(null);
      await fetchData();
    } catch (err) {
      setFormError(err.message || 'Failed to update plug.');
    } finally {
      setFormLoading(false);
    }
  };

  /**
   * Quick status toggle: Available ↔ Maintenance.
   */
  const toggleMaintenance = async (plug) => {
    const newStatus = plug.status === 'available' ? 'maintenance' : 'available';
    try {
      await api.put(`/api/cpo/plugs/${plug.id}`, { status: newStatus });
      await fetchData();
    } catch (err) {
      console.error('Failed to toggle status:', err);
    }
  };

  return (
    <CpoLayout tenantName={tenantName}>
      {/* Page Header */}
      <div className="cpo-page-header flex justify-between items-center" style={{ flexWrap: 'wrap', gap: '1rem' }}>
        <div>
          <h1>Plugs</h1>
          <p>Manage your smart charging plugs</p>
        </div>
        <button className="btn btn-cpo" onClick={() => { setFormError(''); setShowAddModal(true); }}>
          + Add Plug
        </button>
      </div>

      {/* Filter Bar */}
      <div className="filter-bar">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          {STATUS_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>{f.label}</option>
          ))}
        </select>
        <span style={{ color: 'var(--color-text-muted)', fontSize: '0.85rem' }}>
          {filteredPlugs.length} plug{filteredPlugs.length !== 1 ? 's' : ''}
        </span>
      </div>

      {/* Plugs Table */}
      {loading ? (
        <div className="data-table-container">
          {[1, 2, 3].map((i) => (
            <div key={i} style={{ padding: '1rem', borderBottom: '1px solid hsla(0,0%,100%,0.04)' }}>
              <div className="skeleton" style={{ width: '100%', height: '18px' }} />
            </div>
          ))}
        </div>
      ) : filteredPlugs.length > 0 ? (
        <div className="data-table-container animate-fade-in">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Gateway</th>
                <th>IP Address</th>
                <th>Group</th>
                <th>Status</th>
                <th>Power</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredPlugs.map((plug) => (
                <tr key={plug.id}>
                  <td style={{ color: 'var(--color-text-muted)' }}>#{plug.id}</td>
                  <td style={{ color: 'var(--color-text-primary)', fontWeight: 500 }}>
                    {plug.name}
                  </td>
                  <td>
                    {(() => {
                      const gw = gatewaysById[plug.gateway_id];
                      const online = gw?.status === 'online';
                      return (
                        <div className="flex flex-col" style={{ gap: '0.15rem' }}>
                          <span className="flex items-center gap-1" style={{ fontSize: '0.85rem' }}>
                            <span
                              title={online ? 'Online' : 'Offline'}
                              style={{
                                width: '8px', height: '8px', borderRadius: '50%', flexShrink: 0,
                                background: online ? 'var(--color-success)' : 'var(--color-text-muted)',
                              }}
                            />
                            {plug.gateway_id}
                          </span>
                          {gw?.firmware_version && (
                            <span style={{ color: 'var(--color-text-muted)', fontSize: '0.72rem' }}>
                              fw {gw.firmware_version}
                            </span>
                          )}
                        </div>
                      );
                    })()}
                  </td>
                  <td style={{ fontFamily: 'monospace', fontSize: '0.82rem' }}>{plug.local_ip}</td>
                  <td>{plug.group_name || <span style={{ color: 'var(--color-text-muted)' }}>Ungrouped</span>}</td>
                  <td>
                    <span className={`badge ${STATUS_BADGE[plug.status] || 'badge-warning'}`}>
                      {plug.status}
                    </span>
                  </td>
                  <td>
                    {plug.current_power_w > 0
                      ? <span style={{ color: 'var(--color-success)' }}>{plug.current_power_w}W</span>
                      : <span style={{ color: 'var(--color-text-muted)' }}>0W</span>
                    }
                  </td>
                  <td>
                    <div className="flex gap-2">
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => openEditModal(plug)}
                      >
                        Edit
                      </button>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => setQrPlug(plug)}
                        title="Show the QR code that starts this plug"
                      >
                        🔳 QR
                      </button>
                      {(plug.status === 'available' || plug.status === 'maintenance') && (
                        <button
                          className="btn btn-ghost btn-sm"
                          onClick={() => toggleMaintenance(plug)}
                          style={{
                            color: plug.status === 'available'
                              ? 'var(--color-warning)'
                              : 'var(--color-success)',
                          }}
                        >
                          {plug.status === 'available' ? '🔧' : '✓'}
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-state glass">
          <div className="empty-state-icon">🔌</div>
          <h3>No plugs found</h3>
          <p>
            {statusFilter
              ? 'No plugs match the selected filter. Try a different status.'
              : 'Register your first smart plug to get started.'}
          </p>
          {!statusFilter && (
            <button className="btn btn-cpo" onClick={() => setShowAddModal(true)}>
              + Add Your First Plug
            </button>
          )}
        </div>
      )}

      {/* Add Plug Modal */}
      {showAddModal && (
        <div className="modal-overlay" onClick={() => setShowAddModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Add New Plug</h2>
              <button className="modal-close" onClick={() => setShowAddModal(false)}>✕</button>
            </div>

            <form onSubmit={handleAddPlug}>
              <div className="flex flex-col gap-4">
                <div className="input-group">
                  <label>Gateway *</label>
                  <select
                    className="input"
                    value={addForm.gateway_id}
                    onChange={(e) => setAddForm({ ...addForm, gateway_id: e.target.value })}
                    required
                    style={{ appearance: 'auto', WebkitAppearance: 'auto' }}
                  >
                    <option value="">Select a gateway...</option>
                    {gateways.map((gw) => (
                      <option key={gw.id} value={gw.id}>{gw.name} ({gw.id})</option>
                    ))}
                  </select>
                </div>

                <div className="input-group">
                  <label>Plug Name *</label>
                  <input
                    type="text"
                    className="input"
                    placeholder="e.g. Parking Bay 1"
                    value={addForm.name}
                    onChange={(e) => setAddForm({ ...addForm, name: e.target.value })}
                    required
                  />
                </div>

                <div className="input-group">
                  <label>Local IP Address *</label>
                  <input
                    type="text"
                    className="input"
                    placeholder="e.g. 192.168.20.10"
                    value={addForm.local_ip}
                    onChange={(e) => setAddForm({ ...addForm, local_ip: e.target.value })}
                    required
                  />
                </div>

                <div className="input-group">
                  <label>Charger Group (Optional)</label>
                  <select
                    className="input"
                    value={addForm.group_id}
                    onChange={(e) => setAddForm({ ...addForm, group_id: e.target.value })}
                    style={{ appearance: 'auto', WebkitAppearance: 'auto' }}
                  >
                    <option value="">No group (public)</option>
                    {groups.map((g) => (
                      <option key={g.id} value={g.id}>{g.name} ({g.is_public ? 'Public' : 'Private'})</option>
                    ))}
                  </select>
                </div>
              </div>

              {formError && <p className="error-text mt-4">{formError}</p>}

              <div className="modal-actions">
                <button type="button" className="btn btn-ghost" onClick={() => setShowAddModal(false)}>Cancel</button>
                <button type="submit" className="btn btn-cpo" disabled={formLoading}>
                  {formLoading ? 'Adding...' : 'Add Plug'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Edit Plug Modal */}
      {showEditModal && editingPlug && (
        <div className="modal-overlay" onClick={() => setShowEditModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Edit Plug</h2>
              <button className="modal-close" onClick={() => setShowEditModal(false)}>✕</button>
            </div>

            <form onSubmit={handleEditPlug}>
              <div className="flex flex-col gap-4">
                <div className="input-group">
                  <label>Plug Name</label>
                  <input
                    type="text"
                    className="input"
                    value={editForm.name}
                    onChange={(e) => setEditForm({ ...editForm, name: e.target.value })}
                  />
                </div>

                <div className="input-group">
                  <label>Charger Group</label>
                  <select
                    className="input"
                    value={editForm.group_id}
                    onChange={(e) => setEditForm({ ...editForm, group_id: e.target.value })}
                    style={{ appearance: 'auto', WebkitAppearance: 'auto' }}
                  >
                    <option value="">No group (public)</option>
                    {groups.map((g) => (
                      <option key={g.id} value={g.id}>{g.name}</option>
                    ))}
                  </select>
                </div>

                <div className="input-group">
                  <label>Status</label>
                  <select
                    className="input"
                    value={editForm.status}
                    onChange={(e) => setEditForm({ ...editForm, status: e.target.value })}
                    style={{ appearance: 'auto', WebkitAppearance: 'auto' }}
                  >
                    <option value="available">Available</option>
                    <option value="offline">Offline</option>
                    <option value="maintenance">Maintenance</option>
                  </select>
                </div>

                <div className="input-group">
                  <label>Max current (A)</label>
                  <input
                    type="number"
                    className="input"
                    min="0"
                    step="1"
                    placeholder="Default (16 A)"
                    value={editForm.max_current_a}
                    onChange={(e) => setEditForm({ ...editForm, max_current_a: e.target.value })}
                  />
                  <small style={{ color: 'var(--color-text-muted)', fontSize: '0.78rem' }}>
                    This charger's current draw for circuit-capacity math. Blank = the
                    default hardware cutoff.
                  </small>
                  <small style={{ color: 'var(--color-warning, #b8860b)', fontSize: '0.78rem', display: 'block', marginTop: '0.35rem' }}>
                    Heads up: a cap below the 16 A hardware default is advisory for
                    admission math only — it is not yet enforced on the device (pending a
                    firmware OTA), so the plug can still draw up to 16 A. The hard
                    circuit-protection guarantee holds only at the default cap.
                  </small>
                </div>

                {/* [Queued charging] Per-plug override of the tenant default:
                    let drivers queue a charge while this plug has no line
                    power; it auto-starts (after the debounce below) when power
                    returns. Blank fields inherit the tenant setting. */}
                <div className="input-group">
                  <label>Queued charging</label>
                  <select
                    className="input"
                    value={editForm.queued_charging_enabled}
                    onChange={(e) => setEditForm({ ...editForm, queued_charging_enabled: e.target.value })}
                    style={{ appearance: 'auto', WebkitAppearance: 'auto' }}
                  >
                    <option value="">Inherit tenant default</option>
                    <option value="true">Enabled</option>
                    <option value="false">Disabled</option>
                  </select>
                  <small style={{ color: 'var(--color-text-muted)', fontSize: '0.78rem' }}>
                    Let drivers queue a charge when this plug loses line power; it
                    auto-starts when power returns. Blank inherits the tenant default.
                  </small>
                </div>

                <div className="input-group">
                  <label>Auto-start delay (minutes)</label>
                  <input
                    type="number"
                    className="input"
                    min="0"
                    step="1"
                    placeholder="Inherit tenant default"
                    value={editForm.auto_start_delay_min}
                    onChange={(e) => setEditForm({ ...editForm, auto_start_delay_min: e.target.value })}
                  />
                  <small style={{ color: 'var(--color-text-muted)', fontSize: '0.78rem' }}>
                    How long power must stay on continuously before a queued charge
                    auto-starts (debounces a brief line-test blip). Blank inherits the
                    tenant default.
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

      {/* QR Code Modal — deep-links to the driver Home page's `?plug=`
          prefill (Home.jsx / requirements.md still declines in-app QR
          *scanning*; this is a printable code the CPO posts at the charger
          that a phone camera resolves to a normal, auth-gated URL). Origin
          is read fresh from window.location at render time — never
          hardcoded — so this works unchanged on any deployment. */}
      {qrPlug && (
        <div className="modal-overlay" onClick={() => setQrPlug(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>QR Code</h2>
              <button className="modal-close" onClick={() => setQrPlug(null)}>✕</button>
            </div>

            <div className="qr-print-area flex flex-col items-center gap-3">
              <div className="qr-code-box">
                <QRCodeSVG
                  value={`${window.location.origin}/?plug=${qrPlug.id}`}
                  size={200}
                  bgColor="#ffffff"
                  fgColor="#0C0E07"
                  level="M"
                  marginSize={2}
                />
              </div>
              <div className="text-center">
                <p style={{ fontWeight: 600, color: 'var(--color-text-primary)', margin: 0 }}>{qrPlug.name}</p>
                <p style={{ color: 'var(--color-text-muted)', fontSize: '0.85rem', margin: '0.15rem 0 0' }}>
                  Plug ID: {qrPlug.id}
                </p>
                <p style={{ color: 'var(--color-text-muted)', fontSize: '0.75rem', wordBreak: 'break-all', margin: '0.5rem 0 0' }}>
                  {window.location.origin}/?plug={qrPlug.id}
                </p>
              </div>
            </div>

            <p style={{ color: 'var(--color-text-muted)', fontSize: '0.8rem', textAlign: 'center', marginTop: '1rem' }}>
              Scanning this opens AmpHive with the Plug ID prefilled — the driver still signs in and taps Start.
            </p>

            <div className="modal-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setQrPlug(null)}>Close</button>
              <button type="button" className="btn btn-cpo" onClick={() => window.print?.()}>
                🖨️ Print
              </button>
            </div>
          </div>
        </div>
      )}
    </CpoLayout>
  );
};

export default CpoPlugs;
