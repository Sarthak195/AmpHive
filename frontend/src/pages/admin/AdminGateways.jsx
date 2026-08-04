/**
 * AdminGateways — fleet view of all gateways across tenants.
 *
 * Shows a paginated table of gateways with organization, name, online/offline
 * status, firmware version (with behind-latest badge), and plug count. Includes
 * an online/offline filter and a firmware-spread summary showing how many
 * gateways run each version.
 *
 * Also mints preflashed-unit inventory (claim-code onboarding,
 * feat/easy-provisioning): "Mint inventory gateway" POSTs
 * /api/admin/gateways/inventory with the device's MAC, then shows the
 * freshly generated claim code once so the operator can copy it onto the
 * unit's label (see deploy/docs/preflashed_unit_runbook.md — this is the
 * "mint inventory row" step, done after flashing + before creating the
 * broker account). This fleet table only ever shows CLAIMED gateways
 * (GET /api/admin/gateways inner-joins Tenant); unclaimed inventory has no
 * tenant to join and doesn't belong in a tenant fleet view.
 *
 * Data source: GET /api/admin/gateways?online=&limit=&offset=
 */

import { useState, useCallback } from 'react';
import { Server, Plus, UploadCloud } from 'lucide-react';
import {
  ConfirmDialog,
  DataTable,
  ErrorState,
  Modal,
  PageHeader,
  StatusDot,
  useToast,
} from '../../components/ui';
import api from '../../api/client';
import usePoll from '../../hooks/usePoll';
import { apiErrorCopy } from '../../utils/statusCopy';
import { isNewerVersion } from '../../utils/version';

const DEFAULT_LIMIT = 25;

/**
 * Compute the maximum firmware version in a list (semantic versioning).
 * Returns null if no versions are present.
 */
function maxVersion(gateways) {
  const versions = gateways
    .map((g) => g.firmware_version)
    .filter((v) => v && v.trim());

  if (!versions.length) return null;

  return versions.sort((a, b) => {
    const aParts = a.split(/[.-]/).map((p) => parseInt(p, 10) || 0);
    const bParts = b.split(/[.-]/).map((p) => parseInt(p, 10) || 0);
    for (let i = 0; i < Math.max(aParts.length, bParts.length); i++) {
      const av = aParts[i] || 0;
      const bv = bParts[i] || 0;
      if (av !== bv) return bv - av; // descending: biggest first
    }
    return 0;
  })[0];
}

/**
 * Compute a map of { firmware_version: count } for the summary chips.
 */
function firmwareSpread(gateways) {
  const counts = {};
  gateways.forEach((g) => {
    if (g.firmware_version) {
      counts[g.firmware_version] = (counts[g.firmware_version] || 0) + 1;
    }
  });
  return counts;
}

/**
 * Format relative time (e.g. "2h ago", "3d ago", "never").
 */
function relativeTime(iso) {
  if (!iso) return 'never';
  const now = new Date();
  const then = new Date(iso);
  const secs = Math.floor((now - then) / 1000);

  if (secs < 0) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

export default function AdminGateways() {
  const toast = useToast();
  const [data, setData] = useState({
    items: [],
    total: 0,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Filters
  const [onlineFilter, setOnlineFilter] = useState('all'); // 'all' | 'online' | 'offline'
  const [offset, setOffset] = useState(0);

  // ---- Mint inventory gateway (preflashed-unit claim-code onboarding) --
  const [mintOpen, setMintOpen] = useState(false);
  const [mintGatewayId, setMintGatewayId] = useState('');
  const [mintName, setMintName] = useState('');
  const [mintBusy, setMintBusy] = useState(false);
  const [mintError, setMintError] = useState('');
  const [mintResult, setMintResult] = useState(null); // { gateway_id, claim_code } once minted
  const [codeCopied, setCodeCopied] = useState(false);

  const openMint = () => {
    setMintGatewayId('');
    setMintName('');
    setMintError('');
    setMintResult(null);
    setCodeCopied(false);
    setMintOpen(true);
  };

  const closeMint = () => {
    if (mintBusy) return;
    setMintOpen(false);
  };

  const submitMint = async (e) => {
    e.preventDefault();
    setMintBusy(true);
    setMintError('');
    try {
      const res = await api.post('/api/admin/gateways/inventory', {
        gateway_id: mintGatewayId.trim(),
        name: mintName.trim() || undefined,
      });
      setMintResult(res);
    } catch (err) {
      setMintError(apiErrorCopy(err));
    } finally {
      setMintBusy(false);
    }
  };

  const copyClaimCode = async () => {
    if (!mintResult?.claim_code) return;
    try {
      await navigator.clipboard.writeText(mintResult.claim_code);
      setCodeCopied(true);
      toast.ok('Claim code copied.');
      setTimeout(() => setCodeCopied(false), 2000);
    } catch {
      toast.error('Could not copy — copy the code manually.');
    }
  };

  const fetchGateways = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      params.append('limit', DEFAULT_LIMIT);
      params.append('offset', offset);

      // Filter by online status if not 'all'
      if (onlineFilter !== 'all') {
        params.append('online', onlineFilter === 'online' ? 'true' : 'false');
      }

      const res = await api.get(`/api/admin/gateways?${params}`);
      setData(res || { items: [], total: 0 });
    } catch (err) {
      setError(err);
      setData({ items: [], total: 0 });
    } finally {
      setLoading(false);
    }
  }, [offset, onlineFilter]);

  // Poll every 30s to catch online/offline state and firmware changes
  usePoll(fetchGateways, 30_000, [offset, onlineFilter]);

  // ---- OTA: version picker → named confirm (fleet-wide admin push) --------
  // Mirrors the CPO "Update firmware" flow (CpoGateways) but draws releases
  // from the admin registry and always exposes the custom-URL escape hatch.
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
      const res = await api.get('/api/admin/firmware-releases?active=true');
      const list = Array.isArray(res) ? res : res?.items || [];
      setReleases(list);
      // Default to the newest release — the common case is "push the latest".
      if (list.length > 0) setSelectedReleaseId(String(list[0].id));
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
      const res = await api.post(`/api/admin/gateways/${otaTarget.id}/ota`, body);
      toast.ok(res?.message || `Update pushed to ${otaTarget.name}.`);
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

  const items = data.items || [];
  const total = data.total || 0;

  // Compute firmware spread summary
  const spread = firmwareSpread(items);
  const maxFw = maxVersion(items);
  const spreadChips = Object.entries(spread)
    .sort(([v1], [v2]) => {
      const v1Parts = v1.split(/[.-]/).map((p) => parseInt(p, 10) || 0);
      const v2Parts = v2.split(/[.-]/).map((p) => parseInt(p, 10) || 0);
      for (let i = 0; i < Math.max(v1Parts.length, v2Parts.length); i++) {
        const av = v1Parts[i] || 0;
        const bv = v2Parts[i] || 0;
        if (av !== bv) return bv - av;
      }
      return 0;
    })
    .map(([version, count]) => ({
      version,
      count,
    }));

  // DataTable columns — render-based so the actions cell receives the raw
  // gateway (firmware_version string, online, plug_count) rather than the
  // pre-rendered status/firmware JSX.
  const columns = [
    { key: 'tenant_name', label: 'Organization' },
    { key: 'name', label: 'Gateway' },
    {
      key: 'last_seen',
      label: 'Status',
      render: (gw) => (
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <StatusDot state={gw.online ? 'available' : 'offline'} />
          <span>{relativeTime(gw.last_seen_at)}</span>
        </div>
      ),
    },
    {
      key: 'firmware_version',
      label: 'Firmware',
      render: (gw) => (
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <code>{gw.firmware_version || '—'}</code>
          {maxFw && gw.firmware_version && gw.firmware_version !== maxFw && (
            <span className="badge badge-warn">behind</span>
          )}
        </div>
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
          disabled={!gw.online || gw.plug_count === 0}
          title={
            !gw.online
              ? 'Gateway must be online to receive an update'
              : gw.plug_count === 0
                ? 'Gateway has no chargers to update'
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

  const mintAction = (
    <button type="button" className="btn btn-primary" onClick={openMint}>
      <Plus size={16} aria-hidden="true" />
      Mint inventory gateway
    </button>
  );

  const mintModal = (
    <Modal open={mintOpen} onClose={closeMint} title="Mint inventory gateway">
      {mintResult ? (
        <div className="stack">
          <p className="text-2 text-sm">
            Gateway <code>{mintResult.gateway_id}</code> is now unclaimed inventory. Copy this
            claim code onto the unit&apos;s label — it&apos;s shown here once.
          </p>
          <div className="field">
            <label className="field-label" htmlFor="mint-claim-code">
              Claim code
            </label>
            <div style={{ display: 'flex', gap: '8px' }}>
              <input id="mint-claim-code" className="input" value={mintResult.claim_code} readOnly />
              <button type="button" className="btn btn-quiet" onClick={copyClaimCode}>
                {codeCopied ? 'Copied!' : 'Copy'}
              </button>
            </div>
          </div>
          <div className="modal-actions">
            <button type="button" className="btn btn-primary" onClick={closeMint}>
              Done
            </button>
          </div>
        </div>
      ) : (
        <form className="stack" onSubmit={submitMint}>
          <p className="text-2 text-sm">
            Pre-register a preflashed unit as unclaimed inventory with a fresh claim code (deploy/docs/preflashed_unit_runbook.md).
            The gateway ID is the device&apos;s MAC, read off it during flashing.
          </p>
          <div className="field">
            <label className="field-label" htmlFor="mint-gateway-id">
              Gateway ID (device MAC)
            </label>
            <input
              id="mint-gateway-id"
              className="input"
              value={mintGatewayId}
              onChange={(e) => setMintGatewayId(e.target.value)}
              placeholder="a1b2c3d4e5f6"
              required
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="mint-name">
              Name <span className="text-2 text-sm">(optional)</span>
            </label>
            <input
              id="mint-name"
              className="input"
              value={mintName}
              onChange={(e) => setMintName(e.target.value)}
              placeholder="Batch 2026-08 unit 014"
            />
          </div>
          {mintError && <p className="field-error">{mintError}</p>}
          <div className="modal-actions">
            <button type="button" className="btn btn-quiet" onClick={closeMint} disabled={mintBusy}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={mintBusy}>
              {mintBusy ? 'Minting…' : 'Mint gateway'}
            </button>
          </div>
        </form>
      )}
    </Modal>
  );

  if (error) {
    return (
      <>
        <PageHeader title="Gateways" sub="Fleet overview and status" actions={mintAction} />
        <ErrorState error={error} onRetry={fetchGateways} title="Couldn't load gateways" />
        {mintModal}
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Gateways"
        sub={`${total} gateway${total !== 1 ? 's' : ''} across the network`}
        actions={mintAction}
      />

      {/* Firmware spread summary */}
      {!loading && items.length > 0 && (
        <div className="filter-bar">
          <div className="filter-count">
            {spreadChips.length > 0 && (
              <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                {spreadChips.map(({ version, count }) => (
                  <span key={version} className="chip">
                    {version} ×{count}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Online/Offline filter */}
      <div className="filter-bar">
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <label htmlFor="online-filter" className="field-label">
            Status:
          </label>
          <select
            id="online-filter"
            className="select"
            value={onlineFilter}
            onChange={(e) => {
              setOnlineFilter(e.target.value);
              setOffset(0); // reset pagination on filter change
            }}
          >
            <option value="all">All</option>
            <option value="online">Online only</option>
            <option value="offline">Offline only</option>
          </select>
        </div>
      </div>

      {/* DataTable */}
      <div className="table-wrap">
        <DataTable
          columns={columns}
          rows={items}
          keyField="id"
          loading={loading}
          emptyIcon={Server}
          emptyTitle="No gateways yet"
          emptyBody="Gateways will appear here once registered."
          pagination={{
            total,
            offset,
            limit: DEFAULT_LIMIT,
            onPage: (newOffset) => {
              setOffset(newOffset);
            },
          }}
        />
      </div>
      {mintModal}

      {/* OTA: version picker (active releases; custom URL always available) */}
      <Modal open={Boolean(otaTarget) && !otaConfirming} onClose={closeOta} title="Update firmware">
        {otaTarget && (
          <form className="stack" onSubmit={submitOta}>
            <p className="text-2 text-sm">
              Push an update to <strong>{otaTarget.name}</strong>, currently on{' '}
              <strong>{otaTarget.firmware_version || 'unknown'}</strong>. It downloads the image,
              reboots, and rolls back automatically if it fails to reconnect — it refuses the
              update while a session is active.
            </p>

            {!useCustomUrl && (
              <div className="field">
                <label className="field-label" htmlFor="admin-ota-release">
                  Firmware version
                </label>
                {releasesLoading ? (
                  <p className="text-2 text-sm">Loading available versions…</p>
                ) : releasesError ? (
                  <p className="field-error">{releasesError}</p>
                ) : releases.length === 0 ? (
                  <p className="text-2 text-sm">
                    No active firmware releases. Register one on the Firmware page first.
                  </p>
                ) : (
                  <select
                    id="admin-ota-release"
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
                {selectedRelease?.notes && <span className="field-help">{selectedRelease.notes}</span>}
              </div>
            )}

            {useCustomUrl && (
              <div className="field">
                <label className="field-label" htmlFor="admin-ota-url">
                  Firmware image URL (https)
                </label>
                <input
                  id="admin-ota-url"
                  type="url"
                  className="input"
                  value={customUrl}
                  onChange={(e) => setCustomUrl(e.target.value)}
                  placeholder="https://storage.googleapis.com/…/amphive-gateway-<version>.bin"
                  pattern="https://.*"
                  required
                />
                <span className="field-help">
                  Must be https and signed — firmware ≥ 1.4.0 verifies the signature and rejects
                  plain http.
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

            <button
              type="button"
              className="btn btn-ghost btn-sm btn-full"
              onClick={() => setUseCustomUrl((v) => !v)}
            >
              {useCustomUrl ? 'Pick a registered version instead' : 'Use a custom URL instead'}
            </button>
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
    </>
  );
}
