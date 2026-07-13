/**
 * AmpHive CPO Gateways Page
 * =========================
 * Fleet view of the CPO's ESP32 gateways: online/offline state, the firmware
 * version each one last reported, last-seen, and plug count — plus a one-click
 * OTA firmware update (POST /api/cpo/gateways/{id}/ota). This closes the OTA
 * loop: see which gateways are behind, then push an update without curl.
 *
 * Data source: /api/cpo/gateways, /api/cpo/profile
 */

import { useState, useEffect, useCallback } from 'react';
import CpoLayout from '../../components/CpoLayout';
import CpoSummary from '../../components/CpoSummary';
import api from '../../api/client';

// Default OTA image (public signed bucket). Operators can edit before pushing;
// firmware ≥ 1.4.0 requires https + a valid ECDSA signature, and the backend
// rejects non-https URLs, so keep this an https bucket URL.
const DEFAULT_FW_URL = 'https://storage.googleapis.com/amphive-fw/amphive-gateway-1.5.0.bin';

const timeAgo = (iso) => {
  if (!iso) return 'never';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 0) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
};

const CpoGateways = () => {
  const [gateways, setGateways] = useState([]);
  const [tenantName, setTenantName] = useState('');
  const [loading, setLoading] = useState(true);

  // OTA modal state
  const [otaTarget, setOtaTarget] = useState(null); // the gateway being updated
  const [otaUrl, setOtaUrl] = useState(DEFAULT_FW_URL);
  const [otaBusy, setOtaBusy] = useState(false);
  const [otaError, setOtaError] = useState('');
  const [otaSuccess, setOtaSuccess] = useState('');

  const fetchData = useCallback(async () => {
    try {
      const [gwRes, profileRes] = await Promise.all([
        api.get('/api/cpo/gateways'),
        api.get('/api/cpo/profile'),
      ]);
      setGateways(gwRes);
      setTenantName(profileRes.tenant?.name || '');
    } catch (err) {
      console.error('Failed to load gateways:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  const openOtaModal = (gw) => {
    setOtaTarget(gw);
    setOtaUrl(DEFAULT_FW_URL);
    setOtaError('');
    setOtaSuccess('');
  };

  const submitOta = async (e) => {
    e.preventDefault();
    if (!otaTarget) return;
    setOtaBusy(true);
    setOtaError('');
    setOtaSuccess('');
    try {
      await api.post(`/api/cpo/gateways/${otaTarget.id}/ota`, { firmware_url: otaUrl.trim() });
      setOtaSuccess(
        'OTA command sent. The gateway will download the image, reboot, and report its new ' +
        'firmware version here shortly (it refuses the update if a session is active).'
      );
      // Refresh after a beat so the status flips as the gateway reboots.
      setTimeout(fetchData, 4000);
    } catch (err) {
      setOtaError(err.message);
    } finally {
      setOtaBusy(false);
    }
  };

  const onlineCount = gateways.filter((g) => g.status === 'online').length;

  return (
    <CpoLayout tenantName={tenantName}>
      <div className="cpo-page-header">
        <h1>Gateways</h1>
        <p>Your ESP32 gateways — status, firmware, and over-the-air updates</p>
      </div>

      {/* Summary */}
      {!loading && (
        <CpoSummary
          items={[
            { label: 'Gateways Online', value: `${onlineCount} / ${gateways.length}`, tone: 'success' },
          ]}
        />
      )}

      {loading ? (
        <div className="glass glass-panel skeleton" style={{ height: '200px' }} />
      ) : gateways.length === 0 ? (
        <div className="glass glass-panel text-center" style={{ padding: '3rem 2rem' }}>
          <p style={{ fontSize: '2rem', marginBottom: '0.5rem' }}>📡</p>
          <h2 style={{ marginBottom: '0.5rem' }}>No gateways yet</h2>
          <p style={{ color: 'var(--color-text-muted)' }}>
            Register a gateway (from the Plugs page's gateway picker or the API) to see it here.
          </p>
        </div>
      ) : (
        <div className="data-table-container animate-fade-in">
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Gateway ID</th>
                <th>Status</th>
                <th>Firmware</th>
                <th>Last Seen</th>
                <th>Plugs</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {gateways.map((gw) => {
                const online = gw.status === 'online';
                return (
                  <tr key={gw.id}>
                    <td style={{ color: 'var(--color-text-primary)', fontWeight: 500 }}>{gw.name}</td>
                    <td style={{ fontFamily: 'monospace', fontSize: '0.82rem' }}>{gw.id}</td>
                    <td>
                      <span className="flex items-center gap-1">
                        <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: online ? 'var(--color-success)' : 'var(--color-text-muted)' }} />
                        <span className={`badge ${online ? 'badge-success' : 'badge-danger'}`}>{gw.status}</span>
                      </span>
                    </td>
                    <td style={{ fontFamily: 'monospace', fontSize: '0.82rem' }}>
                      {gw.firmware_version || <span style={{ color: 'var(--color-text-muted)' }}>unknown</span>}
                    </td>
                    <td style={{ color: 'var(--color-text-secondary)', fontSize: '0.85rem' }}>{timeAgo(gw.last_seen_at)}</td>
                    <td>{gw.plug_count}</td>
                    <td>
                      <button
                        className="btn btn-ghost btn-sm"
                        disabled={!online || gw.plug_count === 0}
                        title={
                          !online ? 'Gateway must be online to receive an OTA'
                          : gw.plug_count === 0 ? 'Gateway needs at least one plug to route the OTA command'
                          : 'Push a firmware update over the air'
                        }
                        onClick={() => openOtaModal(gw)}
                      >
                        ⬆ Update Firmware
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* OTA Modal */}
      {otaTarget && (
        <div className="modal-overlay" onClick={() => setOtaTarget(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Update Firmware</h2>
              <button className="modal-close" onClick={() => setOtaTarget(null)}>✕</button>
            </div>

            <form onSubmit={submitOta}>
              <div className="flex flex-col gap-4">
                <p style={{ color: 'var(--color-text-secondary)', fontSize: '0.9rem', margin: 0 }}>
                  Push an OTA update to <strong>{otaTarget.name}</strong> (<span style={{ fontFamily: 'monospace' }}>{otaTarget.id}</span>),
                  currently on <strong>{otaTarget.firmware_version || 'unknown'}</strong>.
                  The gateway downloads the image, reboots, and rolls back automatically if the new
                  image fails to reconnect. It refuses the update while a session is active.
                </p>

                <div className="input-group">
                  <label>Firmware image URL (https) *</label>
                  <input
                    type="url"
                    className="input"
                    value={otaUrl}
                    onChange={(e) => setOtaUrl(e.target.value)}
                    placeholder="https://storage.googleapis.com/amphive-fw/amphive-gateway-<ver>.bin"
                    required
                    pattern="https://.*"
                  />
                  <span style={{ color: 'var(--color-text-muted)', fontSize: '0.75rem' }}>
                    Must be https and signed (firmware ≥ 1.4.0 verifies the signature and rejects plain http).
                  </span>
                </div>

                {otaError && <div className="error-text">{otaError}</div>}
                {otaSuccess && (
                  <div style={{ color: 'var(--color-success)', fontSize: '0.9rem' }}>{otaSuccess}</div>
                )}

                <div className="flex gap-2 justify-end">
                  <button type="button" className="btn btn-ghost" onClick={() => setOtaTarget(null)}>
                    {otaSuccess ? 'Close' : 'Cancel'}
                  </button>
                  {!otaSuccess && (
                    <button type="submit" className="btn btn-cpo" disabled={otaBusy}>
                      {otaBusy ? 'Sending…' : 'Push Update'}
                    </button>
                  )}
                </div>
              </div>
            </form>
          </div>
        </div>
      )}
    </CpoLayout>
  );
};

export default CpoGateways;
