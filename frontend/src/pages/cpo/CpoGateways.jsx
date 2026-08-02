/**
 * CpoGateways (redesign v3, D3; claim-code onboarding added 2026-08-02;
 * OTA version picker added 2026-08-02, feat/ota-version-picker) — fleet
 * view of the CPO's ESP32 gateways: online/offline state + last-seen, the
 * firmware version each one last reported (flagged "behind" against the
 * fleet's newest reporting version), plug count, and a one-click OTA push.
 *
 * Adding a gateway: the primary "Add gateway" flow is now the claim-code
 * form (POST /api/cpo/gateways/claim) — a preflashed unit ships with a short
 * code on its label, no operator hand-registration needed. The old manual
 * gateway-ID/name form (POST /api/cpo/gateways) still exists for lab/legacy
 * units that were never minted as claim-code inventory, tucked behind a
 * "Register a gateway manually" link inside the claim modal.
 *
 * Update firmware: replaces hand-pasting a firmware URL with a dropdown of
 * registered releases (GET /api/cpo/firmware-releases), newest first,
 * options newer than the gateway's current version marked "— newer". Only
 * the admin role sees a "custom URL" escape hatch (POST .../ota rejects a
 * raw firmware_url from a non-admin with 403 — this toggle is a UX nicety,
 * not the enforcement).
 *
 * CpoLayout/TenantContext already own the org name and nav badges — this
 * page fetches only its own gateway list.
 *
 * Data: GET /api/cpo/gateways, POST /api/cpo/gateways/claim (primary add),
 * POST /api/cpo/gateways (manual add), GET /api/cpo/firmware-releases
 * (version picker), POST /api/cpo/gateways/{id}/ota.
 */

import { useCallback, useState } from 'react';
import { Plus, Radio, UploadCloud } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import { PageHeader, DataTable, Modal, ConfirmDialog, StatusDot, useToast } from '../../components/ui';
import api from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import usePoll from '../../hooks/usePoll';
import { apiErrorCopy } from '../../utils/statusCopy';
import { compareVersions, isNewerVersion } from '../../utils/version';

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

const fleetMaxVersion = (gateways) => {
  const versions = gateways.map((g) => g.firmware_version).filter(Boolean);
  if (versions.length === 0) return null;
  return versions.reduce((max, v) => (compareVersions(v, max) > 0 ? v : max));
};

export default function CpoGateways() {
  const toast = useToast();
  const { user } = useAuth();
  const isAdmin = user?.role === 'admin';
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

  // ---- Add gateway: claim code (primary flow) --------------------------
  const [claimOpen, setClaimOpen] = useState(false);
  const [claimCode, setClaimCode] = useState('');
  const [claimName, setClaimName] = useState('');
  const [claimBusy, setClaimBusy] = useState(false);
  const [claimError, setClaimError] = useState('');

  const openClaim = () => {
    setClaimCode('');
    setClaimName('');
    setClaimError('');
    setClaimOpen(true);
  };

  const closeClaim = () => {
    if (claimBusy) return;
    setClaimOpen(false);
  };

  const submitClaim = async (e) => {
    e.preventDefault();
    setClaimBusy(true);
    setClaimError('');
    try {
      await api.post('/api/cpo/gateways/claim', {
        claim_code: claimCode.trim(),
        name: claimName.trim() || undefined,
      });
      setClaimOpen(false);
      toast.ok('Gateway added.');
      fetchGateways();
    } catch (err) {
      setClaimError(apiErrorCopy(err));
    } finally {
      setClaimBusy(false);
    }
  };

  // ---- Add gateway: manual registration (secondary, lab/legacy units) --
  const [manualOpen, setManualOpen] = useState(false);
  const [manualGatewayId, setManualGatewayId] = useState('');
  const [manualName, setManualName] = useState('');
  const [manualBusy, setManualBusy] = useState(false);
  const [manualError, setManualError] = useState('');

  const openManual = () => {
    setManualGatewayId('');
    setManualName('');
    setManualError('');
    setClaimOpen(false);
    setManualOpen(true);
  };

  const closeManual = () => {
    if (manualBusy) return;
    setManualOpen(false);
  };

  const submitManual = async (e) => {
    e.preventDefault();
    setManualBusy(true);
    setManualError('');
    try {
      await api.post('/api/cpo/gateways', {
        gateway_id: manualGatewayId.trim(),
        name: manualName.trim(),
      });
      setManualOpen(false);
      toast.ok('Gateway registered.');
      fetchGateways();
    } catch (err) {
      setManualError(apiErrorCopy(err));
    } finally {
      setManualBusy(false);
    }
  };

  // ---- OTA: version picker, then a named confirm step -------------------
  const [otaTarget, setOtaTarget] = useState(null);
  const [releases, setReleases] = useState([]);
  const [releasesLoading, setReleasesLoading] = useState(false);
  const [releasesError, setReleasesError] = useState('');
  const [selectedReleaseId, setSelectedReleaseId] = useState('');
  const [useCustomUrl, setUseCustomUrl] = useState(false);
  const [customUrl, setCustomUrl] = useState('');
  const [otaConfirming, setOtaConfirming] = useState(false);
  const [otaBusy, setOtaBusy] = useState(false);
  const [otaError, setOtaError] = useState('');

  const openOta = async (gw) => {
    setOtaTarget(gw);
    setOtaConfirming(false);
    setOtaError('');
    setUseCustomUrl(false);
    setCustomUrl('');
    setSelectedReleaseId('');
    setReleases([]);
    setReleasesError('');
    setReleasesLoading(true);
    try {
      const res = await api.get('/api/cpo/firmware-releases');
      const items = Array.isArray(res) ? res : res?.items || [];
      setReleases(items);
      // Default to the newest release — the common case is "push the latest".
      if (items.length > 0) setSelectedReleaseId(String(items[0].id));
    } catch (err) {
      setReleasesError(apiErrorCopy(err));
    } finally {
      setReleasesLoading(false);
    }
  };

  const closeOta = () => {
    if (otaBusy) return;
    setOtaTarget(null);
    setOtaConfirming(false);
  };

  const submitOta = (e) => {
    e.preventDefault();
    setOtaConfirming(true);
  };

  const selectedRelease = releases.find((r) => String(r.id) === selectedReleaseId);

  const confirmOta = async () => {
    if (!otaTarget) return;
    setOtaBusy(true);
    setOtaError('');
    try {
      const body = useCustomUrl
        ? { firmware_url: customUrl.trim() }
        : { release_id: Number(selectedReleaseId) };
      await api.post(`/api/cpo/gateways/${otaTarget.id}/ota`, body);
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
          <button type="button" className="btn btn-primary" onClick={openClaim}>
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
        emptyBody="Add a gateway with the claim code printed on its label to see it here."
        emptyAction={
          <button type="button" className="btn btn-primary btn-sm" onClick={openClaim}>
            Add gateway
          </button>
        }
        collapse
      />

      {/* Add gateway: claim code (primary flow) */}
      <Modal open={claimOpen} onClose={closeClaim} title="Add gateway">
        <form className="stack" onSubmit={submitClaim}>
          <p className="text-2 text-sm">
            Enter the claim code printed on the gateway&apos;s label or box. It
            binds the device to your account — no separate registration step.
          </p>
          <div className="field">
            <label className="field-label" htmlFor="gw-claim-code">
              Claim code
            </label>
            <input
              id="gw-claim-code"
              className="input"
              value={claimCode}
              onChange={(e) => setClaimCode(e.target.value)}
              placeholder="H4KX-9Q2P-FW"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck="false"
              required
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="gw-claim-name">
              Name <span className="text-2 text-sm">(optional)</span>
            </label>
            <input
              id="gw-claim-name"
              className="input"
              value={claimName}
              onChange={(e) => setClaimName(e.target.value)}
              placeholder="Basement gateway"
            />
          </div>
          {claimError && <p className="field-error">{claimError}</p>}
          <div className="modal-actions">
            <button type="button" className="btn btn-quiet" onClick={closeClaim} disabled={claimBusy}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={claimBusy}>
              {claimBusy ? 'Adding…' : 'Add gateway'}
            </button>
          </div>
          <button type="button" className="btn btn-ghost btn-sm btn-full" onClick={openManual}>
            Register a gateway manually instead
          </button>
        </form>
      </Modal>

      {/* Add gateway: manual registration (secondary, lab/legacy units) */}
      <Modal open={manualOpen} onClose={closeManual} title="Register a gateway manually">
        <form className="stack" onSubmit={submitManual}>
          <p className="text-2 text-sm">
            Flash the gateway firmware, then copy the device ID it shows in its own
            setup portal — that&apos;s the gateway ID below. Give it a name your team
            will recognize (its parking spot or building).
          </p>
          <div className="field">
            <label className="field-label" htmlFor="gw-manual-id">
              Gateway ID (device MAC)
            </label>
            <input
              id="gw-manual-id"
              className="input"
              value={manualGatewayId}
              onChange={(e) => setManualGatewayId(e.target.value)}
              placeholder="a1b2c3d4e5f6"
              required
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="gw-manual-name">
              Name
            </label>
            <input
              id="gw-manual-name"
              className="input"
              value={manualName}
              onChange={(e) => setManualName(e.target.value)}
              placeholder="Basement gateway"
              required
            />
          </div>
          {manualError && <p className="field-error">{manualError}</p>}
          <div className="modal-actions">
            <button type="button" className="btn btn-quiet" onClick={closeManual} disabled={manualBusy}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={manualBusy}>
              {manualBusy ? 'Registering…' : 'Add gateway'}
            </button>
          </div>
        </form>
      </Modal>

      {/* OTA: version picker (dropdown of registered releases, descending) */}
      <Modal
        open={Boolean(otaTarget) && !otaConfirming}
        onClose={closeOta}
        title="Update firmware"
      >
        {otaTarget && (
          <form className="stack" onSubmit={submitOta}>
            <p className="text-2 text-sm">
              Push an update to <strong>{otaTarget.name}</strong>, currently on{' '}
              <strong>{otaTarget.firmware_version || 'unknown'}</strong>. It downloads
              the image, reboots, and rolls back automatically if it fails to
              reconnect — it refuses the update while a session is active.
            </p>

            {!useCustomUrl && (
              <div className="field">
                <label className="field-label" htmlFor="gw-ota-release">
                  Firmware version
                </label>
                {releasesLoading ? (
                  <p className="text-2 text-sm">Loading available versions…</p>
                ) : releasesError ? (
                  <p className="field-error">{releasesError}</p>
                ) : releases.length === 0 ? (
                  <p className="text-2 text-sm">
                    No firmware releases registered yet. Ask an admin to register one.
                  </p>
                ) : (
                  <select
                    id="gw-ota-release"
                    className="select"
                    value={selectedReleaseId}
                    onChange={(e) => setSelectedReleaseId(e.target.value)}
                    required
                  >
                    {releases.map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.version}
                        {isNewerVersion(r.version, otaTarget.firmware_version) ? ' — newer' : ''}
                      </option>
                    ))}
                  </select>
                )}
                {selectedRelease?.notes && (
                  <span className="field-help">{selectedRelease.notes}</span>
                )}
              </div>
            )}

            {useCustomUrl && (
              <div className="field">
                <label className="field-label" htmlFor="gw-ota-url">
                  Firmware image URL (https)
                </label>
                <input
                  id="gw-ota-url"
                  type="url"
                  className="input"
                  value={customUrl}
                  onChange={(e) => setCustomUrl(e.target.value)}
                  placeholder="https://storage.googleapis.com/…/amphive-gateway-<version>.bin"
                  pattern="https://.*"
                  required
                />
                <span className="field-help">
                  Must be https and signed — firmware ≥ 1.4.0 verifies the signature and
                  rejects plain http.
                </span>
              </div>
            )}

            {otaError && <p className="field-error">{otaError}</p>}

            <div className="modal-actions">
              <button type="button" className="btn btn-quiet" onClick={closeOta}>
                Cancel
              </button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={!useCustomUrl && (releasesLoading || releases.length === 0)}
              >
                Continue
              </button>
            </div>

            {isAdmin && (
              <button
                type="button"
                className="btn btn-ghost btn-sm btn-full"
                onClick={() => setUseCustomUrl((v) => !v)}
              >
                {useCustomUrl ? 'Pick a registered version instead' : 'Use a custom URL instead (admin)'}
              </button>
            )}
          </form>
        )}
      </Modal>

      {/* OTA: named confirm step */}
      <ConfirmDialog
        open={Boolean(otaTarget) && otaConfirming}
        onClose={() => setOtaConfirming(false)}
        onConfirm={confirmOta}
        title="Push firmware update?"
        body={
          useCustomUrl
            ? `${otaTarget?.name || 'This gateway'} will download and install the custom firmware image now. This can't be undone once it starts.`
            : `${otaTarget?.name || 'This gateway'} will download and install ${selectedRelease?.version || 'the selected version'} now. This can't be undone once it starts.`
        }
        confirmLabel="Push update"
        tone="primary"
        busy={otaBusy}
        busyLabel="Sending…"
      />
    </CpoLayout>
  );
}
