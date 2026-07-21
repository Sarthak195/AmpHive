/**
 * CpoGateways (redesign v3, D3) — fleet view of the CPO's ESP32 gateways:
 * online/offline state + last-seen, the firmware version each one last
 * reported (flagged "behind" against the fleet's newest reporting version),
 * plug count, and a one-click OTA push. Also registers new gateways.
 *
 * CpoLayout/TenantContext already own the org name and nav badges — this
 * page fetches only its own gateway list.
 *
 * Data: GET/POST /api/cpo/gateways, POST /api/cpo/gateways/{id}/ota,
 * GET /api/cpo/audit (best-effort OTA URL prefill only — degrades to an
 * empty field if nothing OTA-shaped turns up, never a fake default).
 */

import { useCallback, useState } from 'react';
import { Plus, Radio, UploadCloud } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import { PageHeader, DataTable, Modal, ConfirmDialog, StatusDot, useToast } from '../../components/ui';
import api from '../../api/client';
import usePoll from '../../hooks/usePoll';
import { apiErrorCopy } from '../../utils/statusCopy';

const POLL_MS = 30_000;

const timeAgo = (iso) => {
  if (!iso) return 'never';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 0) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
};

/** Loose numeric-part comparison for firmware strings like "2.3.0" or
    "1.9.0-direct" — non-numeric suffixes count as 0, which is enough to
    rank a fleet's reporting versions without a real semver dependency. */
const versionParts = (v) =>
  String(v || '')
    .split(/[.-]/)
    .map((p) => parseInt(p, 10))
    .map((n) => (Number.isNaN(n) ? 0 : n));

const compareVersions = (a, b) => {
  const pa = versionParts(a);
  const pb = versionParts(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const diff = (pa[i] || 0) - (pb[i] || 0);
    if (diff !== 0) return diff;
  }
  return 0;
};

const fleetMaxVersion = (gateways) => {
  const versions = gateways.map((g) => g.firmware_version).filter(Boolean);
  if (versions.length === 0) return null;
  return versions.reduce((max, v) => (compareVersions(v, max) > 0 ? v : max));
};

/** Best-effort OTA URL prefill: scan the audit log for the most recent
    entry whose action looks OTA-shaped and pull an https URL out of its
    detail string. Nothing today records this, so this quietly resolves to
    '' until the backend starts logging it — never invents a default. */
const extractLastOtaUrl = (auditItems) => {
  for (const item of auditItems) {
    if (!/ota/i.test(item.action || '')) continue;
    const match = /https:\/\/\S+/.exec(item.detail || '');
    if (match) return match[0];
  }
  return '';
};

export default function CpoGateways() {
  const toast = useToast();
  const [gateways, setGateways] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchGateways = useCallback(async () => {
    try {
      const res = await api.get('/api/cpo/gateways');
      setGateways(Array.isArray(res) ? res : res?.items || []);
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  usePoll(fetchGateways, POLL_MS);

  // ---- Add gateway ----------------------------------------------------
  const [addOpen, setAddOpen] = useState(false);
  const [addGatewayId, setAddGatewayId] = useState('');
  const [addName, setAddName] = useState('');
  const [addBusy, setAddBusy] = useState(false);
  const [addError, setAddError] = useState('');

  const openAdd = () => {
    setAddGatewayId('');
    setAddName('');
    setAddError('');
    setAddOpen(true);
  };

  const closeAdd = () => {
    if (addBusy) return;
    setAddOpen(false);
  };

  const submitAdd = async (e) => {
    e.preventDefault();
    setAddBusy(true);
    setAddError('');
    try {
      await api.post('/api/cpo/gateways', {
        gateway_id: addGatewayId.trim(),
        name: addName.trim(),
      });
      setAddOpen(false);
      toast.ok('Gateway registered.');
      fetchGateways();
    } catch (err) {
      setAddError(apiErrorCopy(err));
    } finally {
      setAddBusy(false);
    }
  };

  // ---- OTA: URL entry, then a named confirm step -----------------------
  const [otaTarget, setOtaTarget] = useState(null);
  const [otaUrl, setOtaUrl] = useState('');
  const [otaConfirming, setOtaConfirming] = useState(false);
  const [otaBusy, setOtaBusy] = useState(false);
  const [otaError, setOtaError] = useState('');

  const openOta = async (gw) => {
    setOtaTarget(gw);
    setOtaUrl('');
    setOtaError('');
    setOtaConfirming(false);
    try {
      const audit = await api.get('/api/cpo/audit?limit=100');
      const items = Array.isArray(audit) ? audit : audit?.items || [];
      setOtaUrl(extractLastOtaUrl(items));
    } catch {
      // Prefill is best-effort only — an empty field is a fine fallback.
    }
  };

  const closeOta = () => {
    if (otaBusy) return;
    setOtaTarget(null);
    setOtaConfirming(false);
  };

  const submitOtaUrl = (e) => {
    e.preventDefault();
    setOtaConfirming(true);
  };

  const confirmOta = async () => {
    if (!otaTarget) return;
    setOtaBusy(true);
    setOtaError('');
    try {
      await api.post(`/api/cpo/gateways/${otaTarget.id}/ota`, { firmware_url: otaUrl.trim() });
      toast.ok(`Update pushed to ${otaTarget.name}.`);
      setOtaTarget(null);
      setOtaConfirming(false);
      setTimeout(fetchGateways, 4000);
    } catch (err) {
      toast.error(apiErrorCopy(err));
      setOtaConfirming(false);
    } finally {
      setOtaBusy(false);
    }
  };

  const maxFw = fleetMaxVersion(gateways);

  const columns = [
    { key: 'name', label: 'Name' },
    {
      key: 'gw_status',
      label: 'Status',
      render: (gw) => (
        <span className="row">
          <StatusDot state={gw.status === 'online' ? 'available' : 'offline'} live={gw.status === 'online'} />
          <span className="text-2 text-sm">
            {gw.status === 'online' ? 'Online' : 'Offline'} · {timeAgo(gw.last_seen_at)}
          </span>
        </span>
      ),
    },
    {
      key: 'firmware_version',
      label: 'Firmware',
      render: (gw) => (
        <span className="row">
          <span className="chip">{gw.firmware_version || 'unknown'}</span>
          {maxFw && gw.firmware_version && gw.firmware_version !== maxFw && (
            <span className="badge badge-warn">behind</span>
          )}
        </span>
      ),
    },
    { key: 'plug_count', label: 'Chargers', num: true },
    {
      key: 'actions',
      label: 'Actions',
      render: (gw) => (
        <button
          type="button"
          className="btn btn-quiet btn-sm"
          disabled={gw.status !== 'online' || gw.plug_count === 0}
          title={
            gw.status !== 'online'
              ? 'Gateway must be online to receive an update'
              : gw.plug_count === 0
                ? 'Add a charger to this gateway first'
                : 'Push a firmware update over the air'
          }
          onClick={() => openOta(gw)}
        >
          <UploadCloud size={15} aria-hidden="true" />
          Update firmware
        </button>
      ),
    },
  ];

  return (
    <CpoLayout>
      <PageHeader
        title="Gateways"
        sub="Your ESP32 gateways — status, firmware, and over-the-air updates."
        actions={
          <button type="button" className="btn btn-primary" onClick={openAdd}>
            <Plus size={16} aria-hidden="true" />
            Add gateway
          </button>
        }
      />

      <DataTable
        columns={columns}
        rows={gateways}
        loading={loading}
        error={error}
        onRetry={fetchGateways}
        emptyIcon={Radio}
        emptyTitle="No gateways yet"
        emptyBody="Register a gateway's device ID to see it here."
        emptyAction={
          <button type="button" className="btn btn-primary btn-sm" onClick={openAdd}>
            Add gateway
          </button>
        }
        collapse
      />

      {/* Add gateway */}
      <Modal open={addOpen} onClose={closeAdd} title="Add gateway">
        <form className="stack" onSubmit={submitAdd}>
          <p className="text-2 text-sm">
            Flash the gateway firmware, then copy the device ID it shows in its own
            setup portal — that&apos;s the gateway ID below. Give it a name your team
            will recognize (its parking spot or building).
          </p>
          <div className="field">
            <label className="field-label" htmlFor="gw-add-id">
              Gateway ID (device MAC)
            </label>
            <input
              id="gw-add-id"
              className="input"
              value={addGatewayId}
              onChange={(e) => setAddGatewayId(e.target.value)}
              placeholder="a1b2c3d4e5f6"
              required
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="gw-add-name">
              Name
            </label>
            <input
              id="gw-add-name"
              className="input"
              value={addName}
              onChange={(e) => setAddName(e.target.value)}
              placeholder="Basement gateway"
              required
            />
          </div>
          {addError && <p className="field-error">{addError}</p>}
          <div className="modal-actions">
            <button type="button" className="btn btn-quiet" onClick={closeAdd} disabled={addBusy}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={addBusy}>
              {addBusy ? 'Registering…' : 'Add gateway'}
            </button>
          </div>
        </form>
      </Modal>

      {/* OTA: URL entry */}
      <Modal
        open={Boolean(otaTarget) && !otaConfirming}
        onClose={closeOta}
        title="Update firmware"
      >
        {otaTarget && (
          <form className="stack" onSubmit={submitOtaUrl}>
            <p className="text-2 text-sm">
              Push an update to <strong>{otaTarget.name}</strong>, currently on{' '}
              <strong>{otaTarget.firmware_version || 'unknown'}</strong>. It downloads
              the image, reboots, and rolls back automatically if it fails to
              reconnect — it refuses the update while a session is active.
            </p>
            <div className="field">
              <label className="field-label" htmlFor="gw-ota-url">
                Firmware image URL (https)
              </label>
              <input
                id="gw-ota-url"
                type="url"
                className="input"
                value={otaUrl}
                onChange={(e) => setOtaUrl(e.target.value)}
                placeholder="https://storage.googleapis.com/…/amphive-gateway-<version>.bin"
                pattern="https://.*"
                required
              />
              <span className="field-help">
                Must be https and signed — firmware ≥ 1.4.0 verifies the signature and
                rejects plain http.
              </span>
            </div>
            {otaError && <p className="field-error">{otaError}</p>}
            <div className="modal-actions">
              <button type="button" className="btn btn-quiet" onClick={closeOta}>
                Cancel
              </button>
              <button type="submit" className="btn btn-primary">
                Continue
              </button>
            </div>
          </form>
        )}
      </Modal>

      {/* OTA: named confirm step */}
      <ConfirmDialog
        open={Boolean(otaTarget) && otaConfirming}
        onClose={() => setOtaConfirming(false)}
        onConfirm={confirmOta}
        title="Push firmware update?"
        body={`${otaTarget?.name || 'This gateway'} will download and install the new firmware now. This can't be undone once it starts.`}
        confirmLabel="Push update"
        tone="primary"
        busy={otaBusy}
        busyLabel="Sending…"
      />
    </CpoLayout>
  );
}
